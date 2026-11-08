# -*- coding: utf-8 -*-
"""两条闸叠加：进场「偏离>X%」+ 出场「4h 没起色就砍」

单条各自的结果（60 天，基准 744 笔 +78.07R）：
  偏离 >0.5%              658 笔  +94.9R  均笔 +0.144  回撤 −18.7  15/20
  快速失败 4h/0.50        782 笔 +109.0R  均笔 +0.139  回撤 −21.4  14/20

问题：两条一起上会不会更强？还是互相抵消？
（偏离闸让进场点更少、质量更高；快速失败闸给每笔一个观察期。
  理论上互补 —— 但必须实测，不能靠推理。）

自证：两闸都不开时 == 线上 744 笔 / +78.07R。
"""
import collections
import datetime
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma                      # noqa: E402
from backtest.s15_live_parity import simulate, stats           # noqa: E402

FEE = 0.0005
SWING = 20
BBW_MIN = 70.0
MAXH = 64
MAX_RISK_PCT = 30.0


def simulate_combo(ks, ind, min_dev=0.0, quick_bars=0, quick_min_r=0.0,
                   use_next_open=True):
    """use_next_open=True  -> 进场价 = 下一根开盘（backtest/s15_live_parity 母本口径）
       use_next_open=False -> 进场价 = 本根收盘（backend/strategies.py:492 真实线上口径）"""
    ho, hc = ind["open"], ind["close"]
    zl, dirs, atr, bp = ind["zlsma"], ind["dir"], ind["atr"], ind["bbw_pct"]
    hi_raw, lo_raw = ind["high_raw"], ind["low_raw"]
    n = len(hc)
    out = []
    i = max(1, 60, SWING)
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
        if bp[i] is None or bp[i] < BBW_MIN:
            i += 1
            continue
        entry = float(ks[i + 1]["open"] if use_next_open else ks[i]["close"])
        if entry <= 0:
            i += 1
            continue
        # ★ 闸一：进场偏离
        if min_dev > 0:
            if abs(entry - zl[i]) / zl[i] * 100 <= min_dev:
                i += 1
                continue
        lo_i = max(0, i - SWING + 1)
        s_lo = min(lo_raw[j] for j in range(lo_i, i + 1))
        s_hi = max(hi_raw[j] for j in range(lo_i, i + 1))
        stop = s_lo if want == "long" else s_hi
        risk = (entry - stop) if want == "long" else (stop - entry)
        if risk <= 0 or risk / entry * 100 > MAX_RISK_PCT:
            i += 1
            continue

        sign = 1 if want == "long" else -1
        peak = 0.0
        half_done = False
        quick_ok = quick_bars <= 0
        ep = er = ei = None
        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            if want == "long" and l_ <= stop:
                ep, er, ei = min(o_, stop), "hard_stop", j
                break
            if want == "short" and h_ >= stop:
                ep, er, ei = max(o_, stop), "hard_stop", j
                break
            peak = max(peak, ((h_ - entry) if want == "long" else (entry - l_)) / risk)
            cur = ((c_ - entry) if want == "long" else (entry - c_)) / risk
            if peak >= 2.0 and not half_done:
                half_done = True
            # ★ 闸二：快速失败
            if not quick_ok and peak >= quick_min_r:
                quick_ok = True
            if not quick_ok and (j - i) >= quick_bars:
                ep, er, ei = c_, "quick_fail", j
                break
            if not half_done and (j - i) >= MAXH:
                ep, er, ei = c_, "timeout", j
                break
            if half_done and (cur - 2.0) >= 1.0:
                ep, er, ei = c_, "half_tp", j
                break
            flip = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                else (dirs[j] == 1 and dirs[j - 1] == -1)
            ha_ok = (hc[j] < ho[j] and hc[j] < zl[j]) if want == "long" \
                else (hc[j] > ho[j] and hc[j] > zl[j])
            ok_bp = bp[j] is not None and bp[j] >= BBW_MIN
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
            r = 0.5 * 2.0 + 0.5 * r
        out.append(dict(sym=None, r=r, bars=ei - i, reason=er,
                        entry_ts=int(ks[i + 1].get("ts", i)),
                        exit_ts=int(ks[ei].get("ts", ei))))
        i = ei + 1
    return out


def worst_day(trades):
    d = collections.defaultdict(float)
    for t in trades:
        d[datetime.datetime.fromtimestamp(t["exit_ts"]).strftime("%m-%d")] += t["r"]
    return min(d.values()) if d else 0.0


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    cache = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) >= 800:
            cache.append((sym, ks))
    print(f"载入 {len(cache)} 币\n")
    inds = {sym: _ce_zlsma(ks, 1, 2.0, 50, True, True) for sym, ks in cache}

    # 自证
    print("=" * 100)
    print("自证：两闸都不开 == 线上 744 笔 / +78.07R")
    print("=" * 100)
    ref, mine = [], []
    for sym, ks in cache:
        got = simulate(ks, inds[sym], want_mirror=True, use_bbw=True)
        for t in got:
            t["sym"] = sym
        ref.extend(got)
        g2 = simulate_combo(ks, inds[sym], 0, 0, 0)
        for t in g2:
            t["sym"] = sym
        mine.extend(g2)
    s_ref, s_mine = stats(ref), stats(mine)
    ok = s_ref["n"] == s_mine["n"] and abs(s_ref["r"] - s_mine["r"]) < 1e-6
    print(f"  线上 {s_ref['n']} 笔 {s_ref['r']:+.2f}R   本脚本 {s_mine['n']} 笔 "
          f"{s_mine['r']:+.2f}R   -> {'✅' if ok else '❌'}")
    if not ok:
        return 1

    GROUPS = [
        ("基准（现状）",              0.0, 0,  0.0),
        ("只加偏离>0.5%",             0.5, 0,  0.0),
        ("只加快速失败 4h/0.50",      0.0, 16, 0.50),
        ("只加快速失败 4h/0.30",      0.0, 16, 0.30),
        ("★ 偏离>0.5% + 4h/0.50",    0.5, 16, 0.50),
        ("★ 偏离>0.5% + 4h/0.30",    0.5, 16, 0.30),
        ("★ 偏离>1.0% + 4h/0.50",    1.0, 16, 0.50),
        ("偏离>0.5% + 6h/0.50",      0.5, 24, 0.50),
    ]

    print()
    print("=" * 100)
    print("叠加对比")
    print("=" * 100)
    print(f"{'方案':<24}{'笔数':>7}{'净R':>10}{'均笔R':>9}{'胜率':>8}"
          f"{'回撤':>9}{'最差日':>9}{'币种':>8}")
    print("-" * 100)
    res = {}
    for name, dev, bars, mn in GROUPS:
        tr = []
        per_sym = {}
        for sym, ks in cache:
            got = simulate_combo(ks, inds[sym], dev, bars, mn)
            for t in got:
                t["sym"] = sym
            tr.extend(got)
            per_sym[sym] = sum(t["r"] for t in got)
        st = stats(tr)
        pos = sum(1 for v in per_sym.values() if v > 0)
        res[name] = dict(st=st, trades=tr, per_sym=per_sym, pos=pos)
        print(f"{name:<24}{st['n']:>7}{st['r']:>+10.1f}{st['avg']:>+9.3f}"
              f"{st['win']:>7.1f}%{st['mdd']:>+9.1f}{worst_day(tr):>+9.2f}{pos:>5}/20")

    print()
    print("=" * 100)
    print("★ 口径对比：进场价用「下一根开盘」还是「本根收盘」")
    print("   回测母本 s15_live_parity 用下一根开盘；真实线上 strategies.py:492 用本根收盘")
    print("=" * 100)
    print(f"{'方案':<24}{'口径':<12}{'笔数':>7}{'净R':>10}{'均笔R':>9}{'胜率':>8}{'回撤':>9}")
    print("-" * 100)
    for name, dev, bars, mn in [("基准（现状）", 0.0, 0, 0.0),
                                ("★ 两条全上", 0.5, 16, 0.50)]:
        for nxt in (True, False):
            tr = []
            for sym, ks in cache:
                got = simulate_combo(ks, inds[sym], dev, bars, mn, use_next_open=nxt)
                for t in got:
                    t["sym"] = sym
                tr.extend(got)
            st = stats(tr)
            lab = "下一根开盘" if nxt else "本根收盘(线上)"
            print(f"{name:<24}{lab:<12}{st['n']:>7}{st['r']:>+10.1f}{st['avg']:>+9.3f}"
                  f"{st['win']:>7.1f}%{st['mdd']:>+9.1f}")
        print("-" * 100)

    print()
    print("=" * 100)
    print("防过拟合：前后两半")
    print("=" * 100)
    ts = sorted(t["entry_ts"] for t in ref)
    mid = ts[len(ts) // 2]
    b1 = sum(t["r"] for t in ref if t["entry_ts"] < mid)
    b2 = sum(t["r"] for t in ref if t["entry_ts"] >= mid)
    print(f"{'方案':<24}{'前半':>11}{'后半':>11}   两段都更好？")
    print("-" * 100)
    print(f"{'基准':<24}{b1:>+11.1f}{b2:>+11.1f}   —")
    for name, d in res.items():
        if name.startswith("基准"):
            continue
        a = sum(t["r"] for t in d["trades"] if t["entry_ts"] < mid)
        b = sum(t["r"] for t in d["trades"] if t["entry_ts"] >= mid)
        note = "✅ 是" if (a > b1 and b > b2) else "❌ 否"
        print(f"{name:<24}{a:>+11.1f}{b:>+11.1f}   {note}")

    print()
    print("=" * 100)
    print("最优方案的「提前平」明细（看清代价）")
    print("=" * 100)
    best = "★ 偏离>1.0% + 4h/0.50"
    tr = res[best]["trades"]
    qf = [t for t in tr if t["reason"] == "quick_fail"]
    if qf:
        pos = [t for t in qf if t["r"] > 0]
        neg = [t for t in qf if t["r"] <= 0]
        print(f"  {best}：提前平 {len(qf)} 笔，合计 {sum(t['r'] for t in qf):+.1f}R")
        print(f"    本来是赚的 {len(pos)} 笔（{sum(t['r'] for t in pos):+.1f}R）← 误杀")
        print(f"    本来是亏的 {len(neg)} 笔（{sum(t['r'] for t in neg):+.1f}R）← 省下")
    reasons = collections.Counter(t["reason"] for t in tr)
    print(f"  出场原因分布：{dict(reasons)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
