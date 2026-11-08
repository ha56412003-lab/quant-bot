"""全池扫描：用「止损距离」替代 ATR + 趋势效率筛横盘，看不同门槛下剩多少币。

浩哥 2026-11-08：不想要 ATR，目的是剔除横盘震荡。
方案：两道闸门
  第 1 道  止损距离 >= X%   → 管手续费（替代 ATR，但比 ATR 精确）
  第 2 道  趋势效率 >= Y    → 管横盘

流量账：200 币 × 200 根 15m ≈ 4MB，一次性。
"""
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import binance, pool  # noqa: E402
from backend.strategies import _ce_zlsma  # noqa: E402

CONC = 8
LB = 20          # swing_lookback
WIN_EFF = 96     # 24 小时 = 96 根 15m
FEE = 0.001      # 一买一卖 0.1%


async def one(sym, sem):
    async with sem:
        try:
            ks = await binance.klines(sym, "15m", 200)
            if len(ks) < 150:
                return sym, None
            ind = _ce_zlsma(ks)

            # ① 止损距离：最近 40 根，每根开多/开空的止损距离
            dists = []
            for i in range(len(ks) - 40, len(ks) - 1):
                px = float(ks[i]["close"])
                if px <= 0:
                    continue
                for d in (1, -1):
                    if d == 1:
                        stop = min(ind["low_raw"][i - LB + 1:i + 1])
                        r = (px - stop) / px
                    else:
                        stop = max(ind["high_raw"][i - LB + 1:i + 1])
                        r = (stop - px) / px
                    if 0 < r < 1:
                        dists.append(r * 100)
            if not dists:
                return sym, None
            stop_med = statistics.median(dists)

            # ② 趋势效率：最近 96 根
            seg = ks[-WIN_EFF:]
            hi = max(b["high"] for b in seg)
            lo = min(b["low"] for b in seg)
            amp = (hi - lo) / lo if lo > 0 else 0
            eff = abs(seg[-1]["close"] / seg[0]["close"] - 1) / amp if amp > 0 else None

            return sym, {"stop": stop_med, "eff": eff,
                         "fee_ratio": FEE / (stop_med / 100) * 100}
        except Exception:
            return sym, None


async def main():
    syms = pool.load()
    print(f"全池 {len(syms)} 个币，算止损距离 + 趋势效率（并发 {CONC}）...")
    t0 = time.time()
    sem = asyncio.Semaphore(CONC)
    res = await asyncio.gather(*[one(s, sem) for s in syms])
    d = {s: v for s, v in res if v}
    print(f"完成 {len(d)}/{len(syms)}，耗时 {time.time()-t0:.1f}s\n")

    stops = sorted(v["stop"] for v in d.values())
    effs = sorted(v["eff"] for v in d.values() if v["eff"] is not None)
    n = len(d)

    print("=" * 92)
    print("① 止损距离分布（决定手续费占比）")
    print("=" * 92)
    for p in (10, 25, 50, 75, 90):
        print(f"  {p:>3}% 分位  {stops[min(n-1, int(n*p/100))]:>6.2f}%")
    print(f"  最低 {stops[0]:.2f}%   最高 {stops[-1]:.2f}%")

    print("\n" + "=" * 92)
    print("② 趋势效率分布（决定横盘）")
    print("=" * 92)
    m = len(effs)
    for p in (10, 25, 50, 75, 90):
        print(f"  {p:>3}% 分位  {effs[min(m-1, int(m*p/100))]:>6.3f}")

    print("\n" + "=" * 92)
    print("③ 如果第 1 道门槛换成『止损距离 >= X%』（手续费占比 = 0.1%÷止损距离）")
    print("=" * 92)
    print(f"{'门槛':<12}{'剩币数':>9}{'手续费≤':>10}   说明")
    print("-" * 92)
    for thr in (0.5, 0.7, 1.0, 1.5, 2.0, 3.0):
        keep = sum(1 for v in d.values() if v["stop"] >= thr)
        fee_share = FEE / (thr / 100) * 100      # 手续费占 1R 的百分比
        print(f"  >= {thr:<6.1f}%{keep:>8} 个{fee_share:>9.1f}%   "
              f"（现在 ATR 0.4% 只留 50 个）")

    print("\n" + "=" * 92)
    print("④ 两道闸门叠加：止损距离 + 趋势效率")
    print("=" * 92)
    print(f"{'组合':<32}{'剩币数':>9}")
    print("-" * 92)
    for st in (0.7, 1.0, 1.5):
        for ef in (0.0, 0.30, 0.40, 0.55):
            keep = sum(1 for v in d.values()
                       if v["stop"] >= st and (v["eff"] or 0) >= ef)
            tag = f"止损≥{st}% 且 EFF≥{ef}"
            print(f"  {tag:<30}{keep:>8} 个")

    print("\n" + "=" * 92)
    print("⑤ 现在的池子（ATR≥0.4）在趋势效率上表现如何")
    print("=" * 92)
    now = ["MEGAUSDT", "ERAUSDT", "SWARMSUSDT", "METUSDT", "BMTUSDT", "GTCUSDT",
           "ACTUSDT", "LYNUSDT", "AIOTUSDT", "BTWUSDT", "RSRUSDT", "KOMAUSDT",
           "ONEUSDT"]
    bad = 0
    for s in now:
        v = d.get(s)
        if not v:
            continue
        e = v["eff"] or 0
        if e <= 0.30:
            bad += 1
    print(f"  当前 13 个池内币中，趋势效率 ≤0.30（横盘）的有 {bad} 个")
    print(f"  → 加 EFF≥0.30 这道闸门，这 {bad} 个会被挡在池外")

    out = ROOT / "reports" / "pool_gate_analysis.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {s: {k: round(x, 4) if isinstance(x, float) else x
             for k, x in v.items()} for s, v in d.items()},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细已写 {out}")


if __name__ == "__main__":
    asyncio.run(main())
