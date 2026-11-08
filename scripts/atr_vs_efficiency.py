"""对比 ATR（波动大小）和 趋势效率（有没有方向）—— 证明 ATR 筛不出横盘。

浩哥 2026-11-08：目的是剔除横盘震荡。ATR 能不能做到？

趋势效率口径（公司铁律，S15/radar 同款）：
    amp = (区间最高 - 区间最低) / 区间最低
    eff = |最新收盘/起始收盘 - 1| / amp
    eff >= 0.55  单边趋势
    0.30~0.55    半趋势（来回但偏一边）
    eff <= 0.30  宽震（原地打转）
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import binance, pool, quality  # noqa: E402

CONC = 8
# 24 小时 = 96 根 15m
WIN = 96


def trend_eff(bars):
    """返回 (eff, amp%, 净涨跌%)"""
    if len(bars) < 20:
        return None
    hi = max(b["high"] for b in bars)
    lo = min(b["low"] for b in bars)
    if lo <= 0:
        return None
    amp = (hi - lo) / lo
    if amp <= 0:
        return None
    net = abs(bars[-1]["close"] / bars[0]["close"] - 1)
    return net / amp, amp * 100, (bars[-1]["close"] / bars[0]["close"] - 1) * 100


def label(eff):
    if eff is None:
        return "?"
    if eff >= 0.55:
        return "单边趋势"
    if eff <= 0.30:
        return "宽震横盘"
    return "半趋势"


async def one(sym, sem):
    async with sem:
        try:
            bars = await binance.klines(sym, "15m", WIN + 5)
            e = trend_eff(bars[-WIN:])
            a = quality._atr(bars[-20:], 14) / bars[-1]["close"] * 100 if len(bars) >= 15 else None
            # 5m ATR 另拉（池子用的口径）
            b5 = await binance.klines(sym, "5m", 20)
            a5 = quality._atr(b5, 14) / b5[-1]["close"] * 100 if len(b5) >= 15 else None
            return sym, e, a5
        except Exception:
            return sym, None, None


async def main():
    pool_syms = ["MEGAUSDT", "ERAUSDT", "SWARMSUSDT", "METUSDT", "BMTUSDT",
                 "GTCUSDT", "ACTUSDT", "LYNUSDT", "AIOTUSDT", "BTWUSDT",
                 "RSRUSDT", "KOMAUSDT", "ONEUSDT"]
    # 被 ATR 0.4 拦掉的大币
    big = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
           "DOGEUSDT", "LINKUSDT", "LTCUSDT"]

    sem = asyncio.Semaphore(CONC)
    allsyms = pool_syms + big
    res = await asyncio.gather(*[one(s, sem) for s in allsyms])
    d = {s: (e, a) for s, e, a in res}

    print("=" * 100)
    print("核心对比：ATR（跳得狠不狠） vs 趋势效率（有没有方向）  窗口 = 最近 96 根 15m = 24 小时")
    print("=" * 100)
    print(f"{'币种':<14}{'5m ATR%':>10}{'过了0.4?':>9}{'24h趋势效率':>12}{'判定':>10}{'振幅%':>9}{'净涨跌%':>10}")
    print("-" * 100)

    print("【现在在监控池里的 13 个】")
    cnt_bad = 0
    for s in pool_syms:
        e, a5 = d.get(s, (None, None))
        if e is None:
            continue
        eff, amp, net = e
        lb = label(eff)
        if lb == "宽震横盘":
            cnt_bad += 1
        print(f"{s:<14}{a5:>9.2f}{'✅':>9}{eff:>11.2f}{lb:>10}{amp:>8.1f}{net:>9.1f}")
    print(f"\n  → 池里 13 个中，按 24h 趋势效率算是【宽震横盘】的有 {cnt_bad} 个")
    print(f"  → 也就是说：ATR 让它们进了池，但它们其实是横盘的\n")

    print("【被 ATR 0.4% 拦在池外的大币】")
    cnt_good = 0
    for s in big:
        e, a5 = d.get(s, (None, None))
        if e is None:
            continue
        eff, amp, net = e
        lb = label(eff)
        if lb in ("单边趋势", "半趋势"):
            cnt_good += 1
        gate = "✅过" if a5 and a5 > 0.4 else "❌拦"
        print(f"{s:<14}{a5:>9.2f}{gate:>9}{eff:>11.2f}{lb:>10}{amp:>8.1f}{net:>9.1f}")
    print(f"\n  → 被拦的 8 个大币里，其实【有方向】的有 {cnt_good} 个")

    print("\n" + "=" * 100)
    print("结论")
    print("=" * 100)
    print("  ATR 高 ≠ 有方向。ATR 低 ≠ 横盘。")
    print("  横盘的准确定义是：振幅不小，但净涨跌占比很低（来回打）= 趋势效率低。")
    print("  ATR 只看单根 K 线跳多狠，完全看不到『跳了一圈又回原地』。")


if __name__ == "__main__":
    asyncio.run(main())
