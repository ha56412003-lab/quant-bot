# -*- coding: utf-8 -*-
"""出场端加「出场价离 ZLSMA 至少 0.5%」会怎样？

浩哥 2026-11-08 问的正是这个：进场已经证明「离均线 >=0.5% 才是真突破」，
那出场是不是也该对称地要求「跌破均线 >=0.5% 才是真反转」？

现状出场三闸（多头 / 空头取反）：
    ① CE 方向翻转
    ② HA 反向关键 K 线：hc < ho  （阴线）
    ③ 破 ZLSMA：        hc < zl  （在均线下方）
三者同时成立 → 平仓。只认已收盘 K 线。

关键疑问：③ 只要求「在下方」，没要求「在下方多远」。
    如果价格刚跌破均线（|价-均线| 只有 0.05%），三闸照样成立 → 立刻平。
    那出场端再要求 >=0.5%，就是「跌破 0.5% 才平」= 明显推迟。

本脚本做两件事：
  一、测出现状：每次镜像出场那一刻，价格离 ZLSMA 到底多远（分布）
  二、实测加闸：要求 >= X% 才允许镜像出场，X = 0.2/0.3/0.5/1.0/1.5
      —— 同时反向测「要求 <= X% 才平」（即贴近均线就赶紧平，治滞后）

口径：进场价 = 本根收盘（真实线上 strategies.py:492）。
自证：min_exit_dev=0 时必须等于现状 744 笔。
"""
import glob
import json
import os
import sys
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma    # noqa: E402
from backtest.s15_live_parity import stats   # noqa: E402

FEE = 0.0005
SWING = 20
BBW_MIN = 70.0
MAXH = 64
MAX_RISK_PCT = 30.0


def simulate_exitdev(ks, ind, min_exit_dev=0.0, max_exit_dev=None, collect=False):
    """min_exit_dev: 镜像出场时要求 |价-zl|/zl*100 >= 此值（更晚平）
       max_exit_dev: 镜像出场时要求 |价-zl|/zl*100 <= 此值（更早平 / 治滞后）"""
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
        exit_dev = None
        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
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
                d = abs(c_ - zl[j]) / zl[j] * 100 if zl[j] else 0.0
                # ★ 出场端偏离闸（只在这一刻判）
                pass_gate = True
                if min_exit_dev > 0 and d < min_exit_dev:
                    pass_gate = False
                if max_exit_dev is not None and d > max_exit_dev:
                    pass_gate = False
                if pass_gate:
                    ep, er, ei = c_, "mirror", j
                    exit_dev = d
                    break
                # 不满足 → 继续持有，等下一根
        if ep is None:
            ei = n - 1
            ep = float(ks[-1]["close"])
            er = "eod"
        g = (ep - px) * sign
        r = (g - (px + ep) * FEE) / risk
        if half_done:
            r = 0.5 * 2.0 + 0.5 * r
        rec = dict(sym=None, r=r, bars=ei - i, reason=er,
                   entry_ts=int(ks[i].get("ts", i)),
                   exit_ts=int(ks[ei].get("ts", ei)))
        if collect:
            rec["exit_dev"] = exit_dev
        out.append(rec)
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

    # ── 一、现状：镜像出场那一刻离均线多远 ──────────────
    base = []
    for sym, ks in cache:
        got = simulate_exitdev(ks, inds[sym], 0.0, None, collect=True)
        for t in got:
            t["sym"] = sym
        base.extend(got)
    st0 = stats(base)
    print("=" * 100)
    print(f"一、现状：{st0['n']} 笔 {st0['r']:+.1f}R（自证基准）")
    print("=" * 100)
    mir = [t for t in base if t["reason"] == "mirror" and t.get("exit_dev") is not None]
    if mir:
        ds = sorted(t["exit_dev"] for t in mir)
        print(f"  镜像出场 {len(mir)} 笔，出场那一刻的价格离 ZLSMA：")
        for q, lab in ((0, "最小"), (len(ds) // 10, "10分位"), (len(ds) // 4, "25分位"),
                       (len(ds) // 2, "中位"), (len(ds) * 3 // 4, "75分位"),
                       (len(ds) * 9 // 10, "90分位"), (len(ds) - 1, "最大")):
            print(f"    {lab:<8}{ds[q]:>7.2f}%")
        for thr in (0.2, 0.3, 0.5, 1.0, 1.5):
            cnt = sum(1 for d in ds if d < thr)
            print(f"    离均线 < {thr}% 的：{cnt:>4} 笔 / {len(ds)} 笔 "
                  f"= {cnt / len(ds) * 100:>5.1f}%   ← 加 >= {thr}% 闸会推迟这些")

    # ── 二、加闸实测 ──────────────────────────────
    print()
    print("=" * 100)
    print("二、出场端加偏离闸：要求「离均线 >= X% 才平」（方向：更晚出场）")
    print("=" * 100)
    print(f"{'门槛':<12}{'笔数':>7}{'净R':>10}{'均笔R':>9}{'胜率':>8}{'回撤':>9}"
          f"{'镜像':>7}{'超时':>7}{'止损':>7}")
    print("-" * 100)
    print(f"{'现状':<12}{st0['n']:>7}{st0['r']:>+10.1f}{st0['avg']:>+9.3f}"
          f"{st0['win']:>7.1f}%{st0['mdd']:>+9.1f}"
          f"{sum(1 for t in base if t['reason'] == 'mirror'):>7}"
          f"{sum(1 for t in base if t['reason'] == 'timeout'):>7}"
          f"{sum(1 for t in base if t['reason'] == 'hard_stop'):>7}")
    print("-" * 100)
    for thr in (0.2, 0.3, 0.5, 1.0, 1.5):
        tr = []
        for sym, ks in cache:
            got = simulate_exitdev(ks, inds[sym], thr, None)
            for t in got:
                t["sym"] = sym
            tr.extend(got)
        st = stats(tr)
        print(f"{'>= ' + str(thr) + '%':<12}{st['n']:>7}{st['r']:>+10.1f}{st['avg']:>+9.3f}"
              f"{st['win']:>7.1f}%{st['mdd']:>+9.1f}"
              f"{sum(1 for t in tr if t['reason'] == 'mirror'):>7}"
              f"{sum(1 for t in tr if t['reason'] == 'timeout'):>7}"
              f"{sum(1 for t in tr if t['reason'] == 'hard_stop'):>7}")

    # ── 三、反向：贴近均线就赶紧平（治滞后）───────────────
    print()
    print("=" * 100)
    print("三、反向测：要求「离均线 <= X% 才平」（方向：更早出场 / 治滞后）")
    print("=" * 100)
    print(f"{'门槛':<12}{'笔数':>7}{'净R':>10}{'均笔R':>9}{'胜率':>8}{'回撤':>9}"
          f"{'镜像':>7}{'超时':>7}{'止损':>7}")
    print("-" * 100)
    for thr in (0.2, 0.3, 0.5, 1.0, 1.5):
        tr = []
        for sym, ks in cache:
            got = simulate_exitdev(ks, inds[sym], 0.0, thr)
            for t in got:
                t["sym"] = sym
            tr.extend(got)
        st = stats(tr)
        print(f"{'<= ' + str(thr) + '%':<12}{st['n']:>7}{st['r']:>+10.1f}{st['avg']:>+9.3f}"
              f"{st['win']:>7.1f}%{st['mdd']:>+9.1f}"
              f"{sum(1 for t in tr if t['reason'] == 'mirror'):>7}"
              f"{sum(1 for t in tr if t['reason'] == 'timeout'):>7}"
              f"{sum(1 for t in tr if t['reason'] == 'hard_stop'):>7}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
