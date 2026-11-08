# -*- coding: utf-8 -*-
"""进场位置：进场价离 ZLSMA 太远（追高/追低）就不开仓

背景（2026-11-08）：
  今天 4 笔止损的持仓只有 0.2 / 0.3 / 1.2 / 1.5 小时就反向。
  不是「市场没趋势」（其中 EVAA 进场时 4h EFF 高达 0.617），
  而是「进场价本身就落在短期极值上」——三闸全是事件触发
  （CE 翻转 / HA 变色 / BBW>70），完全不看价格离均线多远。
  CE 翻转那一刻，价格往往已经跑了一大段，正好在短期顶/底接飞刀。

测什么：
  dev = |entry - zl[i]| / zl[i] * 100     进场价偏离信号 K 线 ZLSMA 的百分比
  dev 超过上限就不开仓。上限越大越宽松，0 = 不过滤（线上现状）。

双重口径自证（缺一不可）：
  ① gate=None               必须与 s15_live_parity.simulate 完全一致（744 笔 / +78.07R）
  ② 用 EFF gate 跑 4h/>=0.45 必须复现 entry_eff_test.py 的结果（165 笔 / +40.4R）
     这证明本通用框架与那个专用脚本等价，不是另写一套逻辑。

用法：python backtest/entry_pos_test.py
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma                      # noqa: E402
from backtest.s15_live_parity import simulate, stats           # noqa: E402
from backtest.entry_eff_test import simulate_eff, eff_series    # noqa: E402

FEE = 0.0005
SWING = 20
BBW_MIN = 70.0
MAXH = 64
MAX_RISK_PCT = 30.0


def simulate_gated(ks, ind, gate=None):
    """与 s15_live_parity.simulate 逐行一致，进场闸门改成可插拔的 gate。

    gate(ks, ind, i, want) -> True 表示「放过」，False 表示「挡住」。
    gate=None 时完全不拦，等价线上现状。
    """
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
        if gate is not None and not gate(ks, ind, i, want):
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


def dev_pct(ks, ind, i):
    """进场价（下一根开盘）偏离信号 K 线 ZLSMA 的百分比"""
    zl = ind["zlsma"][i]
    if not zl or zl <= 0:
        return None
    entry = float(ks[i + 1]["open"])
    return abs(entry - zl) / zl * 100


def make_dev_gate(cap):
    """偏离超过 cap% 就挡住"""
    def g(ks, ind, i, want):
        d = dev_pct(ks, ind, i)
        return d is not None and d <= cap
    return g


def make_dev_floor(floor):
    """偏离必须大于 floor% 才放过（反向过滤：贴均线的假信号挡掉）"""
    def g(ks, ind, i, want):
        d = dev_pct(ks, ind, i)
        return d is not None and d > floor
    return g


def make_eff_gate(eff_arr, thr):
    """给口径自证②用：复现 EFF 过滤"""
    def g(ks, ind, i, want):
        e = eff_arr[i]
        return e is not None and e >= thr
    return g


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

    # ── 自证 ①：不设闸门 == 线上脚本 ──────────────────────
    print("=" * 100)
    print("自证 ①  gate=None 必须等于 s15_live_parity（744 笔 / +78.07R）")
    print("=" * 100)
    ref, mine = [], []
    for sym, ks in cache:
        got = simulate(ks, inds[sym], want_mirror=True, use_bbw=True)
        for t in got:
            t["sym"] = sym
        ref.extend(got)
        got2 = simulate_gated(ks, inds[sym], None)
        for t in got2:
            t["sym"] = sym
        mine.extend(got2)
    s_ref, s_mine = stats(ref), stats(mine)
    ok1 = s_ref["n"] == s_mine["n"] and abs(s_ref["r"] - s_mine["r"]) < 1e-6
    print(f"  线上脚本  {s_ref['n']:>4} 笔  {s_ref['r']:>+8.2f}R")
    print(f"  通用框架  {s_mine['n']:>4} 笔  {s_mine['r']:>+8.2f}R   -> {'✅' if ok1 else '❌'}")
    if not ok1:
        return 1

    # ── 自证 ②：通用框架跑 EFF gate == entry_eff_test 的结果 ──
    print()
    print("=" * 100)
    print("自证 ②  用 EFF gate 跑 4h/>=0.45，必须复现 entry_eff_test 的 165 笔 / +40.4R")
    print("=" * 100)
    t2 = []
    for sym, ks in cache:
        g = make_eff_gate(eff_series(ks, 16), 0.45)
        got = simulate_gated(ks, inds[sym], g)
        for t in got:
            t["sym"] = sym
        t2.extend(got)
    s2 = stats(t2)
    ok2 = s2["n"] == 165 and abs(s2["r"] - 40.4) < 0.5
    print(f"  期望 165 笔 / +40.4R   实际 {s2['n']} 笔 / {s2['r']:>+.2f}R   -> {'✅' if ok2 else '❌'}")
    if not ok2:
        print("  两份实现对不上，后面的数字不能用，退出。")
        return 1

    # ── 正题：偏离上限矩阵 ────────────────────────────
    CAPS = [0.3, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0]
    print()
    print("=" * 100)
    print("正题：进场价偏离 ZLSMA 超过 X% 就不开仓")
    print(f"基准（不过滤）：{s_ref['n']} 笔  {s_ref['r']:+.1f}R  均笔 {s_ref['avg']:+.3f}  "
          f"胜率 {s_ref['win']:.1f}%  回撤 {s_ref['mdd']:+.1f}")
    print("=" * 100)
    print(f"{'偏离上限':>9}{'笔数':>7}{'过滤掉':>8}{'净R':>10}{'均笔R':>9}{'胜率':>8}{'回撤':>9}{'最差日':>9}")
    print("-" * 100)

    import datetime
    import collections

    def worst_day(trades):
        d = collections.defaultdict(float)
        for t in trades:
            d[datetime.datetime.fromtimestamp(t["exit_ts"]).strftime("%m-%d")] += t["r"]
        return min(d.values()) if d else 0.0

    results = []
    for cap in CAPS:
        tr = []
        per_sym = {}
        for sym, ks in cache:
            got = simulate_gated(ks, inds[sym], make_dev_gate(cap))
            for t in got:
                t["sym"] = sym
            tr.extend(got)
            per_sym[sym] = sum(t["r"] for t in got)
        st = stats(tr)
        results.append(dict(cap=cap, st=st, per_sym=per_sym, trades=tr))
        print(f"{cap:>8.1f}%{st['n']:>7}{s_ref['n'] - st['n']:>8}"
              f"{st['r']:>+10.1f}{st['avg']:>+9.3f}{st['win']:>7.1f}%"
              f"{st['mdd']:>+9.1f}{worst_day(tr):>+9.2f}")
    print(f"{'不过滤':>9}{s_ref['n']:>7}{0:>8}{s_ref['r']:>+10.1f}{s_ref['avg']:>+9.3f}"
          f"{s_ref['win']:>7.1f}%{s_ref['mdd']:>+9.1f}{worst_day(ref):>+9.2f}")

    # ── 判据 ────────────────────────────────────────
    print()
    print("=" * 100)
    print("判据：①净R不降 ②均笔R升 ③回撤不恶化 ④20币里>=13正贡献")
    print("=" * 100)
    print(f"{'偏离上限':>9}{'净R':>9}{'均笔R':>9}{'回撤':>8}{'币种':>7}{'①':>4}{'②':>4}{'③':>4}{'④':>4}")
    print("-" * 100)
    winners = []
    for r in results:
        st = r["st"]
        c1 = st["r"] >= s_ref["r"]
        c2 = st["avg"] > s_ref["avg"]
        c3 = st["mdd"] >= s_ref["mdd"]
        pos = sum(1 for v in r["per_sym"].values() if v > 0)
        c4 = pos >= 13
        ok = c1 and c2 and c3 and c4
        print(f"{r['cap']:>8.1f}%{st['r']:>+9.1f}{st['avg']:>+9.3f}{st['mdd']:>+8.1f}"
              f"{pos:>4}/20{'✅' if c1 else '❌':>4}{'✅' if c2 else '❌':>4}"
              f"{'✅' if c3 else '❌':>4}{'✅' if c4 else '❌':>4}"
              f"{'   ★四条全过' if ok else ''}")
        if ok:
            winners.append(r)

    # 前后两半的基准值（下面上限/下限两段的防过拟合都要用）
    ts = sorted(t["entry_ts"] for t in ref)
    mid = ts[len(ts) // 2]
    b1 = sum(t["r"] for t in ref if t["entry_ts"] < mid)
    b2 = sum(t["r"] for t in ref if t["entry_ts"] >= mid)

    # ── 反向：偏离下限（大于 X% 才进） ──────────────────────
    print()
    print("=" * 100)
    print("反向测：进场价偏离 ZLSMA **必须大于** X% 才开仓（贴均线的假信号挡掉）")
    print("依据：上面看出偏离 <=1.5% 那 447 笔只赚 15R，偏离 >1.5% 的 297 笔赚 63R")
    print("=" * 100)
    print(f"{'偏离下限':>9}{'笔数':>7}{'过滤掉':>8}{'净R':>10}{'均笔R':>9}{'胜率':>8}{'回撤':>9}{'最差日':>9}")
    print("-" * 100)
    floors = []
    for fl in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]:
        tr = []
        per_sym = {}
        for sym, ks in cache:
            got = simulate_gated(ks, inds[sym], make_dev_floor(fl))
            for t in got:
                t["sym"] = sym
            tr.extend(got)
            per_sym[sym] = sum(t["r"] for t in got)
        st = stats(tr)
        floors.append(dict(cap=fl, st=st, per_sym=per_sym, trades=tr))
        print(f"{fl:>8.1f}%{st['n']:>7}{s_ref['n'] - st['n']:>8}"
              f"{st['r']:>+10.1f}{st['avg']:>+9.3f}{st['win']:>7.1f}%"
              f"{st['mdd']:>+9.1f}{worst_day(tr):>+9.2f}")
    print(f"{'不过滤':>9}{s_ref['n']:>7}{0:>8}{s_ref['r']:>+10.1f}{s_ref['avg']:>+9.3f}"
          f"{s_ref['win']:>7.1f}%{s_ref['mdd']:>+9.1f}{worst_day(ref):>+9.2f}")

    print()
    print("下限判据：①净R不降 ②均笔R升 ③回撤不恶化 ④20币里>=13正贡献")
    print("-" * 100)
    print(f"{'偏离下限':>9}{'净R':>9}{'均笔R':>9}{'回撤':>8}{'币种':>7}{'①':>4}{'②':>4}{'③':>4}{'④':>4}")
    print("-" * 100)
    floor_winners = []
    for r in floors:
        st = r["st"]
        c1 = st["r"] >= s_ref["r"]
        c2 = st["avg"] > s_ref["avg"]
        c3 = st["mdd"] >= s_ref["mdd"]
        pos = sum(1 for v in r["per_sym"].values() if v > 0)
        c4 = pos >= 13
        ok = c1 and c2 and c3 and c4
        print(f"{r['cap']:>8.1f}%{st['r']:>+9.1f}{st['avg']:>+9.3f}{st['mdd']:>+8.1f}"
              f"{pos:>4}/20{'✅' if c1 else '❌':>4}{'✅' if c2 else '❌':>4}"
              f"{'✅' if c3 else '❌':>4}{'✅' if c4 else '❌':>4}"
              f"{'   ★四条全过' if ok else ''}")
        if ok:
            floor_winners.append(r)

    print()
    if floor_winners:
        print(f"★ 偏离下限有 {len(floor_winners)} 组四条全过：")
        for w in sorted(floor_winners, key=lambda x: -x["st"]["avg"]):
            st = w["st"]
            print(f"   偏离>{w['cap']:.1f}%  {st['n']} 笔  净R {st['r']:+.1f}  "
                  f"均笔 {st['avg']:+.3f}  胜率 {st['win']:.1f}%  回撤 {st['mdd']:+.1f}  "
                  f"最差日 {worst_day(w['trades']):+.2f}")
    else:
        print("✗ 偏离下限也没有一组四条全过。")

    print()
    print("下限防过拟合：60 天切前后两半")
    print("-" * 100)
    print(f"{'偏离下限':>9}{'前半净R':>11}{'后半净R':>11}   两段都更好？")
    print("-" * 100)
    print(f"{'不过滤':>9}{b1:>+11.1f}{b2:>+11.1f}   —")
    for r in floors:
        a = sum(t["r"] for t in r["trades"] if t["entry_ts"] < mid)
        b = sum(t["r"] for t in r["trades"] if t["entry_ts"] >= mid)
        note = "✅ 是" if (a > b1 and b > b2) else "❌ 否"
        print(f"{r['cap']:>8.1f}%{a:>+11.1f}{b:>+11.1f}   {note}")

    print()
    if winners:
        print(f"★ 上限有 {len(winners)} 组四条全过：")
        for w in sorted(winners, key=lambda x: -x["st"]["avg"]):
            st = w["st"]
            print(f"   偏离<={w['cap']:.1f}%  {st['n']} 笔  净R {st['r']:+.1f}  "
                  f"均笔 {st['avg']:+.3f}  胜率 {st['win']:.1f}%  回撤 {st['mdd']:+.1f}")
    else:
        print("✗ 没有一组四条全过。")

    # ── 防过拟合：前后两半 ─────────────────────────────
    print()
    print("=" * 100)
    print("防过拟合：60 天切前后两半（上限组）")
    print("=" * 100)
    print(f"{'偏离上限':>9}{'前半净R':>11}{'后半净R':>11}   两段都更好？")
    print("-" * 100)
    print(f"{'不过滤':>9}{b1:>+11.1f}{b2:>+11.1f}   —")
    for r in results:
        a = sum(t["r"] for t in r["trades"] if t["entry_ts"] < mid)
        b = sum(t["r"] for t in r["trades"] if t["entry_ts"] >= mid)
        note = "✅ 是" if (a > b1 and b > b2) else "❌ 否"
        print(f"{r['cap']:>8.1f}%{a:>+11.1f}{b:>+11.1f}   {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
