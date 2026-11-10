"""
T3 中长周期趋势策略 —— 回测版
════════════════════════════════════════════════════════════════════════

设计目标（浩哥 2026-11-07 定）：
  · 最大回撤 ≤ 20%
  · 优先「稳定小赚」，不要大起大落
  · 能接受连续 2 个月不赚钱
  · 与 S15（日内 15m）物理隔离，周期不同、逻辑不同

与 S15 的根本区别：
  S15 = 看到信号就进（任何市场环境都做）
  T3  = 先判断「现在能不能做」，环境不对就空仓等

三个过滤器（T3 名字由来）：
  ① 市场状态过滤  —— 震荡市直接不做
  ② 蓄势过滤      —— 只在布林带收窄、能量积累后出手
  ③ 放量确认      —— 排除假突破

三档止盈（「稳定」的来源）：
  1.5R 平 1/3  → 先回本
  3.0R 再平 1/3 → 落袋
  剩 1/3       → 破均线才走，让大行情自己跑

周期：1h（持有 3~10 天，属中长周期）
════════════════════════════════════════════════════════════════════════
"""
import sys, os, json, glob, math, statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.s15_final import (
    heikin_ashi, zlsma, ce_dir, bollinger_width, pct_rank, efficiency_ratio,
)

# ── 成本参数（币安 U 本位 VIP0 实际值）──
FEE_TAKER = 0.0005      # 市价吃单 0.05%
FEE_MAKER = 0.0002      # 挂单 0.02%
SLIP = 0.0003           # 滑点 0.03%


# ══════════════════════════════════════════════════════════════════
# 指标构建
# ══════════════════════════════════════════════════════════════════
def build_t3(ks, z_len=50, atr_len=14):
    """在 S15 指标基础上，补 T3 需要的：布林带上下轨、成交量均线。"""
    n = len(ks)
    ho, hh, hl, hc = heikin_ashi(ks)
    zl = zlsma(hc, z_len)

    # ATR（用于止损距离）
    atr = _atr(hh, hl, hc, atr_len)

    # 布林带（20, 2.0）—— 上下轨 + 带宽
    bb_mid, bb_up, bb_lo = _bollinger(hc, 20, 2.0)
    bbw = [None if (bb_mid[i] in (None, 0) or bb_mid[i] == 0) else
           (bb_up[i] - bb_lo[i]) / bb_mid[i] for i in range(n)]
    bbw_pct = pct_rank([x if x is not None else 0 for x in bbw], 100)

    # 趋势效率（核心：判断市场状态）
    er20 = efficiency_ratio(hc, 20)
    er50 = efficiency_ratio(hc, 50)

    # 成交量均线（放量确认）
    vols = [float(k["volume"]) for k in ks]
    vol_ma20 = _sma(vols, 20)

    return {
        "ho": ho, "hh": hh, "hl": hl, "hc": hc, "zlsma": zl, "atr": atr,
        "bb_up": bb_up, "bb_lo": bb_lo, "bbw": bbw, "bbw_pct": bbw_pct,
        "er20": er20, "er50": er50, "vol_ma20": vol_ma20,
    }


def _sma(src, length):
    out = [None] * len(src)
    s = 0.0
    for i, v in enumerate(src):
        s += v
        if i >= length:
            s -= src[i - length]
        if i >= length - 1:
            out[i] = s / length
    return out


def _atr(hh, hl, hc, length=14):
    n = len(hc)
    tr = [None] * n
    for i in range(1, n):
        tr[i] = max(hh[i] - hl[i], abs(hh[i] - hc[i - 1]), abs(hl[i] - hc[i - 1]))
    out = [None] * n
    if n <= length:
        return out
    first = sum(t for t in tr[1:length + 1] if t is not None) / length
    out[length] = first
    for i in range(length + 1, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


def _bollinger(src, length=20, mult=2.0):
    n = len(src)
    mid = [None] * n
    up = [None] * n
    lo = [None] * n
    for i in range(length - 1, n):
        win = src[i - length + 1:i + 1]
        m = sum(win) / length
        var = sum((x - m) ** 2 for x in win) / length
        sd = math.sqrt(var)
        mid[i], up[i], lo[i] = m, m + mult * sd, m - mult * sd
    return mid, up, lo


# ══════════════════════════════════════════════════════════════════
# 市场状态判定
# ══════════════════════════════════════════════════════════════════
def regime_at(ind, i, er_trend=0.50, er_range=0.25):
    """用 50 根趋势效率判断市场状态。

    TREND : 单边市 —— 可做，顺方向
    RANGE : 震荡市 —— 不做
    MIXED : 混合   —— 仓位减半
    """
    er = ind["er50"][i] if i < len(ind["er50"]) else None
    if er is None:
        return "UNKNOWN"
    if er >= er_trend:
        return "TREND"
    if er <= er_range:
        return "RANGE"
    return "MIXED"


# ══════════════════════════════════════════════════════════════════
# 模拟器
# ══════════════════════════════════════════════════════════════════
def simulate_t3(
    sym, ks, ind,
    # 入场门槛
    entry_mode="pullback",   # "pullback" 回调买入（默认） / "breakout" 突破买入
    pullback_atr=0.8,        # 回调买入：价格离均线不超过 0.8 倍 ATR
    er_trend=0.50, er_range=0.25,
    bbw_pct_max=30.0,        # 突破「前一根」的带宽分位 ≤ 此值（蓄势）
    vol_mult=1.5,            # 成交量 ≥ 1.5 倍均量
    min_atr_pct=1.0,         # 波动率下限（1h 上 1% 已不算小）
    # 止损与止盈
    stop_atr_mult=2.5,       # 初始止损 2.5 ATR
    tp1_r=1.5, tp1_frac=1/3,
    tp2_r=3.0, tp2_frac=1/3,
    # 剩余尾仓
    trail_ma=True,           # 尾仓用均线
    time_stop_bars=168,      # 168 根 1h = 7 天
    max_risk_pct=25.0,       # 止损距离上限（太宽不做）
    fee=FEE_TAKER, slip=SLIP,
    allow_mixed=False,       # MIXED 市是否允许（默认不做）
    diag=None,               # 传 dict 进来收诊断计数
    # ── 阶梯移动止损（保护已有的利润）──
    be_at_r=1.0,             # 峰顶到 1R → 止损移到保本
    lock_at_r=2.0,           # 峰顶到 2R → 止损移到 +1R
    follow_dist_r=1.0,       # 峰顶超过 2R 后，止损始终离峰顶 1R
):
    """返回 (trades, skipped)。

    每笔 trade 的 r 按「整笔仓位的等效 R」记账：
      1/3 在 tp1_r、1/3 在 tp2_r、1/3 在尾仓（可能更高也可能回吐）
    """
    hc, ho = ind["hc"], ind["ho"]
    zl, atr = ind["zlsma"], ind["atr"]
    bb_up, bb_lo = ind["bb_up"], ind["bb_lo"]
    bbw_pct, vol_ma20 = ind["bbw_pct"], ind["vol_ma20"]
    n = len(hc)
    D = diag if diag is not None else {}

    def _bump(k):
        D[k] = D.get(k, 0) + 1

    trades, skipped = [], 0
    i = 60
    while i < n - 2:
        # 必要指标齐全
        if None in (zl[i], atr[i], bb_up[i], bb_lo[i], bbw_pct[i], vol_ma20[i]):
            i += 1
            continue

        # ① 市场状态
        reg = regime_at(ind, i, er_trend, er_range)
        if reg == "RANGE" or reg == "UNKNOWN":
            _bump("①震荡市不做"); i += 1; continue
        if reg == "MIXED" and not allow_mixed:
            _bump("①混合市不做"); i += 1; continue

        # ③ 入场信号源（两种模式，默认 pullback 回调买入）
        c_ = float(ks[i]["close"])
        want = None
        if entry_mode == "pullback":
            # 回踩均线买入：趋势中价格回到均线附近，出现反转确认才进
            # 多头：价格在均线上方一小段内（≤ pullback_atr 倍 ATR）+ 收阳且突破前收
            if c_ > zl[i]:
                dist_atr = (c_ - zl[i]) / atr[i]
                if dist_atr <= pullback_atr:
                    if c_ > float(ks[i - 1]["close"]) and c_ > float(ks[i]["open"]):
                        want = "long"
            if want is None and c_ < zl[i]:
                dist_atr = (zl[i] - c_) / atr[i]
                if dist_atr <= pullback_atr:
                    if c_ < float(ks[i - 1]["close"]) and c_ < float(ks[i]["open"]):
                        want = "short"
            if want is None:
                _bump("②不是回调买点"); i += 1; continue
        else:
            # 突破买入（保留对比用）
            up_break = c_ > bb_up[i]
            dn_break = c_ < bb_lo[i]
            if not (up_break or dn_break):
                _bump("②没突破"); i += 1; continue
            want = "long" if up_break else "short"
            prev_pct = bbw_pct[i - 1] if i > 0 else None
            if prev_pct is None or prev_pct > bbw_pct_max:
                _bump("③前一根没收窄"); i += 1; continue
            if float(ks[i]["volume"]) < vol_mult * vol_ma20[i]:
                _bump("④没放量"); i += 1; continue

        # ⑤ 顺势（价格与 50 均线同向）
        if want == "long" and c_ <= zl[i]:
            _bump("⑤逆势"); i += 1; continue
        if want == "short" and c_ >= zl[i]:
            _bump("⑤逆势"); i += 1; continue

        # ⑥ 波动率门槛
        cur_atr_pct = atr[i] / c_ * 100
        if cur_atr_pct < min_atr_pct:
            _bump("⑥波动不足"); i += 1; continue

        # ── 开仓 ──
        entry = c_ * (1 + slip) if want == "long" else c_ * (1 - slip)
        sign = 1 if want == "long" else -1
        stop = entry - sign * stop_atr_mult * atr[i]
        risk = abs(entry - stop)
        if risk <= 0 or risk / entry * 100 > max_risk_pct:
            i += 1
            continue

        # 分段仓位状态
        t1_done = t2_done = False
        realized_r = 0.0          # 已落袋的等效 R
        peak_r = 0.0
        exit_px = exit_reason = exit_i = None
        remain_frac = 1.0
        cur_stop = stop           # 会随峰顶往上抬的止损

        for j in range(i + 1, min(n, i + time_stop_bars + 1)):
            o_, h_, l_, cj = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            cur_r = ((cj - entry) if want == "long" else (entry - cj)) / risk

            # ── 止损（用移动后的 cur_stop）──
            hit_stop = (l_ <= cur_stop) if want == "long" else (h_ >= cur_stop)
            if hit_stop:
                px = min(o_, cur_stop) if want == "long" else max(o_, cur_stop)
                stop_r = ((px - entry) if want == "long" else (entry - px)) / risk
                realized_r += remain_frac * stop_r
                exit_px = px
                exit_reason = "移动止损" if cur_stop != stop else "止损"
                exit_i = j
                break

            # 峰顶
            peak_r = max(peak_r, ((h_ - entry) if want == "long" else (entry - l_)) / risk)

            # ── 阶梯止损：峰顶越高，止损抬得越高（下一根生效）──
            if be_at_r > 0 and peak_r >= be_at_r:
                be_stop = entry - sign * 0.0        # 保本
                if (want == "long" and be_stop > cur_stop) or (want == "short" and be_stop < cur_stop):
                    cur_stop = be_stop
            if lock_at_r > 0 and peak_r >= lock_at_r:
                lock_stop = entry + sign * (lock_at_r - 1.0) * risk   # 至少锁住 +1R
                if (want == "long" and lock_stop > cur_stop) or (want == "short" and lock_stop < cur_stop):
                    cur_stop = lock_stop
                if follow_dist_r > 0:
                    trail_stop = entry + sign * max(0.0, peak_r - follow_dist_r) * risk
                    if (want == "long" and trail_stop > cur_stop) or (want == "short" and trail_stop < cur_stop):
                        cur_stop = trail_stop

            # ── 第一档 1.5R ──
            if not t1_done and peak_r >= tp1_r:
                realized_r += tp1_frac * tp1_r
                remain_frac -= tp1_frac
                t1_done = True

            # ── 第二档 3.0R ──
            if t1_done and not t2_done and peak_r >= tp2_r:
                realized_r += tp2_frac * tp2_r
                remain_frac -= tp2_frac
                t2_done = True

            # ── 尾仓：破均线走 ──
            if t2_done and trail_ma and zl[j] is not None:
                reversed_ = (cj < zl[j]) if want == "long" else (cj > zl[j])
                if reversed_:
                    realized_r += remain_frac * cur_r
                    exit_px, exit_reason, exit_i = cj, "尾仓破均线", j
                    break
            # 若没开第二档但已到时间，按当前价走
        if exit_i is None:
            j2 = min(n - 1, i + time_stop_bars)
            cj = float(ks[j2]["close"])
            cur_r = ((cj - entry) if want == "long" else (entry - cj)) / risk
            realized_r += remain_frac * cur_r
            exit_px, exit_reason, exit_i = cj, "时间止损", j2

        # ── 扣手续费（按名义价值两趟）──
        gross_r = realized_r
        fee_r = 2 * fee * entry / risk      # 双边手续费折算成 R
        net_r = gross_r - fee_r

        trades.append({
            "sym": sym, "side": want, "regime": reg,
            "entry": entry, "exit": exit_px,
            "risk_pct": risk / entry * 100,
            "bars": exit_i - i,
            "hours": (exit_i - i),
            "gross_r": gross_r, "fee_r": fee_r, "net_r": net_r,
            "reason": exit_reason,
            "peak_r": peak_r,
        })
        i = exit_i + 1

    return trades, skipped


# ══════════════════════════════════════════════════════════════════
# 统计
# ══════════════════════════════════════════════════════════════════
def stats(trades):
    if not trades:
        return None
    rs = [t["net_r"] for t in trades]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    total = sum(rs)

    # 最大回撤（R 计的权益曲线）
    eq = peak = 0.0
    maxdd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)

    gross_win = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    pf = gross_win / gross_loss if gross_loss > 0 else 999

    why = {}
    for t in trades:
        why[t["reason"]] = why.get(t["reason"], 0) + 1

    return {
        "n": n,
        "win": len(wins) / n * 100,
        "total": total,
        "avg": statistics.mean(rs),
        "pf": pf,
        "dd": maxdd,
        "dd_pct_of_gain": abs(maxdd) / total * 100 if total > 0 else 999,
        "why": why,
        "med_risk_pct": statistics.median(t["risk_pct"] for t in trades),
        "avg_bars": statistics.mean(t["bars"] for t in trades),
        "gross": sum(t["gross_r"] for t in trades),
        "fee": sum(t["fee_r"] for t in trades),
        "regime": {r: sum(1 for t in trades if t["regime"] == r) for r in ("TREND", "MIXED")},
    }


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════
def load_local(interval="1h", days=120, limit=None):
    """读本地已下载的 K 线，聚合成目标周期。"""
    pat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                       f"*_{interval}_{days}d.json")
    files = sorted(glob.glob(pat))
    if limit:
        files = files[:limit]
    out = {}
    for f in files:
        sym = os.path.basename(f).split("_")[0]
        try:
            ks = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if len(ks) >= 500:
            out[sym] = ks
    return out


def main():
    interval = sys.argv[1] if len(sys.argv) > 1 else "1h"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 120

    print("\n" + "=" * 100)
    print("T3 中长周期趋势策略  |  市场状态过滤 + 蓄势突破 + 三档止盈")
    print(f"周期 {interval}  历史 {days} 天  手续费 {FEE_TAKER*100:.2f}% 双程")
    print("=" * 100)

    data = load_local(interval, days)
    print(f"\n载入 {len(data)} 个币的数据\n")

    all_trades = []
    diag = {}
    for sym, ks in sorted(data.items()):
        ind = build_t3(ks)
        ts, _ = simulate_t3(sym, ks, ind, diag=diag)
        all_trades.extend(ts)

    print("【过滤器诊断 —— 每个条件挡掉多少根 K 线】")
    for k in sorted(diag):
        print(f"  {k:<16} {diag[k]:>7}")
    print()

    if not all_trades:
        print("无成交 —— 条件太严，需要放宽")
        return

    # 按时间排序（近似：按 entry 价不稳定，这里按 sym 分组统计）
    r = stats(all_trades)

    print(f"【总体】{r['n']} 笔")
    print(f"  胜率     {r['win']:.1f}%")
    print(f"  净收益   {r['total']:+.2f}R   （毛利 {r['gross']:+.2f}R − 手续费 {r['fee']:.2f}R）")
    print(f"  每笔期望 {r['avg']:+.3f}R")
    print(f"  盈亏比   {r['pf']:.2f}")
    print(f"  最大回撤 {r['dd']:.1f}R   （= 收益的 {r['dd_pct_of_gain']:.0f}%）")
    print(f"  止损距离中位 {r['med_risk_pct']:.2f}%   平均持仓 {r['avg_bars']:.0f} 根 "
          f"（≈{r['avg_bars'] * (1 if interval == '1h' else 0.25):.0f} 小时）")
    print(f"  市场状态分布 {r['regime']}")
    print(f"\n  出场原因分布:")
    for k, v in sorted(r["why"].items(), key=lambda x: -x[1]):
        print(f"    {k}: {v}")

    # 按币看
    print(f"\n【分币种】")
    bys = {}
    for t in all_trades:
        bys.setdefault(t["sym"], []).append(t)
    rows = []
    for sym, ts in bys.items():
        s = stats(ts)
        rows.append((sym, s))
    rows.sort(key=lambda x: -x[1]["total"])
    print(f"  {'币种':<16} {'笔数':>4} {'胜率':>6} {'净R':>9} {'期望':>8}")
    for sym, s in rows[:12]:
        print(f"  {sym:<16} {s['n']:>4} {s['win']:>5.1f}% {s['total']:>+9.2f} {s['avg']:>+8.3f}")
    if len(rows) > 12:
        print(f"  ... 另有 {len(rows)-12} 个币")

    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
