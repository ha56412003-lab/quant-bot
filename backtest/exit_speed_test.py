"""出场加速对比回测 —— 量化「持仓太長、回吐嚴重」的解法

背景（2026-11-09 浩哥）：
    78 笔纸面平仓记录里——
      策略自动出场 35 笔：平均最大浮盈 2.67%，实际到手 -1.29%，回吐 3.96%，均持仓 8.9h
      浩哥手动平   36 笔：平均最大浮盈 2.44%，实际到手 +1.63%，回吐 0.82%，均持仓 3.1h
    结论：入场抓到的浮盈策略其实更多，但拿太久全吐回去还倒亏。

口径：直接 import backend.strategies._ce_zlsma（线上同一份实现），
      与 s15_live_parity.py 完全同源。不做任何自研指标。

自证：本脚本 sim(trail_arm=0) 必须与 s15_live_parity.simulate(want_mirror=True)
      的笔数与净 R 完全一致，否则拒绝出结果（防止又出现口径漂移）。

用法：python backtest/exit_speed_test.py
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma
from backtest.s15_live_parity import simulate as ref_simulate, stats, FEE, SWING, BBW_MIN, MAX_RISK_PCT

BARS_PER_HOUR = 4          # 15m K 线


def _atr20(ks, n=20):
    """ATR(n) 序列（真实波幅均值），用于判定「大阳线/大阴线」的力度。
    用原始 K 线算，不用 HA —— HA 是平滑过的，实体天然偏小会低估力度。"""
    tr = []
    out: list = [None] * len(ks)
    for i, k in enumerate(ks):
        h = float(k["high"])
        l = float(k["low"])
        if i == 0:
            tr.append(h - l)
            out[0] = h - l
            continue
        pc = float(ks[i - 1]["close"])
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        if i >= n - 1:
            out[i] = sum(tr[i - n + 1:i + 1]) / n
    return out


def _is_key_bar(ks, i, atr20, want, mult=1.0, closepos=0.66, bodyfrac=0.5):
    """关键K线（大阳线/大阴线）—— 三条同时满足，全部用原始 K 线：
      ① 力度：实体 > mult × ATR(20)          「比平常波动明显大」
      ② 掌控力：收盘位置 > closepos          「收在最高/最低的 1/3 区」
      ③ 决断力：实体占全幅 > bodyfrac        「实体比影线长，不是十字星」
    收盘位置是核心：收盘价 = 多空最终达成的共识，收在极值说明对手毫无反击。
    """
    o = float(ks[i]["open"])
    c = float(ks[i]["close"])
    h = float(ks[i]["high"])
    l = float(ks[i]["low"])
    rng = h - l
    if rng <= 0:
        return False
    a = atr20[i]
    if a is None or a <= 0:
        return False
    body = abs(c - o)
    if body <= mult * a:              # ① 力度不够
        return False
    if body / rng <= bodyfrac:        # ③ 影线太长
        return False
    if want == "long":
        if c <= o:                    # 必须是阳线
            return False
        return (c - l) / rng > closepos   # ② 收在上 1/3
    else:
        if c >= o:                    # 必须是阴线
            return False
        return (h - c) / rng > closepos   # ② 收在下 1/3


def sim(ks, ind, half_at_r=2.0, half_tp_r=1.0, maxh=64, mirror_bbw=True,
        mirror_bbw_min=70.0, min_dev_pct=0.5,
        trail_arm=0.0, trail_dd=0.0, trail_after_half=False,
        max_risk_pct=None, swing=None, time_stop_always=False,
        qf_bars=0, qf_min_r=0.0,
        stop_mode="swing", min_stop_pct=0.0, eff_mask=None,
        key_bar=False, key_mult=1.0, key_closepos=0.66, key_bodyfrac=0.5,
        stop_key=False, right_exit=False, full_at_r=0.0, right_exit_strict=True,
        key_buf_mult=0.0, stop_key_far=False, bbw_min=None, dev_atr_mult=0.0,
        mirror_exit=True):
    """与线上同源的单币逐笔回测，新增 trail_arm / trail_dd 移动止盈。

    trail_arm=0 表示关闭移动止盈 → 行为必须与原 simulate 完全一致。

    stop_mode（2026-11-10 新增，浩哥测「回到作者原意」用）：
        "swing" —— 前 swing 根原始 K 线最低/最高点（现行线上）
        "ce"    —— 作者原设计 Chandelier Exit 通道，直接用
                   _ce_zlsma 返回的 long_stop / short_stop（收盘价 ± 2×ATR(1)，带棘轮）
        "both"  —— 两者并列、谁先到算谁：多头取较高者、空头取较低者（更早止损）
        默认 "swing" 保证旧结果一字不变。

    min_stop_pct（2026-11-10 新增）：
        模拟监控池「止损距离 ≥ X%」的筛除效果 —— 止损距离低于该值的信号不开仓。
        线上是池子层（quality.STOP_MIN_PCT=1.00）按币筛，这里是按信号筛，
        口径不完全等价（池子整轮排除该币），但方向性结论可用。0 = 不筛。

    移动止盈语义：浮盈达到 trail_arm 倍 R 后，从峰值回吐 trail_dd 倍 R 即平仓。

    trail_after_half=True 时，移动止盈只在「已平过一半」之后才启用 ——
    用来测「1.8R 平一半，剩下半仓改用移动止损接管」这类方案。
    否则 trail_arm(1.5) < half_at_r(1.8) 会在平半之前就砍掉，1.8R 永远到不了。

    qf_bars / qf_min_r：快速失败（线上 _quick_fail，2026-11-08 上线）。
        满 qf_bars 根 K 线后，峰顶仍 < qf_min_r R → 平仓。
        线上参数 quickfail_hours 4.0 / quickfail_min_r 0.5 → qf_bars=16。
        ⚠ 2026-11-09 审计发现：此前 sim() 与 simulate() 都没有这条，
          导致回测里「进去 4 小时没起色」的单子继续跑到 12 小时，
          与线上完全不是同一条路径。默认 0 = 关闭（保持口径自证成立），
          要贴近实盘必须显式传 qf_bars=16, qf_min_r=0.5。
    """
    ho, hc = ind["open"], ind["close"]
    zl, dirs, atr, bp = ind["zlsma"], ind["dir"], ind["atr"], ind["bbw_pct"]
    hi_raw, lo_raw = ind["high_raw"], ind["low_raw"]
    n = len(hc)
    _sw = SWING if swing is None else int(swing)
    # ATR(20) 只在用到「关键K线」时才计算（避免拖慢旧口径回测）
    atr20 = _atr20(ks, 20) if (key_bar or stop_key or right_exit) else None
    out = []
    i = max(1, 60, _sw)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None or atr[i] is None:
            i += 1
            continue
        sl = dirs[i] == 1 and dirs[i - 1] == -1 and hc[i] > zl[i]
        ss = dirs[i] == -1 and dirs[i - 1] == 1 and hc[i] < zl[i]
        if not (sl or ss):
            i += 1
            continue
        want = "long" if sl else "short"
        key = (hc[i] > ho[i] and hc[i] > zl[i]) if want == "long" \
            else (hc[i] < ho[i] and hc[i] < zl[i])
        if not key:
            i += 1
            continue
        # ② 关键K线闸（浩哥 2026-11-10 定义）：光有 HA 形态不够，那根 K 线
        #    本身必须是大阳线/大阴线 —— 实体够大 + 收在极值 + 实体比影线长。
        #    HA 判方向，原始 K 线判力度（HA 平滑过，实体偏小会低估）。
        if key_bar and not _is_key_bar(ks, i, atr20, want,
                                       key_mult, key_closepos, key_bodyfrac):
            i += 1
            continue
        # EFF 池子闸：eff_mask 为 False 的 K 线点不允许开仓
        # （模拟 quality.py 里「EFF ≥ EFF_MIN 才进池」的筛币效果）
        if eff_mask is not None and not eff_mask[i]:
            i += 1
            continue
        # ③ BBW 分位闸（bbw_min=None → 用模块常量 BBW_MIN；设 0 → 关闭这道闸）
        if bbw_min is None or bbw_min > 0:
            _bm = BBW_MIN if bbw_min is None else bbw_min
            if bp[i] is None or bp[i] < _bm:
                i += 1
                continue
        # ⑥ 进场位置闸（线上 yaml min_dev_pct: 0.5）：进场价离 ZLSMA 必须够远。
        #    贴均线 = 突破还没走出去 = 假突破。线上一直有，回测此前没有 → 多开单。
        #    dev_atr_mult > 0 时改用自适应门槛：偏离 ≥ mult × ATR（按波动归一，跨币可比）。
        if min_dev_pct > 0 or dev_atr_mult > 0:
            _dev = abs(float(ks[i]["close"]) - float(zl[i])) / float(zl[i]) * 100.0
            _dthr = min_dev_pct
            if dev_atr_mult > 0:
                _a20 = atr20[i] if (atr20 is not None and i < len(atr20)) else None
                _ap = (_a20 / float(ks[i]["close"]) * 100.0) if _a20 else 0.0
                _dthr = max(_dthr, dev_atr_mult * _ap)
            if _dev < _dthr:
                i += 1
                continue
        entry = float(ks[i + 1]["open"])
        if entry <= 0:
            i += 1
            continue
        d_swing = d_ce = d_key = None   # 三个候选止损的距离%（仅供分析，不影响决策）
        if stop_mode == "ce":
            # 作者原设计：Chandelier Exit 通道（收盘价 ± 2×ATR(1)，带棘轮上移/下移）
            stop = ind["long_stop"][i] if want == "long" else ind["short_stop"][i]
            stop_src = "ce"
            if stop is None:
                i += 1
                continue
            d_ce = abs(entry - stop) / entry * 100
        else:
            lo = max(0, i - _sw + 1)
            s_lo = min(lo_raw[j] for j in range(lo, i + 1))
            s_hi = max(hi_raw[j] for j in range(lo, i + 1))
            stop = s_lo if want == "long" else s_hi
            stop_src = "swing"
            d_swing = abs(entry - stop) / entry * 100
            if stop_mode == "both":
                # 双止损并列、谁先到算谁：多头取较高者，空头取较低者（= 更靠近入场价）
                ce_stop = ind["long_stop"][i] if want == "long" else ind["short_stop"][i]
                if ce_stop is not None:
                    d_ce = abs(entry - ce_stop) / entry * 100
                    if (want == "long" and ce_stop > stop) or (want == "short" and ce_stop < stop):
                        stop = ce_stop
                        stop_src = "ce"
        # ③ 关键K线止损（浩哥 2026-11-10）：那根大阳线的低点 / 大阴线的高点。
        #    与摆动低点、CE 通道三者并列，谁先到算谁 → 多头取较高者、空头取较低者。
        if stop_key:
            kb = float(ks[i]["low"]) if want == "long" else float(ks[i]["high"])
            # 改法A（浩哥 2026-11-10）：关键K线低点再往下留缓冲，缓冲 = mult × ATR(20)。
            #   目的：别再被「贴身的止损」在走出 0.7R 后拽回来。代价是 1R 变大。
            _buf = 0.0
            if key_buf_mult > 0 and atr20 is not None and atr20[i]:
                _buf = key_buf_mult * atr20[i]
                kb = (kb - _buf) if want == "long" else (kb + _buf)
            d_key = abs(entry - kb) / entry * 100
            # 改法B：与摆动止损「取远」而不是取近（多头取更低者，空头取更高者）。
            if stop_key_far:
                stop = min(stop, kb) if want == "long" else max(stop, kb)
                stop_src = "key_far"
            else:
                if (want == "long" and kb > stop) or (want == "short" and kb < stop):
                    stop = kb
                    stop_src = "key"
        risk = (entry - stop) if want == "long" else (stop - entry)
        _cap = MAX_RISK_PCT if max_risk_pct is None else max_risk_pct
        if risk <= 0 or risk / entry * 100 > _cap:
            i += 1
            continue
        # 模拟监控池「止损距离 ≥ min_stop_pct」的筛除（线上在 quality.is_tradable）
        if min_stop_pct > 0 and risk / entry * 100 < min_stop_pct - 1e-9:
            i += 1
            continue

        sign = 1 if want == "long" else -1
        peak = 0.0
        half_done = False
        ep = er = ei = None
        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            # ① 硬止损
            if want == "long" and l_ <= stop:
                ep, er, ei = min(o_, stop), "hard_stop", j
                break
            if want == "short" and h_ >= stop:
                ep, er, ei = max(o_, stop), "hard_stop", j
                break
            peak = max(peak, ((h_ - entry) if want == "long" else (entry - l_)) / risk)
            cur = ((c_ - entry) if want == "long" else (entry - c_)) / risk
            # ⓪ 快速失败（线上 _quick_fail：满 4h 峰顶仍 < 0.5R 就走）
            #    线上在 check_exit 最前面，仅排在 engine 的 12h/硬止损之后
            if qf_bars and (j - i) >= qf_bars and peak < qf_min_r - 1e-9:
                ep, er, ei = c_, "quickfail", j
                break
            # ② 2R 平一半
            if peak >= half_at_r and not half_done:
                half_done = True
            # ③ 时间止损
            #    默认：平过半就豁免（线上现行，engine.py:137）
            #    time_stop_always=True：一视同仁，平半后照样到点强平
            #    —— 长尾的根源就是「平半后不再受时间约束」，
            #       平半线越低，越多单子脱离管束，能磨到 90h。
            if (time_stop_always or not half_done) and (j - i) >= maxh:
                ep, er, ei = c_, "timeout", j
                break
            # ④ 半仓后再赚 half_tp_r 全平
            #    _EPS：2.8-1.8=0.9999999999999996，不加容差会漏掉「恰好到 3R」的单子
            if half_done and half_tp_r > 0 and (cur - half_at_r) >= half_tp_r - 1e-9:
                ep, er, ei = c_, "half_tp", j
                break
            # ④b 最终盈利目标全平（浩哥 2026-11-10：1:1.2 平半后，剩半仓到 2R 全平）
            if half_done and full_at_r > 0 and cur >= full_at_r - 1e-9:
                ep, er, ei = c_, "full_tp", j
                break
            # ④c 右侧止盈（浩哥 2026-11-10 新方案）：平半后不设固定目标，
            #     等趋势自己走坏 —— HA 方向反转 + 收盘穿 ZLSMA50 + 那根原始 K 线
            #     本身是反向的关键K线（大阴线/大阳线）。三者同时满足才走。
            if half_done and right_exit:
                ha_rev = (hc[j] < ho[j] and hc[j] < zl[j]) if want == "long" \
                    else (hc[j] > ho[j] and hc[j] > zl[j])
                rev = "short" if want == "long" else "long"
                # right_exit_strict=True（浩哥原话）：那根反向 K 线本身也要是
                #   大阴线/大阳线。False：只要 HA 反向 + 收盘穿 ZLSMA 就走。
                if right_exit_strict:
                    ok_exit = ha_rev and _is_key_bar(ks, j, atr20, rev,
                                                     key_mult, key_closepos, key_bodyfrac)
                else:
                    ok_exit = ha_rev
                if ok_exit:
                    ep, er, ei = c_, "right_exit", j
                    break
            # ⑤ 移动止盈（arm 后才生效；trail_after_half 时只在平半后启用）
            if trail_arm > 0 and (half_done or not trail_after_half) \
                    and peak >= trail_arm - 1e-9 and (peak - cur) >= trail_dd - 1e-9:
                ep, er, ei = c_, "trail", j
                break
            # ⑥ 镜像出场（mirror_exit=False → 整条关闭，2026-11-10 新方案不再用）
            if mirror_exit:
                flip = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                    else (dirs[j] == 1 and dirs[j - 1] == -1)
                ha_ok = (hc[j] < ho[j] and hc[j] < zl[j]) if want == "long" \
                    else (hc[j] > ho[j] and hc[j] > zl[j])
                # 2026-11-09 浩哥选 B：镜像加布林带宽度分位门槛，线上已落地（yaml
                # mirror_bbw_min: 70.0），所以这里默认 mirror_bbw=True 才是线上真实口径。
                # 历史：2026-11-08 曾定过「出场不加 BBW」，但那是在比两套完全不同的
                #   出场方案（旧破均线 vs 新镜像），不是单变量；换成 1.8半+移动止盈
                #   之后前提变了，实测 8 仓并发下带 ≥70 比不带多赚 15.0R、回撤还小 2.0R。
                #   不带时镜像占出场 43%、均笔 −0.08R（在震荡里被反复扫）。
                ok_bp = (not mirror_bbw) or (bp[j] is not None and bp[j] >= mirror_bbw_min)
                if flip and ha_ok and ok_bp:
                    ep, er, ei = c_, "mirror", j
                    break
        if ep is None:
            ei = n - 1
            ep = float(ks[-1]["close"])
            er = "eod"
        g = (ep - entry) * sign
        r = (g - (entry + ep) * FEE) / risk
        if half_done:
            r = 0.5 * half_at_r + 0.5 * r
        out.append(dict(side=want, r=r, bars=ei - i, reason=er, peak=peak,
                        risk_pct=risk / entry * 100, stop_src=stop_src,
                        d_swing=d_swing, d_ce=d_ce, d_key=d_key,
                        entry_ts=int(ks[i + 1].get("ts", i + 1)),
                        exit_ts=int(ks[ei].get("ts", ei))))
        i = ei + 1
    return out


BASE = dict(half_at_r=1.8, half_tp_r=0.0, trail_arm=1.5, trail_after_half=True)
PLANS = [
    ("基线（现行 2R半/3R全平）",        dict()),
    ("参考★ 当前最优 1.8半+移动1.5/1.1", dict(BASE, trail_dd=1.1)),
    # ── 用户方案：1.2R 平一半，剩余半仓移动止损 ──
    ("1.2半 + 移动1.0/0.5 ★用户",     dict(half_at_r=1.2, half_tp_r=0.0, trail_arm=1.0,
                                        trail_dd=0.5, trail_after_half=True)),
    ("1.2半 + 移动1.0/0.4",           dict(half_at_r=1.2, half_tp_r=0.0, trail_arm=1.0,
                                        trail_dd=0.4, trail_after_half=True)),
    ("1.2半 + 移动1.0/0.6",           dict(half_at_r=1.2, half_tp_r=0.0, trail_arm=1.0,
                                        trail_dd=0.6, trail_after_half=True)),
    ("1.2半 + 移动0.8/0.5",           dict(half_at_r=1.2, half_tp_r=0.0, trail_arm=0.8,
                                        trail_dd=0.5, trail_after_half=True)),
    ("1.4半 + 移动1.0/0.5",           dict(half_at_r=1.4, half_tp_r=0.0, trail_arm=1.0,
                                        trail_dd=0.5, trail_after_half=True)),
]


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    cache = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        cache.append((sym, ks))
    print("载入 %d 币" % len(cache))

    inds = {sym: _ce_zlsma(ks, 1, 2.0, 50, True, True) for sym, ks in cache}

    # ── 口径自证 ────────────────────────────────────────────────
    ref = []
    for sym, ks in cache:
        ref.extend(ref_simulate(ks, inds[sym], want_mirror=True, use_bbw=True))
    ref_s = stats(ref)
    mine = []
    for sym, ks in cache:
        # 自证必须与母本同口径 —— 显式关闭 2026-11-09 之后才加的三项特性：
        #   偏离闸 min_dev_pct、快速失败 qf_bars、移动止盈 trail_arm。
        # 母本 s15_live_parity.simulate() 至今没有这三项，不显式关掉就会一直报不一致
        # （2026-11-10 查明：这不是 bug，是自证调用的默认值没跟上）。
        mine.extend(sim(ks, inds[sym], min_dev_pct=0.0, qf_bars=0, trail_arm=0.0))
    my_s = stats(mine)
    print("\n【口径自证】trail 关闭时，必须与 s15_live_parity 完全一致")
    print("  参考实现: %d 笔 / %+.2fR" % (ref_s["n"], ref_s["r"]))
    print("  本脚本  : %d 笔 / %+.2fR" % (my_s["n"], my_s["r"]))
    if ref_s["n"] != my_s["n"] or abs(ref_s["r"] - my_s["r"]) > 1e-6:
        print("  ✗ 口径不一致，拒绝出结果")
        return 1
    print("  ✓ 一致（%d 笔零差异）" % ref_s["n"])

    # ── 参数矩阵 ────────────────────────────────────────────────
    # ⚠ 2026-11-09 审计教训：线上 check_exit 第一步是「快速失败」(_quick_fail
    #   4h/0.5R)，而 sim() / simulate() 都没有。不注入这条，回测里
    #   「进去 4 小时没起色」的单子会继续跑到 16 小时，与线上根本不是一条路径。
    #   实测对本次方案影响 13R 量级、对原方案影响 25R 量级，足以让结论反转。
    #   口径自证仍用 qf 关闭（ref_simulate 无此参数），只验「两条实现路径一致」；
    #   业务方案对比则一律按线上真实口径跑。
    LIVE_QF = dict(qf_bars=16, qf_min_r=0.5)
    print()
    print("口径：cap 5% / 15m / 线上含快速失败(4h,0.5R) / maxh 默认 16h")
    print("\n" + "=" * 112)
    print("%-22s%7s%10s%9s%8s%10s%9s%8s" % (
        "方案", "笔数", "净R", "均笔R", "胜率", "最大回撤", "均持仓h", "最长h"))
    print("-" * 112)
    results = []
    for name, kw in PLANS:
        trades = []
        merged = dict(kw)
        merged.update(LIVE_QF)
        for sym, ks in cache:
            trades.extend(sim(ks, inds[sym], **merged))
        s = stats(trades)
        hmax = max(t["bars"] for t in trades) / BARS_PER_HOUR
        results.append((name, kw, s, trades))
        print("%-22s%7d%+10.1f%+9.3f%7.1f%%%+10.1f%9.2f%8.1f" % (
            name, s["n"], s["r"], s["avg"], s["win"], s["mdd"],
            s["bars"] / BARS_PER_HOUR, hmax))
    print("=" * 112)

    # ── 出场原因分布（看各出口贡献）──────────────────────────────
    print("\n【出场原因分布：笔数 / 合计R】")
    reasons = ["hard_stop", "timeout", "half_tp", "trail", "mirror", "eod"]
    print("%-22s" % "方案" + "".join("%16s" % r for r in reasons))
    print("-" * 112)
    for name, kw, s, trades in results:
        line = "%-22s" % name
        for rn in reasons:
            sel = [t for t in trades if t["reason"] == rn]
            if sel:
                line += "%16s" % ("%d/%+.0fR" % (len(sel), sum(t["r"] for t in sel)))
            else:
                line += "%16s" % "-"
        print(line)

    # ── 与基线对比 ──────────────────────────────────────────────
    base_r = results[0][2]["r"]
    base_h = results[0][2]["bars"] / BARS_PER_HOUR
    print("\n【相对基线的变化】")
    print("%-22s%12s%12s%10s" % ("方案", "净R差", "持仓缩短", "回撤差"))
    print("-" * 60)
    for name, kw, s, trades in results[1:]:
        print("%-22s%+12.1f%11.1f%%%+10.1f" % (
            name, s["r"] - base_r,
            (base_h - s["bars"] / BARS_PER_HOUR) / base_h * 100 if base_h else 0,
            s["mdd"] - results[0][2]["mdd"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
