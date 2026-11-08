"""诊断：监控池只有 13 个，瓶颈到底在哪一关？

进池三条件（quality.py recompute 第 343-356 行）：
  ① 5m ATR > ATR_ENTER_MIN (0.40%)
  ② 24h 成交额 >= 1000 万 U
  ③ 插针 (单根 1h 振幅 ÷ 1h ATR) <= 4.0

浩哥 2026-11-08：想放宽 ATR。但先要确认卡点到底是不是 ATR。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import binance, pool, quality  # noqa: E402

CONC = 8
SPIKE_LOOKBACK = 100


async def probe(sym, sem, qv_map):
    async with sem:
        try:
            # 5m ATR（池子口径）
            b5 = await binance.klines(sym, "5m", 20)
            atr5 = quality._atr(b5, 14) / b5[-1]["close"] * 100 if len(b5) >= 15 else None

            # 插针（1h）
            b1 = await binance.klines(sym, "1h", SPIKE_LOOKBACK)
            spike = None
            if len(b1) >= 96:
                a = quality._atr(b1)
                if a:
                    spike = max(x["high"] - x["low"] for x in b1[-96:]) / a

            return sym, {"atr": atr5, "qv": qv_map.get(sym, 0.0), "spike": spike}
        except Exception:
            return sym, None


async def main():
    syms = pool.load()
    tick = await binance.ticker_24h_all()
    qv_map = {d["symbol"]: float(d.get("quoteVolume") or 0) for d in tick}
    print(f"候选池 {len(syms)} 个币，三维度全查...\n")

    sem = asyncio.Semaphore(CONC)
    res = await asyncio.gather(*[probe(s, sem, qv_map) for s in syms])
    d = {s: v for s, v in res if v}

    MIN_QV = 1e7
    MAX_SPIKE = 4.0

    def fails(v, atr_thr):
        f = []
        if v["atr"] is None or v["atr"] <= atr_thr:
            f.append("ATR不够")
        if v["qv"] < MIN_QV:
            f.append("成交额小")
        if v["spike"] is not None and v["spike"] > MAX_SPIKE:
            f.append("易插针")
        return f

    print("=" * 92)
    print("① 现行三关（ATR>0.40 / 成交额≥1000万 / 插针≤4.0）各卡掉多少")
    print("=" * 92)
    from collections import Counter
    cnt = Counter()
    for s, v in d.items():
        f = fails(v, 0.40)
        if not f:
            cnt["全过 → 进池"] += 1
        else:
            for x in f:
                cnt[x] += 1
    for k, n in cnt.most_common():
        print(f"  {k:<18}{n:>4} 个")

    print("\n" + "=" * 92)
    print("② 只放宽 ATR：池子能变多大？（成交额/插针不变）")
    print("=" * 92)
    print(f"{'ATR 门槛':<12}{'进池数':>9}{'其中被量/插针卡':>16}")
    print("-" * 92)
    for thr in (0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.0):
        ok = 0
        blocked = 0
        for s, v in d.items():
            if v["atr"] is None or v["atr"] <= thr:
                continue
            f = [x for x in fails(v, thr) if x != "ATR不够"]
            if f:
                blocked += 1
            else:
                ok += 1
        tag = "  ← 现在" if thr == 0.40 else ""
        print(f"  > {thr:<8.2f}%{ok:>8} 个{blocked:>14} 个{tag}")

    print("\n" + "=" * 92)
    print("③ 真正的瓶颞：max_spike（插针）放宽能扩多大")
    print("=" * 92)
    print(f"{'插针上限':<14}{'ATR>0.40':>12}{'ATR>0.30':>12}{'ATR>0.15':>12}")
    print("-" * 92)
    for sp in (4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 999.0):
        row = []
        for atr_thr in (0.40, 0.30, 0.15):
            ok = sum(1 for v in d.values()
                     if v["atr"] is not None and v["atr"] > atr_thr
                     and v["qv"] >= MIN_QV
                     and (v["spike"] is None or v["spike"] <= sp))
            row.append(ok)
        tag = "  ← 现在" if sp == 4.0 else ("  ← 不限插针" if sp == 999.0 else "")
        print(f"  <= {sp:<10.1f}{row[0]:>10} 个{row[1]:>10} 个{row[2]:>10} 个{tag}")

    print("\n" + "=" * 92)
    print("④ 卡点排序：如果放宽到 ATR > 0.15%，剩下被谁卡住")
    print("=" * 92)
    c2 = Counter()
    for s, v in d.items():
        if v["atr"] is None or v["atr"] <= 0.15:
            continue
        f = [x for x in fails(v, 0.15) if x != "ATR不够"]
        if not f:
            c2["过关"] += 1
        else:
            for x in f:
                c2[x] += 1
    for k, n in c2.most_common():
        print(f"  {k:<18}{n:>4} 个")

    print("\n" + "=" * 92)
    print("④ 被成交额卡掉的币（放宽 ATR 后也进不来）")
    print("=" * 92)
    small = [(s, v) for s, v in d.items()
             if v["atr"] and v["atr"] > 0.15 and v["qv"] < MIN_QV]
    print(f"  共 {len(small)} 个（成交额 < 1000 万）")
    for s, v in sorted(small, key=lambda x: x[1]["qv"])[:12]:
        print(f"    {s:<16} ATR {v['atr']:>5.2f}%  成交额 {v['qv']/1e4:>8.0f} 万U")

    print("\n" + "=" * 92)
    print("⑥ 被插针卡掉的币里，有多少其实是好东西（止损距离够 + 不横盘）")
    print("=" * 92)
    import json
    gate_path = ROOT / "reports" / "pool_gate_analysis.json"
    if gate_path.exists():
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        lost = []
        for s, v in d.items():
            if v["atr"] is None or v["atr"] <= 0.15:
                continue
            if v["qv"] < MIN_QV:
                continue
            if v["spike"] is None or v["spike"] <= MAX_SPIKE:
                continue          # 没被插针卡
            g = gate.get(s)
            if not g:
                continue
            if g["stop"] >= 1.0 and (g.get("eff") or 0) >= 0.30:
                lost.append((s, v["spike"], g["stop"], g["eff"], g["fee_ratio"]))
        print(f"  被插针卡掉、但止损够+趋势好的：{len(lost)} 个")
        for s, sp, st, ef, fr in sorted(lost, key=lambda x: -x[3])[:15]:
            print(f"    {s:<16} 插针 {sp:>6.1f}倍  止损 {st:>5.2f}%  "
                  f"EFF {ef:>5.2f}  手续费占1R {fr:>4.1f}%")

    out = ROOT / "reports" / "pool_bottleneck.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {s: {k: (round(x, 4) if isinstance(x, float) else x)
             for k, x in v.items()} for s, v in d.items()},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细已写 {out}")
    spike_bad = [(s, v) for s, v in d.items()
                 if v["atr"] and v["atr"] > 0.15
                 and v["spike"] is not None and v["spike"] > MAX_SPIKE]
    print(f"  共 {len(spike_bad)} 个（插针 > 4.0 倍 ATR）")
    for s, v in sorted(spike_bad, key=lambda x: -x[1]["spike"])[:12]:
        print(f"    {s:<16} ATR {v['atr']:>5.2f}%  插针 {v['spike']:>6.2f} 倍")


if __name__ == "__main__":
    asyncio.run(main())
