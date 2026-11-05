"""S15 严格按浩哥口述规则实现（多空对称）

═══════════════════════════════════════════════════════════════
【多单】
进场（三个条件同时成立）：
  ① CE 发出买入信号（方向由空翻多，事件不是状态）
  ② 信号发出当根，收盘价在 ZLSMA50 上方（过滤逆势信号）
  ③ 关键 K 线：收盘价在均线之上的阳线
出场：
  ① 止损：关键 K 线低点下方 / 阶段性低点下方（20 根内最低）
  ② 止盈两段：到 1.5R 平一半；剩一半等收盘价下穿 ZLSMA50
【空单】镜像
═══════════════════════════════════════════════════════════════
关键技术约定：
  · 信号、ZLSMA、CE、关键K线判定 → 全在 Heikin Ashi 序列上算
  · 止损价 → 用【原始 K 线的真实 high/low】（HA 的 high/low 是合成值，
    实盘不可能在合成价成交，用 HA 会虚高）
  · CE 参数：ATR 周期 1、乘积 2.0（浩哥 TradingView 实际设置）
  · 手续费 taker 0.05% 双边 + 滑点 0.03%
"""
import asyncio
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FEE = 0.0005      # taker 单边
SLIP = 0.0003     # 单边滑点估算
ONE_R = 50.0      # 1R = 净值 10000 × 0.5%


# ══════════════════════════════════════════════════════════════
#  指标（全部在 HA 序列上）
# ══════════════════════════════════════════════════════════════
def heikin_ashi(ks):
    ho, hh, hl, hc = [], [], [], []
    for i, k in enumerate(ks):
        o, h, l, c = float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"])
        hc_ = (o + h + l + c) / 4.0
        if i == 0:
            ho.append((o + c) / 2.0)
        else:
            ho.append((ho[i - 1] + hc[i - 1]) / 2.0)
        ho_, hc_ = ho[i], hc_
        hh.append(max(h, ho_, hc_))
        hl.append(min(l, ho_, hc_))
        hc.append(hc_)
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
            acc = v * length
            cnt = 1
            if i >= length - 1:
                acc = sum(x for x in src[i - length + 1:i + 1] if x is not None)
                out[i] = acc / length
            continue
        acc = acc + v - acc / length
        out[i] = acc / length
    return out


def chandelier_dir(ho, hh, hl, hc, atr_len=1, mult=2.0):
    """Chandelier Exit 方向。返回 (dir, atr) —— dir: 1 多 / -1 空。"""
    n = len(hc)
    tr = [None] * n
    for i in range(n):
        if i == 0:
            tr[i] = hh[i] - hl[i]
        else:
            tr[i] = max(hh[i] - hl[i], abs(hh[i] - hc[i - 1]), abs(hl[i] - hc[i - 1]))
    atr = _rma(tr, atr_len)
    ls_raw, ss_raw = [None] * n, [None] * n
    ls, ss = [None] * n, [None] * n
    direc = [None] * n
    for i in range(n):
        if atr[i] is None:
            continue
        ls_raw[i] = hh[i] - atr[i] * mult
        ss_raw[i] = hl[i] + atr[i] * mult
        if i == 0 or ls[i - 1] is None:
            ls[i], ss[i], direc[i] = ls_raw[i], ss_raw[i], 1
            continue
        ls[i] = max(ls_raw[i], ls[i - 1]) if hc[i - 1] > ls[i - 1] else ls_raw[i]
        ss[i] = min(ss_raw[i], ss[i - 1]) if hc[i - 1] < ss[i - 1] else ss_raw[i]
        prev = direc[i - 1]
        if prev == 1:
            direc[i] = -1 if hc[i] < ls[i] else 1
        else:
            direc[i] = 1 if hc[i] > ss[i] else -1
    return direc, atr


# ══════════════════════════════════════════════════════════════
#  模拟
# ══════════════════════════════════════════════════════════════
def simulate(sym, ks, ind, swing_lookback=20, stop_mode="far",
             rr_half=1.5, half_exit=True, same_bar_only=True,
             signal_max_wait=0, min_atr_pct=0.0, min_exit_r=0.0,
             stop_atr_mult=None, trail_r=0.0, trail_all=False):
    """严格按浩哥规则模拟单币。

    stop_mode: 'far' 取更远的（关键K线低点 vs 阶段低点中更低者）
               'near' 取更近的（更高者）
    rr_half:   多少 R 平一半
    half_exit: 是否分批止盈（False = 全仓等均线穿越）
    same_bar_only: True = CE信号与关键K线必须同一根
    signal_max_wait: 若不同一根，CE 信号最多等几根 K 线找关键K线
    """
    ho, hh, hl, hc = ind["ho"], ind["hh"], ind["hl"], ind["hc"]
    zl, dirs = ind["zlsma"], ind["dir"]
    n = len(hc)

    trades = []
    i = max(1, 60)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None:
            i += 1
            continue

        # ── 找信号 ──
        crossed_long = dirs[i] == 1 and dirs[i - 1] == -1
        crossed_short = dirs[i] == -1 and dirs[i - 1] == 1

        sig_long = crossed_long and hc[i] > zl[i]
        sig_short = crossed_short and hc[i] < zl[i]
        if not sig_long and not sig_short:
            i += 1
            continue

        want = "long" if sig_long else "short"

        # ── 找关键 K 线 ──
        k = i
        if not same_bar_only and signal_max_wait > 0:
            found = False
            for j in range(i, min(i + signal_max_wait + 1, n - 2)):
                if want == "long" and hc[j] > ho[j] and hc[j] > zl[j]:
                    k, found = j, True
                    break
                if want == "short" and hc[j] < ho[j] and hc[j] < zl[j]:
                    k, found = j, True
                    break
            if not found:
                i += 1
                continue
        else:
            key_ok = (hc[k] > ho[k] and hc[k] > zl[k]) if want == "long" \
                else (hc[k] < ho[k] and hc[k] < zl[k])
            if not key_ok:
                i += 1
                continue

        # ── 先决条件：ATR% 门槛（进场那根）──
        cur_atr_pct = (ind["atr"][k] / hc[k] * 100) if (ind["atr"][k] and hc[k]) else 0.0
        if cur_atr_pct < min_atr_pct:
            i += 1
            continue

        entry = float(ks[k + 1]["open"])
        if entry <= 0:
            i += 1
            continue

        # ── 止损：用【原始 K 线】的真实 low/high ──
        bar_low = float(ks[k]["low"])
        bar_high = float(ks[k]["high"])
        lo_start = max(0, k - swing_lookback + 1)
        swing_low = min(float(ks[j]["low"]) for j in range(lo_start, k + 1))
        swing_high = max(float(ks[j]["high"]) for j in range(lo_start, k + 1))

        if stop_atr_mult:                      # N×ATR 止损（昨天线上的做法）
            _atr = ind["atr"][k]
            if not _atr or _atr <= 0:
                i += 1
                continue
            if want == "long":
                stop = entry - stop_atr_mult * _atr
                risk = entry - stop
            else:
                stop = entry + stop_atr_mult * _atr
                risk = stop - entry
        elif want == "long":
            if stop_mode == "far":
                stop = min(bar_low, swing_low)
            else:
                stop = max(bar_low, swing_low)
            stop = stop * 0.999          # 略微跌破
            risk = entry - stop
        else:
            if stop_mode == "far":
                stop = max(bar_high, swing_high)
            else:
                stop = min(bar_high, swing_high)
            stop = stop * 1.001
            risk = stop - entry

        if risk <= 0 or risk / entry > 0.30:   # 止损过远，放弃这笔
            i += 1
            continue

        half_hit = False
        half_pnl = 0.0
        half_fee = 0.0
        exit_px = None
        exit_reason = None
        exit_i = None
        peak_r = 0.0       # 持仓期间最大浮盈（R）
        broke_ma = False   # 是否已经破过均线

        for j in range(k + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            if want == "long":
                if l_ <= stop:
                    exit_px = min(o_, stop)
                    exit_reason = "止损"
                    exit_i = j
                    break
                peak_r = max(peak_r, (h_ - entry) / risk)   # 用最高价记峰值
                if half_exit and not half_hit:
                    tgt = entry + rr_half * risk
                    if h_ >= tgt:
                        half_hit = True
                        half_pnl = (tgt - entry) * 0.5
                        half_fee = (entry * FEE * 0.5) + (tgt * FEE * 0.5) + tgt * SLIP * 0.25
                if zl[j] is not None and c_ < zl[j]:
                    broke_ma = True
                    cur_r = (c_ - entry) / risk
                    need_trail = trail_r > 0 and (half_hit or trail_all)
                    if need_trail:
                        if (peak_r - cur_r) >= trail_r:
                            exit_px = c_
                            exit_reason = "均线破+回撤确认"
                            exit_i = j
                            break
                        # 破均线但回撤不够 → 继续持有，等回撤扩大
                    else:
                        exit_px = c_
                        exit_reason = ("均线止盈" if half_hit else "均线出场")
                        exit_i = j
                        break
            else:
                if h_ >= stop:
                    exit_px = max(o_, stop)
                    exit_reason = "止损"
                    exit_i = j
                    break
                peak_r = max(peak_r, (entry - l_) / risk)
                if half_exit and not half_hit:
                    tgt = entry - rr_half * risk
                    if l_ <= tgt:
                        half_hit = True
                        half_pnl = (entry - tgt) * 0.5
                        half_fee = (entry * FEE * 0.5) + (tgt * FEE * 0.5) + tgt * SLIP * 0.25
                if zl[j] is not None and c_ > zl[j]:
                    broke_ma = True
                    cur_r = (entry - c_) / risk
                    need_trail = trail_r > 0 and (half_hit or trail_all)
                    if need_trail:
                        if (peak_r - cur_r) >= trail_r:
                            exit_px = c_
                            exit_reason = "均线破+回撤确认"
                            exit_i = j
                            break
                    else:
                        exit_px = c_
                        exit_reason = ("均线止盈" if half_hit else "均线出场")
                        exit_i = j
                        break
        if exit_px is None:
            exit_px = float(ks[-1]["close"])
            exit_reason = "数据结束"
            exit_i = n - 1

        # ── 结算 ──
        if half_hit:
            # 一半在 rr_half R 平掉，剩一半在 exit_px 平掉
            rem_frac = 0.5
            sign = 1 if want == "long" else -1
            rem_pnl = (exit_px - entry) * sign * rem_frac
            rem_fee = (exit_px * FEE * rem_frac) + (exit_px * SLIP * 0.5 * rem_frac)
            net = half_pnl + rem_pnl - (half_fee + rem_fee)
        else:
            sign = 1 if want == "long" else -1
            gross = (exit_px - entry) * sign
            fees = entry * FEE + exit_px * FEE
            slip = (entry + exit_px) * SLIP * 0.5
            net = gross - fees - slip

        r = net / risk
        trades.append({
            "sym": sym, "side": want, "entry": entry, "exit": exit_px,
            "stop": stop, "risk": risk, "risk_pct": risk / entry * 100,
            "net": net, "r": r, "why": exit_reason, "half": half_hit,
            "ts": ks[exit_i]["ts"], "bars": exit_i - k - 1,
            "atr_pct": cur_atr_pct,
        })
        i = exit_i + 1

    return trades


def stats(trades, label):
    if not trades:
        return {"label": label, "n": 0, "win": 0, "total_r": 0, "avg_r": 0,
                "pf": 0, "dd": 0, "why": "", "fee_r": 0}
    n = len(trades)
    wins = [t for t in trades if t["r"] > 0]
    win = len(wins) / n * 100
    total = sum(t["r"] for t in trades)
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
    why_str = " ".join(f"{k}{v}" for k, v in sorted(whys.items(), key=lambda x: -x[1]))
    return {"label": label, "n": n, "win": win, "total_r": total,
            "avg_r": total / n, "pf": (gw / gl if gl > 0 else 99),
            "dd": dd, "why": why_str, "fee_r": sum(t["net"] for t in trades)}


def build(ks, ce_len=1, ce_mult=2.0, z_len=50):
    ho, hh, hl, hc = heikin_ashi(ks)
    zl = zlsma(hc, z_len)
    dirs, atr = chandelier_dir(ho, hh, hl, hc, ce_len, ce_mult)
    return {"ho": ho, "hh": hh, "hl": hl, "hc": hc, "zlsma": zl,
            "dir": dirs, "atr": atr}


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════
async def main():
    from backtest.s15_original import fetch_klines

    syms = sys.argv[1].split(",") if len(sys.argv) > 1 else [
        "CLANKERUSDT", "JCTUSDT", "ELSAUSDT", "AINUSDT", "AKEUSDT", "AIOUSDT",
        "IDOLUSDT", "UAIUSDT", "HUMAUSDT", "ONUSDT", "AGTUSDT", "MAGMAUSDT",
        "LYNUSDT", "ESPORTSUSDT"]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    interval = sys.argv[3] if len(sys.argv) > 3 else "15m"

    print(f"\n{'='*90}")
    print(f"S15 严格按浩哥规则  |  {interval}  {days}天  {len(syms)}币")
    print("规则: HA图表 + CE(ATR1×2.0) + ZLSMA50 + 信号须在均线同侧 + 关键K线")
    print("出场: 止损(关键K线/阶段低点) + 1.5R平一半 + 剩余收盘穿均线")
    print(f"{'='*90}\n")

    variants = [
        ("止损取远 + 分批止盈", dict(stop_mode="far", half_exit=True)),
        ("止损取近 + 分批止盈", dict(stop_mode="near", half_exit=True)),
        ("止损取远 + 全仓均线出", dict(stop_mode="far", half_exit=False)),
        ("止损取近 + 全仓均线出", dict(stop_mode="near", half_exit=False)),
    ]

    for vname, vkw in variants:
        all_tr = []
        for s in syms:
            ks = await fetch_klines(s, interval, days)
            if len(ks) < 500:
                continue
            ind = build(ks)
            all_tr.extend(simulate(s, ks, ind, **vkw))
        r = stats(all_tr, vname)
        if not r["n"]:
            print(f"{vname}: 无成交")
            continue
        print(f"\n【{vname}】")
        print(f"  {r['n']} 笔  胜率 {r['win']:.1f}%  净 {r['total_r']:+.2f}R  "
              f"均 {r['avg_r']:+.3f}R  盈亏比 {r['pf']:.2f}  "
              f"回撤 {r['dd']:.1f}R  ≈ {r['total_r']*ONE_R:+,.0f}U")
        print(f"  出场分布: {r['why']}")


if __name__ == "__main__":
    asyncio.run(main())
