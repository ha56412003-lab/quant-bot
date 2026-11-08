# -*- coding: utf-8 -*-
"""快速失败退出：进场后 N 小时内没走出利润，就提前平掉

证据（2026-11-08 当天）：
  4 笔止损的持仓时间只有 0.2 / 0.3 / 1.2 / 1.5 小时 —— 极快反向。
  现在它们都是硬止损离场，每笔亏满 −1R（EVAA −158U / BEAMX −160U / RSR −152U / ERA −156U）。
  既然进场后几根 K 线内就能看出「这股不对」，为什么非要等它跌到硬止损？

规则：
  进场后若 quick_bars 根 K 线内，peak（最高浮盈）一直没达到 quick_min_r R，
  就在第 quick_bars+1 根收盘时平仓（不等硬止损、不等镜像出场）。
  一旦 peak 曾达到 quick_min_r，这条闸永久失效，后面的逻辑照旧。

风险（必须一起看）：
  这会误杀「先磨后涨」的交易 —— 进场后横了几小时才启动的那种。
  所以矩阵里 bars 从 4 根(1h) 到 16 根(4h)、min_r 从 0.10 到 0.50 都要扫。

口径：与 s15_live_parity.simulate 逐行一致，唯一新增 quick_fail 一条。
      自证：quick_bars=0 时必须等于基准 744 笔 / +78.07R。

用法：python backtest/exit_quickfail_test.py
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


def simulate_quick(ks, ind, quick_bars=0, quick_min_r=0.0):
    """与 s15_live_parity.simulate 逐行一致，新增 quick_fail 提前平仓闸。"""
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
        entry = float(ks[i + 1]["open"])
        if entry <= 0:
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
        quick_ok = quick_bars <= 0        # 未启用时永久放过
        ep = er = ei = None
        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            # ① 硬止损（最高优先级，实时）
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
            # ② ★ 快速失败：一旦摸到 quick_min_r 就永久放过
            if not quick_ok and peak >= quick_min_r:
                quick_ok = True
            if not quick_ok and (j - i) >= quick_bars:
                ep, er, ei = c_, "quick_fail", j
                break
            # ③ 时间止损
            if not half_done and (j - i) >= MAXH:
                ep, er, ei = c_, "timeout", j
                break
            # ④ 2R 平半后 3R 全平
            if half_done and (cur - 2.0) >= 1.0:
                ep, er, ei = c_, "half_tp", j
                break
            # ⑤ 镜像出场
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

    # ── 自证：quick_bars=0 等于线上 ────────────────────
    print("=" * 100)
    print("自证：quick_bars=0 必须等于线上（744 笔 / +78.07R）")
    print("=" * 100)
    ref, mine = [], []
    for sym, ks in cache:
        got = simulate(ks, inds[sym], want_mirror=True, use_bbw=True)
        for t in got:
            t["sym"] = sym
        ref.extend(got)
        g2 = simulate_quick(ks, inds[sym], 0, 0.0)
        for t in g2:
            t["sym"] = sym
        mine.extend(g2)
    s_ref, s_mine = stats(ref), stats(mine)
    ok = s_ref["n"] == s_mine["n"] and abs(s_ref["r"] - s_mine["r"]) < 1e-6
    print(f"  线上 {s_ref['n']} 笔 {s_ref['r']:+.2f}R   本脚本 {s_mine['n']} 笔 "
          f"{s_mine['r']:+.2f}R   -> {'✅' if ok else '❌'}")
    if not ok:
        return 1

    # ── 矩阵 ──────────────────────────────────────
    BARS = [(4, "1h"), (8, "2h"), (12, "3h"), (16, "4h"), (24, "6h")]
    MINS = [0.10, 0.20, 0.30, 0.50]
    print()
    print("=" * 100)
    print("矩阵：进场后 N 根 K 线内 peak 没到 X R 就平")
    print(f"基准：{s_ref['n']} 笔 {s_ref['r']:+.1f}R 均笔 {s_ref['avg']:+.3f} "
          f"胜率 {s_ref['win']:.1f}% 回撤 {s_ref['mdd']:+.1f} 最差日 {worst_day(ref):+.2f}")
    print("=" * 100)
    print(f"{'窗口':<7}{'peak门槛':>9}{'笔数':>7}{'净R':>10}{'均笔R':>9}{'胜率':>8}"
          f"{'回撤':>9}{'最差日':>9}{'提前平':>8}")
    print("-" * 100)

    results = []
    for bars, blabel in BARS:
        for mn in MINS:
            tr = []
            per_sym = {}
            for sym, ks in cache:
                got = simulate_quick(ks, inds[sym], bars, mn)
                for t in got:
                    t["sym"] = sym
                tr.extend(got)
                per_sym[sym] = sum(t["r"] for t in got)
            st = stats(tr)
            nq = sum(1 for t in tr if t["reason"] == "quick_fail")
            results.append(dict(bars=bars, label=f"{blabel}/{mn:.2f}", thr=mn,
                                st=st, per_sym=per_sym, trades=tr, nq=nq))
            print(f"{blabel:<7}{mn:>9.2f}{st['n']:>7}{st['r']:>+10.1f}{st['avg']:>+9.3f}"
                  f"{st['win']:>7.1f}%{st['mdd']:>+9.1f}{worst_day(tr):>+9.2f}{nq:>8}")
        print("-" * 100)

    # ── 判据 ──────────────────────────────────────
    print()
    print("=" * 100)
    print("判据：①净R不降 ②均笔R升 ③回撤不恶化 ④20币里>=13正贡献")
    print("=" * 100)
    print(f"{'参数':<12}{'净R':>9}{'均笔R':>9}{'回撤':>8}{'币种':>7}{'①':>4}{'②':>4}{'③':>4}{'④':>4}")
    print("-" * 100)
    winners = []
    for r in results:
        st = r["st"]
        c1 = st["r"] >= s_ref["r"]
        c2 = st["avg"] > s_ref["avg"]
        c3 = st["mdd"] >= s_ref["mdd"]
        pos = sum(1 for v in r["per_sym"].values() if v > 0)
        c4 = pos >= 13
        good = c1 and c2 and c3 and c4
        print(f"{r['label']:<12}{st['r']:>+9.1f}{st['avg']:>+9.3f}{st['mdd']:>+8.1f}"
              f"{pos:>4}/20{'✅' if c1 else '❌':>4}{'✅' if c2 else '❌':>4}"
              f"{'✅' if c3 else '❌':>4}{'✅' if c4 else '❌':>4}"
              f"{'   ★' if good else ''}")
        if good:
            winners.append(r)

    print()
    if winners:
        print(f"★ {len(winners)} 组四条全过：")
        for w in sorted(winners, key=lambda x: -x["st"]["avg"]):
            st = w["st"]
            print(f"   {w['label']}  {st['n']} 笔 净R {st['r']:+.1f} 均笔 {st['avg']:+.3f} "
                  f"胜率 {st['win']:.1f}% 回撤 {st['mdd']:+.1f} 提前平 {w['nq']} 笔")
    else:
        print("✗ 没有一组四条全过。")

    # ── 防过拟合 ───────────────────────────────────
    print()
    print("=" * 100)
    print("防过拟合：前后两半")
    print("=" * 100)
    ts = sorted(t["entry_ts"] for t in ref)
    mid = ts[len(ts) // 2]
    b1 = sum(t["r"] for t in ref if t["entry_ts"] < mid)
    b2 = sum(t["r"] for t in ref if t["entry_ts"] >= mid)
    print(f"{'参数':<12}{'前半':>11}{'后半':>11}   两段都更好？")
    print("-" * 100)
    print(f"{'基准':<12}{b1:>+11.1f}{b2:>+11.1f}   —")
    for r in (winners if winners else results[:6]):
        a = sum(t["r"] for t in r["trades"] if t["entry_ts"] < mid)
        b = sum(t["r"] for t in r["trades"] if t["entry_ts"] >= mid)
        note = "✅ 是" if (a > b1 and b > b2) else "❌ 否"
        print(f"{r['label']:<12}{a:>+11.1f}{b:>+11.1f}   {note}")

    # ── 提前平的分布（看清代价） ────────────────────────
    print()
    print("=" * 100)
    print("提前平掉的那些交易，本来会赚还是会亏？（取最好的一组看）")
    print("=" * 100)
    pick = winners[0] if winners else results[0]
    tr = pick["trades"]
    qf = [t for t in tr if t["reason"] == "quick_fail"]
    print(f"  参数 {pick['label']}：提前平 {len(qf)} 笔，合计 {sum(t['r'] for t in qf):+.1f}R，"
          f"均笔 {sum(t['r'] for t in qf) / len(qf):+.3f}R" if qf else "  没有提前平的")
    if qf:
        pos = [t for t in qf if t["r"] > 0]
        neg = [t for t in qf if t["r"] <= 0]
        print(f"    其中赚钱的 {len(pos)} 笔（{sum(t['r'] for t in pos):+.1f}R）"
              f" —— 这些是被误杀的")
        print(f"    亏钱的   {len(neg)} 笔（{sum(t['r'] for t in neg):+.1f}R）"
              f" —— 这些是省下的")
        print(f"    净效果：{sum(t['r'] for t in qf):+.2f}R")
    return 0


if __name__ == "__main__":
    sys.exit(main())
