# -*- coding: utf-8 -*-
"""进场闸：加「当下震荡过滤」到底值不值

背景（2026-11-08）：
  今日自动平仓 10 笔亏 755U，其中 6 笔（−650U，占 86%）峰顶 <0.35R —— 进场就逆。
  查下来 S15 策略里 `eff` 出现 0 次：监控池用 24h EFF 筛「币要不要进池」，
  但进场那一刻完全不判断趋势效率。今天 BTC EFF 只有 0.302~0.428（半趋势边缘）。

本脚本测：进场当刻算最近 N 根 15m 的 EFF，低于门槛就不开仓。

口径对齐（铁律，这里第三次强调）：
  除「新增 EFF 闸」这一处外，simulate_eff 与 backtest/s15_live_parity.py 的
  simulate() 逐行一致：同样的 _ce_zlsma(ks,1,2.0,50,True,True)、同样的
  SWING=20 / BBW_MIN=70 / MAXH=64 / MAX_RISK_PCT=30 / FEE=0.0005、
  同样的 2R 平半 + half_tp + 镜像出场。
  脚本第一步就跑 eff_thr=0，结果必须与 s15_live_parity 的线上口径**完全相同**，
  对不上就直接退出 —— 口径不对的数字不能用。

EFF 定义照抄 backend/quality.py 的 _eff()：
  p0,p1 = 窗口首尾 close；amp = (max(high) - min(low)) / min(low)
  eff = |p1/p0 - 1| / amp

用法：python backtest/entry_eff_test.py
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma          # noqa: E402  线上同一份实现
from backtest.s15_live_parity import simulate, stats   # noqa: E402

FEE = 0.0005
SWING = 20
BBW_MIN = 70.0
MAXH = 64
MAX_RISK_PCT = 30.0


def eff_series(ks, win):
    """逐根的 EFF，口径照抄 quality.py::_eff。返回 list，长度同 ks。"""
    n = len(ks)
    out = [None] * n
    for i in range(win - 1, n):
        seg = ks[i - win + 1:i + 1]
        p0, p1 = seg[0]["close"], seg[-1]["close"]
        if not p0 or p0 <= 0:
            continue
        lo = min(b["low"] for b in seg)
        hi = max(b["high"] for b in seg)
        if lo <= 0:
            continue
        amp = (hi - lo) / lo
        if amp <= 0:
            continue
        out[i] = abs(p1 / p0 - 1) / amp
    return out


def simulate_eff(ks, ind, eff, eff_thr, eff_win):
    """与 s15_live_parity.simulate 逐行一致，唯一新增：进场前查 eff[i] < eff_thr 就跳过。"""
    ho, hc = ind["open"], ind["close"]
    zl, dirs, atr, bp = ind["zlsma"], ind["dir"], ind["atr"], ind["bbw_pct"]
    hi_raw, lo_raw = ind["high_raw"], ind["low_raw"]
    n = len(hc)
    out = []
    i = max(1, 60, SWING, eff_win)
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
        # ★ 唯一新增的闸：进场当刻的趋势效率
        if eff_thr > 0:
            e = eff[i]
            if e is None or e < eff_thr:
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


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    cache = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        cache.append((sym, ks))
    print(f"载入 {len(cache)} 个币\n")

    inds = {sym: _ce_zlsma(ks, 1, 2.0, 50, True, True) for sym, ks in cache}

    # ── 第 0 步：口径自证，不加过滤必须与线上脚本完全一致 ──────────────
    print("=" * 100)
    print("第 0 步 · 口径自证：eff_thr=0 必须和 s15_live_parity 一模一样")
    print("=" * 100)
    base_ref, base_mine = [], []
    for sym, ks in cache:
        ind = inds[sym]
        got = simulate(ks, ind, want_mirror=True, use_bbw=True)
        for t in got:
            t["sym"] = sym          # 原函数返回 sym=None，这里补上才能做分币种判据
        base_ref.extend(got)
        base_mine.extend(simulate_eff(ks, ind, eff_series(ks, 96), 0, 96))
    s_ref, s_mine = stats(base_ref), stats(base_mine)
    same = (s_ref["n"] == s_mine["n"] and abs(s_ref["r"] - s_mine["r"]) < 1e-6)
    print(f"  线上脚本   {s_ref['n']:>4} 笔  净R {s_ref['r']:>+8.2f}  胜率 {s_ref['win']:>5.1f}%")
    print(f"  本脚本     {s_mine['n']:>4} 笔  净R {s_mine['r']:>+8.2f}  胜率 {s_mine['win']:>5.1f}%")
    print(f"  -> {'✅ 完全一致，口径可用' if same else '❌ 不一致，停止'}")
    if not same:
        return 1

    # ── 测试矩阵 ─────────────────────────────────────────────
    WINS = [(16, "4h"), (32, "8h"), (48, "12h"), (96, "24h")]
    THRS = [0.0, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]

    print()
    print("=" * 100)
    print("测试矩阵：进场时最近 N 根 15m 的 EFF 必须 >= 门槛，否则不开仓")
    print("（0 门槛 = 现在的线上行为，作为基准）")
    print("=" * 100)
    print(f"{'窗口':<7}{'EFF门槛':>8}{'笔数':>7}{'过滤掉':>8}{'净R':>10}{'均笔R':>9}{'胜率':>8}{'回撤':>9}{'均持仓':>8}")
    print("-" * 100)

    all_res = []
    for win, wlabel in WINS:
        effs = {sym: eff_series(ks, win) for sym, ks in cache}
        for thr in THRS:
            tr = []
            per_sym = {}
            for sym, ks in cache:
                got = simulate_eff(ks, inds[sym], effs[sym], thr, win)
                for t in got:
                    t["sym"] = sym
                tr.extend(got)
                per_sym[sym] = sum(t["r"] for t in got)
            st = stats(tr)
            dropped = s_ref["n"] - st["n"]
            all_res.append(dict(win=win, wlabel=wlabel, thr=thr, st=st,
                                dropped=dropped, per_sym=per_sym, trades=tr))
            flag = "" if thr == 0 else ("  ←基准" if False else "")
            print(f"{wlabel:<7}{thr:>8.2f}{st['n']:>7}{dropped:>8}"
                  f"{st['r']:>+10.1f}{st['avg']:>+9.3f}{st['win']:>7.1f}%"
                  f"{st['mdd']:>+9.1f}{st['bars']:>8.1f}{flag}")
        print("-" * 100)

    # ── 四条判据 ────────────────────────────────────────────
    print()
    print("=" * 100)
    print("判据（四条全过才算真的更好，只看净R 会被过拟合骗）")
    print("  ① 净R 不低于基准            ② 均笔R 高于基准")
    print("  ③ 最大回撤不恶化            ④ 分币种稳健：20 币里 >=13 个不亏")
    print("=" * 100)
    base_r, base_avg, base_mdd = s_ref["r"], s_ref["avg"], s_ref["mdd"]
    # 基准的 per_sym 用于对比
    base_per_sym = {}
    for t in base_ref:
        base_per_sym[t["sym"]] = base_per_sym.get(t["sym"], 0.0) + t["r"]

    print(f"{'参数':<14}{'净R':>9}{'均笔R':>9}{'回撤':>8}{'币种':>7}{'①':>4}{'②':>4}{'③':>4}{'④':>4}   判定")
    print("-" * 100)
    winners = []
    for r in all_res:
        if r["thr"] == 0:
            continue
        st = r["st"]
        c1 = st["r"] >= base_r
        c2 = st["avg"] > base_avg
        c3 = st["mdd"] >= base_mdd
        pos = sum(1 for v in r["per_sym"].values() if v > 0)
        c4 = pos >= 13
        ok = c1 and c2 and c3 and c4
        label = f"{r['wlabel']}/>={r['thr']:.2f}"
        print(f"{label:<14}{st['r']:>+9.1f}{st['avg']:>+9.3f}{st['mdd']:>+8.1f}"
              f"{pos:>4}/20{'✅' if c1 else '❌':>4}{'✅' if c2 else '❌':>4}"
              f"{'✅' if c3 else '❌':>4}{'✅' if c4 else '❌':>4}   "
              f"{'★ 四条全过' if ok else ''}")
        if ok:
            winners.append(r)

    print()
    print("=" * 100)
    print("基准 = 现在的线上行为（无 EFF 过滤）")
    print(f"  {s_ref['n']} 笔  净R {base_r:+.1f}  均笔 {base_avg:+.3f}  胜率 {s_ref['win']:.1f}%  回撤 {base_mdd:+.1f}")
    print("=" * 100)

    if winners:
        print(f"\n★ 有 {len(winners)} 组四条全过，按均笔R 排序：")
        for w in sorted(winners, key=lambda x: -x["st"]["avg"])[:5]:
            st = w["st"]
            print(f"   {w['wlabel']} EFF>={w['thr']:.2f}  {st['n']} 笔  "
                  f"净R {st['r']:+.1f}  均笔 {st['avg']:+.3f}  "
                  f"胜率 {st['win']:.1f}%  回撤 {st['mdd']:+.1f}  "
                  f"（少开 {w['dropped']} 笔）")
    else:
        print("\n✗ 没有一组四条全过 —— 说明「进场加 EFF 过滤」这条路不成立。")

    # ── 防过拟合：前后半段分开看 ──────────────────────────────
    print()
    print("=" * 100)
    print("防过拟合：把 60 天切成前后两半，看优势是不是只在某一段（上次出场 ATR 就栽在这）")
    print("=" * 100)
    all_ts = sorted(t["entry_ts"] for t in base_ref)
    mid = all_ts[len(all_ts) // 2]
    print(f"{'参数':<14}{'前半净R':>10}{'后半净R':>10}   说明")
    print("-" * 100)
    print(f"{'基准(无过滤)':<14}"
          f"{sum(t['r'] for t in base_ref if t['entry_ts'] < mid):>+10.1f}"
          f"{sum(t['r'] for t in base_ref if t['entry_ts'] >= mid):>+10.1f}")
    ba = sum(t["r"] for t in base_ref if t["entry_ts"] < mid)
    bb = sum(t["r"] for t in base_ref if t["entry_ts"] >= mid)
    for w in (winners[:3] if winners else []):
        a = sum(t["r"] for t in w["trades"] if t["entry_ts"] < mid)
        b = sum(t["r"] for t in w["trades"] if t["entry_ts"] >= mid)
        note = "两段都更好 ✅" if (a > ba and b > bb) else \
               ("只在前半段好 ❌" if a > ba else "只在后半段好 ❌")
        tag = w["wlabel"] + "/>=" + format(w["thr"], ".2f")
        print(f"{tag:<14}{a:>+10.1f}{b:>+10.1f}   {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
