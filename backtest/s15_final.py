"""S15 最终版 —— 浩哥 2026-11-05 定稿规则

════════════════════════════════════════════════════════════════
【固定不变量】
  · 图表：Heikin Ashi
  · Chandelier Exit：ATR 周期 1 × 倍数 2.0
  · 均线：ZLSMA 50
  · 止损：阶段性低点（20 根 K 线内最低/最高）下方

【入场】
  · 监控池 → 持仓池门槛：ATR > 2.5%
  · 趋势过滤：ADX(14) > 25（排除震荡）
  · 信号：CE 方向翻转（事件）+ 信号当根收盘在均线同侧 + 关键 K 线
  · 成交：挂单（maker 0.02%），挂在信号收盘价，下一根回踩才成交

【出场】（并列关系，谁先满足谁全平）
  ① 止损：碰到阶段低点 → 全平
  ② 趋势没反转：涨满 1.5R → 全平落袋
  ③ 趋势反转（收盘破均线）+ 从峰顶回撤 ≥ 0.6R → 全平
════════════════════════════════════════════════════════════════
"""
import asyncio
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.s15_original import _adx  # noqa: E402

FEE_TAKER = 0.0005
FEE_MAKER = 0.0002
SLIP = 0.0003
ONE_R = 50.0


# ══════════════════════════════════════════════════════════════
def heikin_ashi(ks):
    ho, hh, hl, hc = [], [], [], []
    for i, k in enumerate(ks):
        o, h, l, c = (float(k["open"]), float(k["high"]),
                      float(k["low"]), float(k["close"]))
        hc_ = (o + h + l + c) / 4.0
        ho_ = (o + c) / 2.0 if i == 0 else (ho[i - 1] + hc[i - 1]) / 2.0
        ho.append(ho_)
        hc.append(hc_)
        hh.append(max(h, ho_, hc_))
        hl.append(min(l, ho_, hc_))
    return ho, hh, hl, hc


def _linreg_series(src, length):
    """滚动线性回归（对应 Pine 的 ta.linreg(src, length, 0)）。

    ★ 关键：窗口内只要有一个 None，本根就返回 None（Pine 里 ta.linreg 遇 na 返回 na）。
      绝不允许把 None 当 0.0 参与求和 —— 那会把结果严重拉偏。
    """
    n = len(src)
    out = [None] * n
    for i in range(length - 1, n):
        start = i - length + 1
        seg = src[start:i + 1]
        if any(x is None for x in seg):
            continue
        xs = length * (length - 1) / 2
        ys = sum(seg)
        xxs = sum(j * j for j in range(length))
        xys = sum(j * seg[j] for j in range(length))
        den = length * xxs - xs * xs
        if den == 0:
            continue
        slope = (length * xys - xs * ys) / den
        out[i] = (ys - slope * xs) / length + slope * (length - 1)
    return out


def zlsma(src, length):
    """ZLSMA = lsma + (lsma - lsma2)，其中 lsma2 是对 lsma 再做一次线性回归。

    Pine 源码：
        lsma  = ta.linreg(src, zLen, 0)
        lsma2 = ta.linreg(lsma, zLen, 0)
        zlsma = lsma + (lsma - lsma2)
    """
    a = _linreg_series(src, length)
    b = _linreg_series(a, length)
    return [None if (a[i] is None or b[i] is None) else a[i] + (a[i] - b[i])
            for i in range(len(src))]




def _rma(src, length):
    out = [None] * len(src)
    acc = None
    for i, v in enumerate(src):
        if v is None:
            continue
        if acc is None:
            if i >= length - 1:
                seg = [x for x in src[i - length + 1:i + 1] if x is not None]
                if seg:
                    acc = sum(seg)
                    out[i] = acc / length
            continue
        acc = acc + v - acc / length
        out[i] = acc / length
    return out


def ce_dir(ho, hh, hl, hc, atr_len=1, mult=2.0):
    n = len(hc)
    tr = [0.0] * n
    for i in range(n):
        tr[i] = (hh[i] - hl[i]) if i == 0 else max(
            hh[i] - hl[i], abs(hh[i] - hc[i - 1]), abs(hl[i] - hc[i - 1]))
    atr = _rma(tr, atr_len)
    ls, ss, direc = [None] * n, [None] * n, [None] * n
    for i in range(n):
        if atr[i] is None:
            continue
        ls_raw = hh[i] - atr[i] * mult
        ss_raw = hl[i] + atr[i] * mult
        if i == 0 or ls[i - 1] is None:
            ls[i], ss[i], direc[i] = ls_raw, ss_raw, 1
            continue
        ls[i] = max(ls_raw, ls[i - 1]) if hc[i - 1] > ls[i - 1] else ls_raw
        ss[i] = min(ss_raw, ss[i - 1]) if hc[i - 1] < ss[i - 1] else ss_raw
        if direc[i - 1] == 1:
            direc[i] = -1 if hc[i] < ls[i] else 1
        else:
            direc[i] = 1 if hc[i] > ss[i] else -1
    return direc, atr


def bollinger_width(src, length=20, mult=2.0):
    """布林带宽度 % = (上轨-下轨)/中轨*100。宽度小=震荡挤压，宽度大=趋势展开。"""
    n = len(src)
    out = [None] * n
    for i in range(length - 1, n):
        seg = src[i - length + 1:i + 1]
        if any(x is None for x in seg):
            continue
        ma = sum(seg) / length
        if ma == 0:
            continue
        sd = (sum((x - ma) ** 2 for x in seg) / length) ** 0.5
        out[i] = (2 * mult * sd) / ma * 100.0
    return out


def pct_rank(series, lookback=100):
    """当前值在过去 lookback 根里的百分位（0~100）"""
    n = len(series)
    out = [None] * n
    for i in range(n):
        if series[i] is None:
            continue
        lo = max(0, i - lookback + 1)
        vals = [v for v in series[lo:i + 1] if v is not None]
        if len(vals) < 20:
            continue
        out[i] = sum(1 for v in vals if v <= series[i]) / len(vals) * 100.0
    return out


def efficiency_ratio(src, length=20):
    """Kaufman 效率比 = |净位移| / 总路程。1=完美单边，接近0=原地震荡。

    这是区分「单边趋势」和「来回大幅度甩」的关键指标 —— 布林带宽度做不到这件事。
    """
    n = len(src)
    out = [None] * n
    for i in range(length, n):
        seg = src[i - length:i + 1]
        if any(x is None for x in seg):
            continue
        net = abs(seg[-1] - seg[0])
        path = sum(abs(seg[j] - seg[j - 1]) for j in range(1, len(seg)))
        if path <= 0:
            continue
        out[i] = net / path
    return out


def build(ks, ce_len=1, ce_mult=2.0, z_len=50, adx_len=14):
    ho, hh, hl, hc = heikin_ashi(ks)
    zl = zlsma(hc, z_len)
    dirs, atr = ce_dir(ho, hh, hl, hc, ce_len, ce_mult)
    adx = _adx(ho, hh, hl, hc, adx_len)
    bbw = bollinger_width(hc, 20, 2.0)
    bbw_pct = pct_rank(bbw, 100)
    er20 = efficiency_ratio(hc, 20)
    er50 = efficiency_ratio(hc, 50)
    return {"ho": ho, "hh": hh, "hl": hl, "hc": hc, "zlsma": zl,
            "dir": dirs, "atr": atr, "adx": adx,
            "bbw": bbw, "bbw_pct": bbw_pct, "er20": er20, "er50": er50}


# ══════════════════════════════════════════════════════════════
def simulate(sym, ks, ind, swing_lookback=20, min_atr_pct=2.5, min_adx=25.0,
             rr_target=1.5, trail_r=0.6, maker_entry=True,
             fee_entry=None, slippage=SLIP, max_risk_pct=30.0,
             half_at_rr=False, adx_mode="level", adx_lookback=1,
             adx_min_level=0.0, bbw_min=0.0, bbw_pct_min=0.0,
             er_min=0.0, er_len=20,
             time_stop_bars=0, ma_confirm_bars=1, breakeven_r=0.0,
             stop_atr_mult=0.0,
             # ── 分段回撤（峰顶未到 trail_pct_max_r 时改用「浮盈回撤百分比」）──
             trail_pct=0.0, trail_pct_max_r=2.0, ma_bars_lt=None,
             trail_pct_cap=0.0, dd_force_mult=0.0,
             half_tp_r=0.0, half_dd_pct=0.0, half_dd_r=0.0,
             half_fresh=False, guard_split=True):
    """浩哥定稿规则。返回 (trades, missed)。

    trail_pct: 0=关闭。设为 0.5 表示峰顶不足 trail_pct_max_r 时，
               自峰顶回撤「浮盈的 50%」就走 —— 永远留住一半利润。
    ma_bars_lt: 走比例回撤时要求的连续破均线根数（None = 沿用 ma_confirm_bars）。
    """
    ho, hh, hl, hc = ind["ho"], ind["hh"], ind["hl"], ind["hc"]
    zl, dirs, atr, adx = ind["zlsma"], ind["dir"], ind["atr"], ind["adx"]
    n = len(hc)

    fee_in = fee_entry if fee_entry is not None else (FEE_MAKER if maker_entry else FEE_TAKER)

    trades, missed = [], 0
    i = max(1, 60, swing_lookback)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None or atr[i] is None:
            i += 1
            continue

        # ── 入场信号 ──
        sig_long = dirs[i] == 1 and dirs[i - 1] == -1 and hc[i] > zl[i]
        sig_short = dirs[i] == -1 and dirs[i - 1] == 1 and hc[i] < zl[i]
        if not (sig_long or sig_short):
            i += 1
            continue
        want = "long" if sig_long else "short"

        # 关键 K 线（同根）
        key_ok = (hc[i] > ho[i] and hc[i] > zl[i]) if want == "long" \
            else (hc[i] < ho[i] and hc[i] < zl[i])
        if not key_ok:
            i += 1
            continue

        # ATR 门槛
        cur_atr_pct = atr[i] / hc[i] * 100
        if cur_atr_pct < min_atr_pct:
            i += 1
            continue
        # ADX 过滤
        if adx_mode == "level":
            if adx[i] is not None and adx[i] < min_adx:
                i += 1
                continue
        elif adx_mode == "rising":
            k0 = i - adx_lookback
            if k0 < 0 or adx[i] is None or adx[k0] is None:
                i += 1
                continue
            if adx[i] <= adx[k0]:
                i += 1
                continue
            if adx_min_level > 0 and adx[i] < adx_min_level:
                i += 1
                continue
        # 布林带宽度过滤（排除震荡）
        if bbw_min > 0:
            if ind["bbw"][i] is None or ind["bbw"][i] < bbw_min:
                i += 1
                continue
        if bbw_pct_min > 0:
            if ind["bbw_pct"][i] is None or ind["bbw_pct"][i] < bbw_pct_min:
                i += 1
                continue
        # 效率比过滤（区分单边趋势 vs 来回震荡）
        if er_min > 0:
            key = "er50" if er_len == 50 else "er20"
            if ind[key][i] is None or ind[key][i] < er_min:
                i += 1
                continue

        # ── 成交 ──
        if maker_entry:
            limit = float(ks[i]["close"])
            nxt = ks[i + 1]
            if want == "long":
                if float(nxt["open"]) <= limit:
                    entry = float(nxt["open"])
                elif float(nxt["low"]) <= limit:
                    entry = limit
                else:
                    missed += 1
                    i += 1
                    continue
            else:
                if float(nxt["open"]) >= limit:
                    entry = float(nxt["open"])
                elif float(nxt["high"]) >= limit:
                    entry = limit
                else:
                    missed += 1
                    i += 1
                    continue
        else:
            entry = float(ks[i + 1]["open"])
        if entry <= 0:
            i += 1
            continue

        # ── 止损：阶段低点（原始 K 线）──
        lo = max(0, i - swing_lookback + 1)
        swing_low = min(float(ks[j]["low"]) for j in range(lo, i + 1))
        swing_high = max(float(ks[j]["high"]) for j in range(lo, i + 1))
        if want == "long":
            stop = swing_low * 0.999
            risk = entry - stop
        else:
            stop = swing_high * 1.001
            risk = stop - entry
        # stop_atr_mult > 0 时：止损至少放到 N×ATR 之外（取更远的那个）
        # 目的：15m 的「20 根阶段低点」中位只有 1.6%，一根插针就扫掉。
        # 用 ATR 兜底把止损推远，代价是仓位自动缩小（风险金额不变）。
        if stop_atr_mult > 0:
            _a = atr[i]
            if _a and _a > 0:
                if want == "long":
                    _atr_stop = entry - _a * stop_atr_mult
                    if _atr_stop < stop:
                        stop = _atr_stop
                else:
                    _atr_stop = entry + _a * stop_atr_mult
                    if _atr_stop > stop:
                        stop = _atr_stop
                risk = abs(entry - stop)
        if risk <= 0 or risk / entry * 100 > max_risk_pct:
            i += 1
            continue

        sign = 1 if want == "long" else -1
        peak_r = 0.0
        half_done = False
        half_pnl = 0.0
        exit_px = exit_reason = None
        exit_i = None
        cur_stop = stop              # 可上移的止损（保本）
        ma_break_cnt = 0             # 连续破均线计数

        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            # ① 止损（含上移后的保本止损）
            if want == "long" and l_ <= cur_stop:
                exit_px, exit_reason, exit_i = min(o_, cur_stop), "止损", j
                break
            if want == "short" and h_ >= cur_stop:
                exit_px, exit_reason, exit_i = max(o_, cur_stop), "止损", j
                break
            # 峰顶浮盈（用当根最高/最低）
            peak_r = max(peak_r, ((h_ - entry) if want == "long" else (entry - l_)) / risk)
            if half_done:
                peak_r_half = max(peak_r_half, cur_r - half_base_r)
            cur_r = ((c_ - entry) if want == "long" else (entry - c_)) / risk
            # ② 到 1.5R
            if peak_r >= rr_target and not half_done:
                if half_at_rr:
                    half_done = True
                    half_pnl = rr_target * 0.5 * risk
                    # 半仓账本的 0 点 = 平半那一刻的 R；峰顶和破线计数从零开始
                    half_base_r = cur_r
                    peak_r_half = 0.0
                    ma_break_cnt_half = 0
                else:
                    exit_px = entry + sign * rr_target * risk
                    exit_reason, exit_i = "1.5R止盈", j
                    break
            # ②-b 半仓专属出场（2R 平掉一半之后，管剩下那一半）
            # 门槛按「平半后新赚的那一段」算：从开仓算的话峰顶 3R 回撤 50%
            # =1.5R，出场掉到平半价 2R 以下，等于把落袋的又吐回去。
            if half_done:
                _base = rr_target
                _extra_peak = peak_r - _base
                _extra_cur = cur_r - _base
                if (half_dd_pct > 0 and _extra_peak > 0.05
                        and (peak_r - cur_r) >= _extra_peak * half_dd_pct):
                    exit_px, exit_reason, exit_i = c_, "半仓回撤50%", j
                    break
                if half_dd_r > 0 and (peak_r - cur_r) >= half_dd_r:
                    exit_px, exit_reason, exit_i = c_, "半仓回撤0.8R", j
                    break
                _ht_cur = (cur_r - half_base_r) if half_fresh else _extra_cur
                if half_tp_r > 0 and _ht_cur >= half_tp_r:
                    exit_px, exit_reason, exit_i = c_, "半仓再满2R", j
                    break

            # ④ 保本止损：浮盈达 breakeven_r 后，止损上移到成本价
            if breakeven_r > 0 and peak_r >= breakeven_r:
                cur_stop = entry if sign > 0 else entry
            # ③ 破均线 + 回撤达标 → 全平
            #    峰顶还没到 trail_pct_max_r 时（默认 2R），改用「回撤浮盈的 trail_pct」：
            #    峰顶 0.6R 回撤 50% → 在 +0.3R 走，永远留住一半，不会倒亏。
            if zl[j] is not None:
                reversed_ = (c_ < zl[j]) if want == "long" else (c_ > zl[j])
                if reversed_:
                    ma_break_cnt += 1
                    if half_done:
                        ma_break_cnt_half += 1
                else:
                    ma_break_cnt = 0
                    if half_done:
                        ma_break_cnt_half = 0
                # ── 半仓按「全新的一张票」跑：用半仓自己的峰顶和破线计数 ──
                # 平半那一刻半仓账本清零，之后完全独立：
                #   半仓峰顶 < 2R → 回到段A（5 根 + 回撤浮盈 50%）
                #   半仓峰顶 ≥ 2R → 段B（2 根 + 回撤 0.8R）
                # 不这样分开的话，平半时峰顶已经是 2R+，半仓永远走段B，
                # 等于半仓拿到一张「天生就在高位」的票，规则跟新开的票不一样。
                _fresh = half_fresh and half_done
                if _fresh:
                    pk = peak_r_half
                    cr = cur_r - half_base_r
                else:
                    pk = peak_r
                    cr = cur_r
                # 段A/段B 的分界只看 2R。0.05 护栏只管「门槛怎么算」，
                # 不能连带把根数也切到 2 根 —— 否则新开的票（峰顶还没起来）
                # 会一直显示 2 根，与「不足 2R 走 5 根」的规则不符。
                in_lt = pk < trail_pct_max_r
                use_pct = trail_pct > 0 and 0.05 < pk < trail_pct_max_r
                _lt_bars = ma_bars_lt if in_lt else ma_confirm_bars
                if guard_split:
                    need_bars = _lt_bars
                else:   # 旧口径：护栏连带切根数
                    need_bars = _lt_bars if use_pct else ma_confirm_bars
                if use_pct:
                    need_dd = pk * trail_pct
                    if trail_pct_cap > 0:
                        need_dd = min(need_dd, trail_pct_cap)
                else:
                    need_dd = trail_r
                _dd = pk - cr
                # 回撤严重超标时不等穿线根数（dd_force_mult=0 关闭）。
                # 动机：5 根确认要慢一根 15m K，遇到一根长阴线会出现
                # 「浮盈全吐光还倒亏，但还差一根不肯走」。
                _force = dd_force_mult > 0 and _dd >= need_dd * dd_force_mult
                _cnt = ma_break_cnt_half if _fresh else ma_break_cnt
                if ((_cnt >= need_bars or _force) and _dd >= need_dd):
                    exit_px, exit_reason, exit_i = c_, "均线破+回撤", j
                    break
            else:
                ma_break_cnt = 0
            # ⑤ 时间止损：持仓超 N 根还没到 1.5R 就走
            if time_stop_bars > 0 and (j - i) >= time_stop_bars and peak_r < rr_target:
                exit_px, exit_reason, exit_i = c_, "时间止损", j
                break
        if exit_px is None:
            exit_px, exit_reason, exit_i = float(ks[-1]["close"]), "数据结束", n - 1

        # ── 结算 ──
        if half_done:
            rem = (exit_px - entry) * sign * 0.5
            f1 = entry * fee_in * 0.5 + 0
            f2 = exit_px * (FEE_TAKER + slippage * 0.5) * 0.5
            net = half_pnl + rem - (f1 + f2) - (0 if maker_entry else entry * slippage * 0.5 * 0.5)
        else:
            gross = (exit_px - entry) * sign
            fees = entry * fee_in + exit_px * FEE_TAKER
            slip = exit_px * slippage * 0.5 + (0 if maker_entry else entry * slippage * 0.5)
            net = gross - fees - slip
        trades.append({
            "sym": sym, "side": want, "entry": entry, "exit": exit_px,
            "stop": stop, "risk": risk, "risk_pct": risk / entry * 100,
            "net": net, "r": net / risk, "why": exit_reason,
            "ts": ks[exit_i]["ts"], "bars": exit_i - i - 1, "atr_pct": cur_atr_pct,
            "adx": adx[i], "entry_i": i,
            "peak_r": peak_r,          # 持仓期间达到过的最大浮盈（R 倍数），用于统计到达率
            "half_done": half_done,
        })
        i = exit_i + 1
    return trades, missed


def stats(trades):
    if not trades:
        return {}
    n = len(trades)
    tot = sum(t["r"] for t in trades)
    wins = [t for t in trades if t["r"] > 0]
    gw = sum(t["r"] for t in wins)
    gl = abs(sum(t["r"] for t in trades if t["r"] <= 0))
    eq = pk = dd = 0.0
    for t in sorted(trades, key=lambda x: x["ts"]):
        eq += t["r"]
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    whys = {}
    for t in trades:
        whys[t["why"]] = whys.get(t["why"], 0) + 1
    return {"n": n, "win": len(wins) / n * 100, "total": tot, "avg": tot / n,
            "pf": (gw / gl if gl > 0 else 99), "dd": dd,
            "why": " ".join(f"{k}{v}" for k, v in sorted(whys.items(), key=lambda x: -x[1])),
            "med_risk_pct": statistics.median([t["risk_pct"] for t in trades]),
            "avg_bars": sum(t["bars"] for t in trades) / n}


# ══════════════════════════════════════════════════════════════
async def main():
    from backtest.s15_original import fetch_klines

    syms = sys.argv[1].split(",") if len(sys.argv) > 1 else [
        "CLANKERUSDT", "JCTUSDT", "ELSAUSDT", "AINUSDT", "AKEUSDT", "AIOUSDT",
        "IDOLUSDT", "UAIUSDT", "HUMAUSDT", "ONUSDT", "AGTUSDT", "MAGMAUSDT",
        "LYNUSDT", "ESPORTSUSDT"]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    interval = sys.argv[3] if len(sys.argv) > 3 else "15m"

    print(f"\n{'='*96}")
    print("S15 定稿版  |  HA + CE(ATR1×2.0) + ZLSMA50 + ADX>25 + ATR>2.5% + 挂单")
    print("出场并列: 止损(阶段低点20) / 1.5R全平 / 破均线+回撤0.6R全平")
    print(f"{'='*96}\n")

    for maker in (True, False):
        rows, missed = [], 0
        for s in syms:
            ks = await fetch_klines(s, interval, days)
            if len(ks) < 500:
                continue
            ind = build(ks)
            t, m = simulate(s, ks, ind, maker_entry=maker)
            rows.extend(t)
            missed += m
        r = stats(rows)
        if not r:
            print("无成交")
            continue
        mode = "挂单 maker 0.02%" if maker else "市价 taker 0.05%"
        miss_pct = missed / (r["n"] + missed) * 100 if (r["n"] + missed) else 0
        print(f"【{mode}】漏单 {miss_pct:.1f}%")
        print(f"  {r['n']} 笔  胜率 {r['win']:.1f}%  净 {r['total']:+.2f}R  "
              f"均 {r['avg']:+.3f}R  盈亏比 {r['pf']:.2f}  "
              f"回撤 {r['dd']:.1f}R  ≈ {r['total']*ONE_R:+,.0f}U")
        print(f"  出场: {r['why']}")
        print(f"  止损距离中位 {r['med_risk_pct']:.2f}%  平均持仓 {r['avg_bars']:.1f} 根"
              f"（≈{r['avg_bars']*15/60:.1f} 小时）\n")


if __name__ == "__main__":
    asyncio.run(main())
