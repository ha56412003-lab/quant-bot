# -*- coding: utf-8 -*-
"""出场端治滞后：三条路线一次测完

背景（2026-11-08）：
  镜像出场唯一确凿的滞后案例是 TAUSDT：
    19:00 +0.59R（峰顶）→ 19:30 −0.02R（一根吐光 0.61R）→ 21:00 −0.40R 才平，白吐 144U。
  根因是「三闸必须全过」：CE 是棘轮，快速反向时没翻；HA 没确认 → 干等。

本脚本测三组，全部只测「出场侧」，不动进场：

  A 组：离均线距离细化（要求 |价-zl| <= X% 才平 = 更早出场）
        —— 上一轮 <=0.3% 出过 +86.0R 但回撤 −25.8，这里把网格打细。

  B 组：放松三闸组合（这是治滞后的正面方向）
        现状 = ①CE翻转 AND ②HA反向 AND ③破均线
        测：去掉 ①CE / 去掉 ②HA / 只留 ③破均线
        ★ 关键：TA 那笔卡在 ①CE，所以「去 CE」最可能救它。

  C 组：峰值回吐保护（不依赖三闸，直接锁利润）
        peak 达到 X R 之后，若回吐 Y R 就平。
        ★ TA 峰顶 0.59R → 设 X=0.5/Y=0.4 会在 19:30 那根平掉。

口径：进场价 = 本根收盘（真实线上 strategies.py:492）。
自证：三组各自的「现状配置」必须都等于 744 笔 / +76.1R，否则退出。

注意：出场已封版。本脚本只做回测，不改任何线上代码。
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma    # noqa: E402
from backtest.s15_live_parity import stats   # noqa: E402

FEE = 0.0005
SWING = 20
BBW_MIN = 70.0
MAXH = 64
MAX_RISK_PCT = 30.0


def simulate_exit(ks, ind, mode="all3", max_dev=None,
                  trail_peak=0.0, trail_give=0.0):
    """mode: 'all3' 现状 | 'no_ce' 去CE | 'no_ha' 去HA | 'zl_only' 只破均线
             'ce_zl' CE+破均线 | 'ce_ha' CE+HA（不要求破均线）"""
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
        px = float(ks[i]["close"])
        if px <= 0:
            i += 1
            continue
        lo_i = max(0, i - SWING + 1)
        s_lo = min(lo_raw[j] for j in range(lo_i, i + 1))
        s_hi = max(hi_raw[j] for j in range(lo_i, i + 1))
        stop = s_lo if want == "long" else s_hi
        risk = (px - stop) if want == "long" else (stop - px)
        if risk <= 0 or risk / px * 100 > MAX_RISK_PCT:
            i += 1
            continue

        sign = 1 if want == "long" else -1
        peak = 0.0
        half_done = False
        ep = er = ei = None
        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            # 硬止损
            if want == "long" and l_ <= stop:
                ep, er, ei = min(o_, stop), "hard_stop", j
                break
            if want == "short" and h_ >= stop:
                ep, er, ei = max(o_, stop), "hard_stop", j
                break
            peak = max(peak, ((h_ - px) if want == "long" else (px - l_)) / risk)
            cur = ((c_ - px) if want == "long" else (px - c_)) / risk
            if peak >= 2.0 and not half_done:
                half_done = True
            # ★ C 组：峰值回吐保护（放在镜像之前，先锁利润）
            if trail_peak > 0 and peak >= trail_peak and (peak - cur) >= trail_give:
                ep, er, ei = c_, "trail", j
                break
            if not half_done and (j - i) >= MAXH:
                ep, er, ei = c_, "timeout", j
                break
            if half_done and (cur - 2.0) >= 1.0:
                ep, er, ei = c_, "half_tp", j
                break
            # 三闸
            flip = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                else (dirs[j] == 1 and dirs[j - 1] == -1)
            ha_rev = (hc[j] < ho[j]) if want == "long" else (hc[j] > ho[j])
            z_break = (hc[j] < zl[j]) if want == "long" else (hc[j] > zl[j])
            ok_bp = bp[j] is not None and bp[j] >= BBW_MIN
            if mode == "all3":
                trig = flip and ha_rev and z_break
            elif mode == "no_ce":
                trig = ha_rev and z_break
            elif mode == "no_ha":
                trig = flip and z_break
            elif mode == "zl_only":
                trig = z_break
            elif mode == "ce_zl":
                trig = flip and z_break
            elif mode == "ce_ha":
                trig = flip and ha_rev
            else:
                trig = flip and ha_rev and z_break
            if not (trig and ok_bp):
                continue
            # ★ A 组：离均线距离闸
            if max_dev is not None:
                d = abs(c_ - zl[j]) / zl[j] * 100 if zl[j] else 0.0
                if d > max_dev:
                    continue
            ep, er, ei = c_, "mirror", j
            break
        if ep is None:
            ei = n - 1
            ep = float(ks[-1]["close"])
            er = "eod"
        g = (ep - px) * sign
        r = (g - (px + ep) * FEE) / risk
        if half_done:
            r = 0.5 * 2.0 + 0.5 * r
        out.append(dict(sym=None, r=r, bars=ei - i, reason=er,
                        entry_ts=int(ks[i].get("ts", i)),
                        exit_ts=int(ks[ei].get("ts", ei))))
        i = ei + 1
    return out


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

    def run(**kw):
        tr = []
        per_sym = {}
        for sym, ks in cache:
            got = simulate_exit(ks, inds[sym], **kw)
            for t in got:
                t["sym"] = sym
            tr.extend(got)
            per_sym[sym] = sum(t["r"] for t in got)
        return tr, per_sym

    # ── 自证 ───────────────────────────────────────
    base, base_sym = run(mode="all3")
    st0 = stats(base)
    print("=" * 100)
    print(f"自证：三组各自的「现状配置」基准 = {st0['n']} 笔 {st0['r']:+.1f}R")
    print("      （与线上真实口径对齐；回测母本用下一根开盘为 +78.1R，此差是口径差）")
    print("=" * 100)
    if st0["n"] != 744:
        print(f"  ✗ 笔数不是 744，退出")
        return 1
    print(f"  ✅ 笔数一致\n")

    pos0 = sum(1 for v in base_sym.values() if v > 0)

    def line(label, tr, per_sym, mark=""):
        st = stats(tr)
        pos = sum(1 for v in per_sym.values() if v > 0)
        c1 = st["r"] >= st0["r"]
        c2 = st["avg"] > st0["avg"]
        c3 = st["mdd"] >= st0["mdd"]
        c4 = pos >= 13
        good = c1 and c2 and c3 and c4
        print(f"{label:<22}{st['n']:>7}{st['r']:>+10.1f}{st['avg']:>+9.3f}"
              f"{st['win']:>7.1f}%{st['mdd']:>+9.1f}{pos:>5}/20"
              f"{'✅' if c1 else '❌':>3}{'✅' if c2 else '❌':>3}"
              f"{'✅' if c3 else '❌':>3}{'✅' if c4 else '❌':>3}"
              f"{'  ★' if good else ''}{mark}")
        return good, st

    HDR = (f"{'方案':<22}{'笔数':>7}{'净R':>10}{'均笔R':>9}{'胜率':>8}{'回撤':>9}"
           f"{'币种':>7}  {'①②③④':<8}")
    DASH = "-" * 100

    # ── A 组：离均线距离细化 ─────────────────────────
    print("=" * 100)
    print("A 组：镜像出场要求「离均线 <= X%」（越靠均线越早平 → 治滞后）")
    print("=" * 100)
    print(HDR)
    print(DASH)
    print(f"{'现状（不加）':<22}{st0['n']:>7}{st0['r']:>+10.1f}{st0['avg']:>+9.3f}"
          f"{st0['win']:>7.1f}%{st0['mdd']:>+9.1f}{pos0:>5}/20")
    print(DASH)
    a_win = []
    for thr in (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50):
        tr, ps = run(mode="all3", max_dev=thr)
        g, st = line(f"<= {thr:.2f}%", tr, ps)
        if g:
            a_win.append((thr, st, tr))
    print(DASH)

    # ── B 组：放松三闸 ────────────────────────────
    print()
    print("=" * 100)
    print("B 组：放松三闸组合（★ 治滞后的正面方向）")
    print("=" * 100)
    print(HDR)
    print(DASH)
    print(f"{'现状 CE+HA+ZL':<22}{st0['n']:>7}{st0['r']:>+10.1f}{st0['avg']:>+9.3f}"
          f"{st0['win']:>7.1f}%{st0['mdd']:>+9.1f}{pos0:>5}/20")
    print(DASH)
    b_win = []
    for mode, label in (("no_ce", "去CE  HA+ZL"), ("no_ha", "去HA  CE+ZL"),
                        ("ce_zl", "CE+ZL（同去HA）"), ("zl_only", "只留 破均线"),
                        ("ce_ha", "CE+HA（不破均线）")):
        tr, ps = run(mode=mode)
        g, st = line(label, tr, ps)
        if g:
            b_win.append((mode, st, tr))
    print(DASH)

    # ── C 组：峰值回吐保护 ──────────────────────────
    print()
    print("=" * 100)
    print("C 组：峰值回吐保护（peak 达到 X R 后，回吐 Y R 就平）")
    print("=" * 100)
    print(HDR)
    print(DASH)
    print(f"{'现状（无保护）':<22}{st0['n']:>7}{st0['r']:>+10.1f}{st0['avg']:>+9.3f}"
          f"{st0['win']:>7.1f}%{st0['mdd']:>+9.1f}{pos0:>5}/20")
    print(DASH)
    c_win = []
    for tp in (0.5, 1.0, 1.5):
        for tg in (0.3, 0.5, 1.0):
            tr, ps = run(mode="all3", trail_peak=tp, trail_give=tg)
            g, st = line(f"peak>={tp} 回吐{tg}", tr, ps)
            if g:
                c_win.append((tp, tg, st, tr))
    print(DASH)

    # ── 汇总 ──────────────────────────────────────
    print()
    print("=" * 100)
    print("四条全过的方案汇总")
    print("=" * 100)
    if not (a_win or b_win or c_win):
        print("  ✗ 一条都没有。")
    for thr, st, tr in a_win:
        print(f"  A 离均线<={thr:.2f}%   {st['n']} 笔 {st['r']:+.1f}R "
              f"均笔 {st['avg']:+.3f} 回撤 {st['mdd']:+.1f}")
    for mode, st, tr in b_win:
        print(f"  B {mode:<10}        {st['n']} 笔 {st['r']:+.1f}R "
              f"均笔 {st['avg']:+.3f} 回撤 {st['mdd']:+.1f}")
    for tp, tg, st, tr in c_win:
        print(f"  C peak>={tp}回吐{tg}    {st['n']} 笔 {st['r']:+.1f}R "
              f"均笔 {st['avg']:+.3f} 回撤 {st['mdd']:+.1f}")

    # ── 最优者的防过拟合 ─────────────────────────────
    allwin = ([(f"A <={t:.2f}%", s, tr) for t, s, tr in a_win]
              + [(f"B {m}", s, tr) for m, s, tr in b_win]
              + [(f"C peak>={p}回吐{g}", s, tr) for p, g, s, tr in c_win])
    if allwin:
        print()
        print("=" * 100)
        print("防过拟合：前后两半")
        print("=" * 100)
        ts = sorted(t["entry_ts"] for t in base)
        mid = ts[len(ts) // 2]
        b1 = sum(t["r"] for t in base if t["entry_ts"] < mid)
        b2 = sum(t["r"] for t in base if t["entry_ts"] >= mid)
        print(f"{'方案':<22}{'前半':>11}{'后半':>11}   两段都更好？")
        print(DASH)
        print(f"{'基准':<22}{b1:>+11.1f}{b2:>+11.1f}   —")
        for name, st, tr in allwin:
            a = sum(t["r"] for t in tr if t["entry_ts"] < mid)
            b = sum(t["r"] for t in tr if t["entry_ts"] >= mid)
            note = "✅ 是" if (a > b1 and b > b2) else "❌ 否"
            print(f"{name:<22}{a:>+11.1f}{b:>+11.1f}   {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
