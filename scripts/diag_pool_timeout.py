"""诊断：PoolTimeout 到底是代理限流还是服务内部连接泄漏。

对同一个 premiumIndex 接口，分别测：
  1. 单个请求            —— 基线
  2. 24 个并发（正好等于连接池上限）
  3. 48 个并发（超过上限）

如果并发也全成功 → 代理没问题，是服务内部有连接没释放。
如果并发就开始卡     → 代理侧有并发限制。

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/diag_pool_timeout.py
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import httpx  # noqa: E402

URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
PROXY = "http://127.0.0.1:7897"


async def one(client, i, results):
    t0 = time.perf_counter()
    try:
        r = await client.get(URL, params={"symbol": "BTCUSDT"})
        r.raise_for_status()
        results.append((i, "ok", time.perf_counter() - t0))
    except Exception as exc:  # noqa: BLE001
        results.append((i, type(exc).__name__, time.perf_counter() - t0))


async def run(n, max_conn):
    results = []
    limits = httpx.Limits(max_connections=max_conn,
                          max_keepalive_connections=max_conn // 2,
                          keepalive_expiry=90)
    async with httpx.AsyncClient(proxy=PROXY, timeout=15, limits=limits) as c:
        await asyncio.gather(*[one(c, i, results) for i in range(n)])
    ok = [r for r in results if r[1] == "ok"]
    bad = [r for r in results if r[1] != "ok"]
    times = sorted(r[2] for r in results)
    print(f"  并发 {n:>2}（池上限 {max_conn}）→ 成功 {len(ok):>2}/{n}"
          f"   耗时 中位 {times[len(times) // 2]:.2f}s 最慢 {times[-1]:.2f}s")
    if bad:
        cnt = {}
        for _, e, _t in bad:
            cnt[e] = cnt.get(e, 0) + 1
        print(f"     失败类型: {cnt}")
    return len(bad) == 0


async def main():
    print("=" * 74)
    print("测试 1：单请求基线")
    print("=" * 74)
    await run(1, 24)
    print()

    print("=" * 74)
    print("测试 2：24 并发（= 服务连接池上限 24）")
    print("=" * 74)
    ok24 = await run(24, 24)
    print()

    print("=" * 74)
    print("测试 3：48 并发（超过上限，看会不会排队/超时）")
    print("=" * 74)
    ok48 = await run(48, 24)
    print()

    print("=" * 74)
    print("测试 4：连续跑 5 轮 24 并发（看会不会累积泄漏）")
    print("=" * 74)
    allok = True
    for k in range(5):
        print(f"  第 {k + 1} 轮：", end="")
        r = await run(24, 24)
        allok = allok and r
    print()

    print("=" * 74)
    print("结论")
    print("=" * 74)
    if ok24 and ok48 and allok:
        print("  ✅ 代理侧完全没问题：24 并发、48 并发、连跑 5 轮全成功。")
        print("  → PoolTimeout 是服务内部连接没释放（连接泄漏），不是网络。")
    else:
        print("  ❌ 代理侧有并发限制，见上面失败类型。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
