"""BBW 分位到底在剔除什么 —— 按 BBW 分组看真实收益。

思路：
  把 BBW 门槛临时设为 0（等于关掉这道闸），让所有「CE 翻转 + HA 关键K线」
  的信号都进场，记录每笔进场时的 BBW 分位，然后按分位分组统计收益。

  如果 BBW > 70 那组明显比 < 70 的好 → 这道闸有效，它剔掉的是坏信号。
  如果各组都差不多 → 这道闸白加，只是减少了交易次数。

口径与线上完全一致：
  - 直接 import backend.strategies._ce_zlsma（唯一实现）
  - use_close=True、use_ha=True
  - 2R 平半 + 半仓后再赚 1R 全平 + 镜像出场 + 16h 超时 + 硬止损
  - fee_rate 0.0005

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/bbw_effect.py
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma  # noqa: E402

FEE = 0.0005
SWING = 20
MAXH = 64                 # 16 小时 / 15m
MAX_RISK_PCT = 30.0


def simulate_all(ks, ind, half_at_r=2.0, half_tp_r=1.0):
    """与 s15_live_parity.simulate 同源，但：
       - 不设 BBW 门槛（bbw_thr=0）
       - 每笔记录进场时的 BBW 分位
    """
    ho, hc = ind["open"], ind["close"]
    zl, dirs, bp = ind["zlsma"], ind["dir"], ind["bbw_pct"]
    hi_raw, lo_raw = ind["high_raw"], ind["low_raw"]
    n = len(hc)
    out = []
    i = max(1, 60, SWING)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None:
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
        # ← 这里原本是 BBW 门槛，现在只记录不过滤
        bbw_at_entry = bp[i]

        entry = float(ks[i + 1]["open"])
        if entry <= 0:
            i += 1
            continue
        lo = max(0, i - SWING + 1)
        s_lo = min(lo_raw[j] for j in range(lo, i + 1))
        s_hi = max(hi_raw[j] for j in range(lo, i + 1))
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
            if peak >= half_at_r and not half_done:
                half_done = True
            if not half_done and (j - i) >= MAXH:
                ep, er, ei = c_, "timeout", j
                break
            if half_done and half_tp_r > 0 and (cur - half_at_r) >= half_tp_r:
                ep, er, ei = c_, "half_tp", j
                break
            flip = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                else (dirs[j] == 1 and dirs[j - 1] == -1)
            ha_ok = (hc[j] < ho[j] and hc[j] < zl[j]) if want == "long" \
                else (hc[j] > ho[j] and hc[j] > zl[j])
            if flip and ha_ok:
                ep, er, ei = c_, "mirror", j
                break
        if ep is None:
            ei = n - 1
            ep = float(ks[-1]["close"])
            er = "eod"
        assert ep is not None and er is not None
        g = (ep - entry) * sign
        r = (g - (entry + ep) * FEE) / risk
        if half_done:
            r = 0.5 * half_at_r + 0.5 * r
        out.append(dict(r=r, bars=ei - i, reason=er, bbw=bbw_at_entry,
                        entry_ts=int(ks[i + 1].get("ts", i)),
                        exit_ts=int(ks[ei].get("ts", ei))))
        i = ei + 1
    return out


def group_stats(trades):
    if not trades:
        return dict(n=0, r=0.0, avg=0.0, win=0.0)
    r = sum(t["r"] for t in trades)
    w = sum(1 for t in trades if t["r"] > 0) / len(trades) * 100
    return dict(n=len(trades), r=r, avg=r / len(trades), win=w)


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    cache = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        cache.append((sym, ks))
    print(f"载入 {len(cache)} 币 × 60 天 15m K 线")
    print()

    all_tr = []
    for sym, ks in cache:
        ind = _ce_zlsma(ks, 1, 2.0, 50, True, True)
        trs = simulate_all(ks, ind)
        for t in trs:
            t["sym"] = sym
        all_tr.extend(trs)

    valid = [t for t in all_tr if t["bbw"] is not None]
    print(f"CE 翻转 + HA 关键K线 全过的信号共 {len(all_tr)} 个"
          f"（BBW 分位有值的 {len(valid)} 个）")
    print()

    # ── 按 BBW 分位分组 ────────────────────────────────────────
    buckets = [(0, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 80), (80, 101)]
    print("=" * 92)
    print("按进场时的 BBW 分位分组（这就是「BBW 在剔除什么」的答案）")
    print("=" * 92)
    print(f"{'BBW分位区间':<16}{'笔数':>7}{'占比':>8}{'净R':>10}{'均笔R':>10}{'胜率':>8}{'这组赚钱吗':>12}")
    print("-" * 92)
    for a, b in buckets:
        grp = [t for t in valid if a <= t["bbw"] < b]
        s = group_stats(grp)
        share = f"{s['n'] / max(1, len(valid)) * 100:.0f}%"
        verdict = "赚钱" if s["avg"] > 0.05 else ("打平" if s["avg"] > -0.05 else "亏钱")
        print(f"{a:>3}-{min(b, 100):<3} 分位{'':<6}{s['n']:>7}{share:>8}"
              f"{s['r']:>+10.1f}{s['avg']:>+10.3f}{s['win']:>7.1f}%{verdict:>12}")

    print("-" * 92)
    lo = [t for t in valid if t["bbw"] < 70]
    hi = [t for t in valid if t["bbw"] >= 70]
    sl, sh = group_stats(lo), group_stats(hi)
    print(f"{'BBW < 70（现在被拦）':<16}{sl['n']:>7}{'':>8}{sl['r']:>+10.1f}{sl['avg']:>+10.3f}{sl['win']:>7.1f}%")
    print(f"{'BBW >= 70（现在放行）':<16}{sh['n']:>7}{'':>8}{sh['r']:>+10.1f}{sh['avg']:>+10.3f}{sh['win']:>7.1f}%")
    print()

    print("=" * 92)
    print("结论：BBW 门槛 70 的效果")
    print("=" * 92)
    print(f"  放行的（>=70）：{sh['n']} 笔，净 {sh['r']:+.1f}R，均笔 {sh['avg']:+.3f}R，胜率 {sh['win']:.1f}%")
    print(f"  拦掉的（<70） ：{sl['n']} 笔，净 {sl['r']:+.1f}R，均笔 {sl['avg']:+.3f}R，胜率 {sl['win']:.1f}%")
    diff = sh["avg"] - sl["avg"]
    print()
    if diff > 0.1:
        print(f"  ✅ 门槛有效：拦掉的那些均笔 {sl['avg']:+.3f}R，明显比放行的差。")
        print(f"     拦掉它们等于避免了 {sl['r']:+.1f}R 的亏损。")
    elif diff > -0.05:
        print(f"  ⚠️ 门槛几乎没用：两组均笔只差 {diff:+.3f}R，等于随机。")
        print(f"     它只是把交易次数砍掉 {sl['n'] / max(1, len(valid)) * 100:.0f}%，没提高质量。")
    else:
        print(f"  ❌ 门槛是反的：被拦掉的反而更好（{sl['avg']:+.3f}R vs {sh['avg']:+.3f}R）。")
        print(f"     拦掉它们等于错过了 {sl['r']:+.1f}R 的利润。")

    # ── 门槛值对比 ──────────────────────────────────────
    print()
    print("=" * 92)
    print("把门槛定在哪最好（同一批信号，只改门槛值）")
    print("=" * 92)
    print(f"{'门槛':>6}{'笔数':>8}{'净R':>10}{'均笔R':>10}{'胜率':>8}{'笔数保留':>10}{'均笔提升':>10}")
    print("-" * 92)
    base = group_stats(valid)
    for thr in (0.0, 50.0, 60.0, 70.0, 75.0, 80.0, 85.0, 90.0):
        grp = [t for t in valid if t["bbw"] >= thr]
        s = group_stats(grp)
        keep = f"{s['n'] / max(1, base['n']) * 100:.0f}%"
        lift = f"{(s['avg'] - base['avg']):+.3f}"
        mark = "  <- 现在" if thr == 70.0 else ("  <- 不过滤" if thr == 0.0 else "")
        print(f"{thr:>6.0f}{s['n']:>8}{s['r']:>+10.1f}{s['avg']:>+10.3f}"
              f"{s['win']:>7.1f}%{keep:>10}{lift:>10}{mark}")

    # 各出场原因分布，看 BBW 低的是不是更容易被时间止损磨死
    print()
    print("=" * 92)
    print("出场原因（看低 BBW 的仓位是怎么死的）")
    print("=" * 92)
    for label, grp in (("BBW < 70", lo), ("BBW >= 70", hi)):
        if not grp:
            continue
        cnt = {}
        for t in grp:
            cnt[t["reason"]] = cnt.get(t["reason"], 0) + 1
        tot = len(grp)
        parts = [f"{k} {v}({v / tot * 100:.0f}%)" for k, v in sorted(cnt.items(), key=lambda x: -x[1])]
        print(f"  {label:<10} {' / '.join(parts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
