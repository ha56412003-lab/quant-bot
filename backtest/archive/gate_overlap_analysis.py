# -*- coding: utf-8 -*-
"""「偏离 > 0.5%」这道闸到底在干什么？会跟 CE / HA / BBW 重叠或矛盾吗？

要回答四个具体问题：
  ① 它是不是「重复建设」—— 挡掉的交易是否本来就会被别的闸挡？
  ② 会不会「自相矛盾」—— HA 要求 c > zlsma，偏离又要求 c 离 zlsma 0.5%+
     两者方向一致，是否限制过死？
  ③ 和 BBW 分位有没有冗余（带宽大 = 波动大 = 价格可能天然离均线远）
  ④ 它带来的信息是独立的，还是别的指标已经包含了

做法：不设任何过滤，把每个「三闸全过」的进场点全部记录下来，
      带上偏离 / BBW 分位 / ATR% / 结果 R，然后做分桶和相关性分析。
"""
import glob
import json
import math
import os
import sys
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma    # noqa: E402

FEE = 0.0005
SWING = 20
BBW_MIN = 70.0
MAXH = 64
MAX_RISK_PCT = 30.0


def collect(ks, ind, sym):
    """与 s15_live_parity.simulate 同样的判定，但不加任何额外闸，只记录特征"""
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

        dev = abs(px - zl[i]) / zl[i] * 100
        atr_pct = atr[i] / px * 100
        # 信号 K 线实体大小（HA）
        body = abs(hc[i] - ho[i]) / ho[i] * 100
        # 这根 K 线走多远（真实 K 线振幅）
        rng = (float(ks[i]["high"]) - float(ks[i]["low"])) / float(ks[i]["low"]) * 100

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
        out.append(dict(sym=sym, r=r, dev=dev, bbw=bp[i], atr_pct=atr_pct,
                        body=body, rng=rng, peak=peak, reason=er,
                        entry_ts=int(ks[i].get("ts", i))))
        i = ei + 1
    return out


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    if sx == 0 or sy == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n * sx * sy)


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    recs = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        ind = _ce_zlsma(ks, 1, 2.0, 50, True, True)
        recs.extend(collect(ks, ind, sym))
    print(f"三闸全过的进场点共 {len(recs)} 个\n")

    # ── ① 分桶：偏离到底起什么作用 ───────────────────────
    BUCKETS = [(0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0), (1.0, 1.5),
               (1.5, 2.5), (2.5, 4.0), (4.0, 6.0), (6.0, 10.0), (10.0, 999)]
    print("=" * 100)
    print("① 偏离分桶：这道闸到底在挡什么")
    print("=" * 100)
    print(f"{'偏离区间':<14}{'笔数':>7}{'净R':>9}{'均笔R':>9}{'胜率':>8}{'峰顶中位':>10}{'BBW中位':>9}")
    print("-" * 100)
    for lo, hi in BUCKETS:
        seg = [x for x in recs if lo <= x["dev"] < hi]
        if not seg:
            continue
        n = len(seg)
        net = sum(x["r"] for x in seg)
        win = sum(1 for x in seg if x["r"] > 0) / n * 100
        peaks = sorted(x["peak"] for x in seg)
        bbws = sorted(x["bbw"] for x in seg)
        lab = f"{lo:.2f}~{hi:.2f}%" if hi < 999 else f">{lo:.1f}%"
        print(f"{lab:<14}{n:>7}{net:>+9.1f}{net / n:>+9.3f}{win:>7.1f}%"
              f"{peaks[n // 2]:>10.2f}{bbws[n // 2]:>9.1f}")

    small = [x for x in recs if x["dev"] <= 0.5]
    big = [x for x in recs if x["dev"] > 0.5]
    print("-" * 100)
    print(f"  <=0.5%（会被挡）     {len(small):>3} 笔  净R {sum(x['r'] for x in small):+7.1f}  "
          f"均笔 {sum(x['r'] for x in small) / max(len(small), 1):+.3f}")
    print(f"  >0.5%（留下来）      {len(big):>3} 笔  净R {sum(x['r'] for x in big):+7.1f}  "
          f"均笔 {sum(x['r'] for x in big) / max(len(big), 1):+.3f}")

    # ── ② 重叠度：和三闸的关系 ────────────────────────
    print()
    print("=" * 100)
    print("② 重叠度：偏离和三闸各自的关系")
    print("=" * 100)
    print("  说明：三闸（CE翻转 / HA关键K线 / BBW>70）是「是否开仓」的判断，")
    print("        偏离是「开在哪」的判断，维度不同。下面看数值上有没有冗余。")
    print()
    devs = [x["dev"] for x in recs]
    for key, label in [("bbw", "BBW 分位"), ("atr_pct", "ATR%"), ("body", "HA 实体%"),
                       ("rng", "K 线振幅%"), ("peak", "最终峰顶 R")]:
        ys = [x[key] for x in recs]
        r = pearson(devs, ys)
        strength = ("几乎无关" if abs(r) < 0.15 else
                    "弱相关" if abs(r) < 0.35 else
                    "中等相关" if abs(r) < 0.6 else "强相关")
        print(f"  偏离 vs {label:<12} 相关系数 {r:>+6.3f}   {strength}")

    # ── ③ 矛盾检查 ────────────────────────────────
    print()
    print("=" * 100)
    print("③ 矛盾检查：HA 要求「站上 ZLSMA」，偏离要求「离 0.5%」，会不会互相打架？")
    print("=" * 100)
    print("  方向：两者同向 —— HA 说「在均线这一侧」，偏离说「离得够远」。")
    print(f"  HA 通过但偏离 <=0.5% 的：{len(small)} 笔 / {len(recs)} 笔 "
          f"= {len(small) / len(recs) * 100:.1f}%")
    print("  也就是说 100 个能开仓的机会里，有 "
          f"{len(small) / len(recs) * 100:.0f} 个是「刚贴上均线、还没走出去」的。")
    print()
    print("  这 " + str(len(small)) + " 笔的表现：")
    if small:
        net = sum(x["r"] for x in small)
        win = sum(1 for x in small if x["r"] > 0) / len(small) * 100
        hs = sum(1 for x in small if x["reason"] == "hard_stop") / len(small) * 100
        print(f"    净R {net:+.1f}  均笔 {net / len(small):+.3f}  胜率 {win:.1f}%  "
              f"止损离场占比 {hs:.1f}%")
    print()
    print("  对照：留下来的 " + str(len(big)) + " 笔：")
    if big:
        net = sum(x["r"] for x in big)
        win = sum(1 for x in big if x["r"] > 0) / len(big) * 100
        hs = sum(1 for x in big if x["reason"] == "hard_stop") / len(big) * 100
        print(f"    净R {net:+.1f}  均笔 {net / len(big):+.3f}  胜率 {win:.1f}%  "
              f"止损离场占比 {hs:.1f}%")

    # ── ④ 独立性：挡住的是不是「别的指标也能挑出来的差交易」────
    print()
    print("=" * 100)
    print("④ 独立性：偏离挡住的那批，是不是 BBW 本来就低 / ATR 本来就小？")
    print("=" * 100)
    def med(seg, key):
        v = sorted(x[key] for x in seg)
        return v[len(v) // 2] if v else 0.0
    print(f"{'指标':<16}{'<=0.5% 组中位':>16}{'>0.5% 组中位':>16}   差异说明")
    print("-" * 100)
    for key, label in [("bbw", "BBW 分位"), ("atr_pct", "ATR%"),
                       ("body", "HA 实体%"), ("peak", "最终峰顶 R")]:
        a, b = med(small, key), med(big, key)
        diff = (a - b) / abs(b) * 100 if b else 0
        note = "两批差不多 → 偏离是独立信息" if abs(diff) < 10 else \
               (f"被挡组偏低 {abs(diff):.0f}% → 有部分重叠" if diff < 0 else
                f"被挡组偏高 {diff:.0f}%")
        print(f"{label:<16}{a:>16.2f}{b:>16.2f}   {note}")

    # ── ⑤ 和快速失败闸会不会重复 ───────────────────────
    print()
    print("=" * 100)
    print("⑤ 和「快速失败 4h」闸会不会重复？")
    print("=" * 100)
    # 模拟：4h 内 peak 没到 0.5R 的会被 quick fail 砍
    # 这里近似：看 peak 分布 —— 但精确判断需要逐根回放，用 reason 近似
    print("  两道闸看的是不同阶段：")
    print("    偏离闸   → 进场前：这个位置值不值得进（空间问题）")
    print("    快速失败 → 进场后：进去 4 小时了还在磨，撤（时间问题）")
    print()
    qf_like = [x for x in recs if x["peak"] < 0.5]
    print(f"  峰顶 <0.5R 的交易（快速失败闸的主要目标）：{len(qf_like)} 笔，"
          f"合计 {sum(x['r'] for x in qf_like):+.1f}R")
    both = [x for x in qf_like if x["dev"] <= 0.5]
    print(f"  其中同时偏离 <=0.5% 的（两闸都会管）：{len(both)} 笔，"
          f"合计 {sum(x['r'] for x in both):+.1f}R")
    if qf_like:
        print(f"  也就是说快速失败闸管的 {len(qf_like)} 笔里，只有 "
              f"{len(both) / len(qf_like) * 100:.0f}% 会被偏离闸先挡掉 —— 重叠很小。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
