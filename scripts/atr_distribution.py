"""候选池 200 个币的 5m ATR 分布统计 —— 看 ATR ≥ 0.4% 这道闸门到底拦了多少。

浩哥 2026-11-08 要求：确认 0.4% 是不是设得太松。
流量账：200 币 × 20 根 5m K 线 ≈ 400KB，一次性。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import binance, pool, quality  # noqa: E402

CONC = 8
BARS = 20


async def one(sym, sem):
    async with sem:
        try:
            bars = await binance.klines(sym, "5m", BARS)
            if len(bars) < 15:
                return sym, None
            a = quality._atr(bars, 14)
            px = bars[-1]["close"]
            if not a or not px:
                return sym, None
            return sym, a / px * 100.0
        except Exception:
            return sym, None


async def main():
    syms = pool.load()
    print(f"候选池 {len(syms)} 个币，拉 5m ATR（并发 {CONC}）...")
    t0 = time.time()
    sem = asyncio.Semaphore(CONC)
    res = await asyncio.gather(*[one(s, sem) for s in syms])
    ok = [(s, v) for s, v in res if v is not None]
    fail = [s for s, v in res if v is None]
    print(f"完成 {len(ok)}/{len(syms)}，耗时 {time.time()-t0:.1f}s，失败 {len(fail)}\n")

    if not ok:
        print("没有取到数据，退出")
        return

    vals = sorted(v for _, v in ok)
    n = len(vals)

    def pct_threshold(p):
        idx = min(n - 1, int(n * p / 100))
        return vals[idx]

    print("=" * 88)
    print(f"候选池 {n} 个币的 5m ATR% 分布")
    print("=" * 88)
    print(f"  最低       {vals[0]:.3f}%   ({min(ok, key=lambda x: x[1])[0]})")
    print(f"  25% 分位   {pct_threshold(25):.3f}%")
    print(f"  中位数     {pct_threshold(50):.3f}%")
    print(f"  75% 分位   {pct_threshold(75):.3f}%")
    print(f"  最高       {vals[-1]:.3f}%   ({max(ok, key=lambda x: x[1])[0]})")

    print("\n" + "=" * 88)
    print("分档：哪些会被 0.4% 拦住")
    print("=" * 88)
    buckets = [(0, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.6),
               (0.6, 0.8), (0.8, 1.2), (1.2, 2.0), (2.0, 99)]
    for lo, hi in buckets:
        grp = [(s, v) for s, v in ok if lo <= v < hi]
        bar = "█" * len(grp)
        mark = "  ← 被 0.4 拦在池外" if hi <= 0.4 else ""
        print(f"  {lo:>4.1f}~{hi:<5.1f}%  {len(grp):>3} 个  {bar}{mark}")

    blocked = [(s, v) for s, v in ok if v <= 0.40]
    passed = [(s, v) for s, v in ok if v > 0.40]
    print("\n" + "=" * 88)
    print("结论")
    print("=" * 88)
    print(f"  过 ATR > 0.40% 的：{len(passed):>3} 个  ({len(passed)/n*100:.1f}%)")
    print(f"  被拦在池外的：    {len(blocked):>3} 个  ({len(blocked)/n*100:.1f}%)")
    print()
    print("  如果门槛调高会拦掉多少：")
    for thr in (0.4, 0.5, 0.6, 0.8, 1.0, 1.2):
        p = sum(1 for _, v in ok if v > thr)
        print(f"    > {thr:.1f}%  →  过 {p:>3} 个 ({p/n*100:>5.1f}%)，"
              f"拦掉 {n-p:>3} 个")
    print()
    print("  如果门槛调低会多放进来多少：")
    for thr in (0.3, 0.2):
        p = sum(1 for _, v in ok if v > thr)
        print(f"    > {thr:.1f}%  →  过 {p:>3} 个 ({p/n*100:>5.1f}%)")

    if blocked:
        print(f"\n  被 0.4 拦住的币（按 ATR 升序，前 20 个）：")
        for s, v in sorted(blocked, key=lambda x: x[1])[:20]:
            print(f"    {s:<16} {v:.3f}%")

    out = ROOT / "reports" / "atr_distribution.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {"n": n, "vals": {s: round(v, 4) for s, v in ok}, "fail": fail},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细已写 {out}")


if __name__ == "__main__":
    asyncio.run(main())
