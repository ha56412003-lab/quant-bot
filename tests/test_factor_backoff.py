"""factor 失败退避机制测试（2026-11-09 上云后雷达饿死事故）。

背景：collect_due() 的到期判据是 `now - factor_ts >= interval`，失败不写 ts
→ 下一 tick 又到期 → 上游限流时无限重排 64 个请求，占满 binance 主池
（24 槽位），雷达排在队尾 PoolTimeout → 一轮 10 分钟 → 界面「有持仓却空仓」。

这里验证退避的三条硬规则：
  1. 失败一次 → 该 (factor, symbol) 进入退避窗口
  2. 退避窗口内 → collect_due() 不再给它排队
  3. 连续失败 → 窗口按 60/180/600 秒递增并封顶；成功后立即清零
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import factors  # noqa: E402

FAILS = []
OK = []


def check(name, cond, extra=""):
    (OK if cond else FAILS).append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} {name}{(' — ' + extra) if extra else ''}")


def _reset():
    factors._FAIL_COUNT.clear()
    factors._FAIL_UNTIL.clear()
    factors._IN_FLIGHT.clear()


async def main():
    print("=== 1. _penalize 设置退避窗口 ===")
    _reset()
    key = ("px", "BTCUSDT")
    t0 = time.time()
    factors._penalize(key)
    until = factors._FAIL_UNTIL.get(key, 0)
    check("第 1 次失败写 _FAIL_UNTIL", until > t0, f"窗口 {until - t0:.0f}s")
    check("第 1 次退避 = 60s", 55 <= until - t0 <= 65, f"实际 {until - t0:.1f}s")
    check("失败计数 = 1", factors._FAIL_COUNT.get(key) == 1)

    print("\n=== 2. 连续失败递增并封顶 ===")
    _reset()
    factors._penalize(key)
    factors._penalize(key)
    d2 = factors._FAIL_UNTIL[key] - time.time()
    check("第 2 次退避 = 180s", 175 <= d2 <= 185, f"实际 {d2:.1f}s")
    factors._penalize(key)
    d3 = factors._FAIL_UNTIL[key] - time.time()
    check("第 3 次退避 = 600s", 595 <= d3 <= 605, f"实际 {d3:.1f}s")
    for _ in range(6):
        factors._penalize(key)
    d9 = factors._FAIL_UNTIL[key] - time.time()
    check("第 9 次仍封顶 600s", 595 <= d9 <= 605, f"实际 {d9:.1f}s")
    check("失败计数持续累加 = 9", factors._FAIL_COUNT.get(key) == 9)

    print("\n=== 3. _clear_fail 成功后清零 ===")
    factors._clear_fail(key)
    check("_FAIL_COUNT 已清", key not in factors._FAIL_COUNT)
    check("_FAIL_UNTIL 已清", key not in factors._FAIL_UNTIL)

    print("\n=== 4. _run_one: fetcher 抛异常 → 退避 ===")
    _reset()

    async def boom(_sym):
        raise RuntimeError("upstream 429")

    await factors._run_one("px", "ETHUSDT", boom)
    k = ("px", "ETHUSDT")
    check("异常后退避窗口已设", factors._FAIL_UNTIL.get(k, 0) > time.time())
    check("异常后 _IN_FLIGHT 已清", k not in factors._IN_FLIGHT)

    print("\n=== 5. _run_one: fetcher 返回 None → 也退避 ===")
    _reset()

    async def empty(_sym):
        return None

    await factors._run_one("px", "SOLUSDT", empty)
    k2 = ("px", "SOLUSDT")
    check("返回 None 也退避", factors._FAIL_UNTIL.get(k2, 0) > time.time(),
          "上游限流返回空不能每 0.5 秒重打")

    print("\n=== 6. _run_one: 成功 → 清零退避 ===")
    _reset()
    k3 = ("px", "ADAUSDT")
    factors._penalize(k3)          # 先制造一次失败

    async def good(_sym):
        return {"v": 1.0}

    # db.set_factor 打桩，避免碰生产库
    orig_set = factors.db.set_factor
    factors.db.set_factor = lambda *a, **kw: None
    try:
        await factors._run_one("px", "ADAUSDT", good)
    finally:
        factors.db.set_factor = orig_set
    check("成功后 _FAIL_COUNT 清零", k3 not in factors._FAIL_COUNT)
    check("成功后 _FAIL_UNTIL 清零", k3 not in factors._FAIL_UNTIL)
    check("成功后 _IN_FLIGHT 清理", k3 not in factors._IN_FLIGHT)

    print("\n=== 7. collect_due 跳过退避中的因子 ===")
    _reset()
    now = time.time()
    blocked = ("zzz_fake", "BTCUSDT")
    factors._FAIL_UNTIL[blocked] = now + 300     # 退避 5 分钟

    # 把 zzz_fake 注册进 REGISTRY 并让它"到期"，验证不会排队
    orig_registry = factors.REGISTRY
    orig_universe = factors.config.UNIVERSE
    orig_get_ts = factors.db.get_factor_ts

    async def fetcher(_sym):
        return {"v": 1.0}

    factors.REGISTRY = [("zzz_fake", 300, True, fetcher, "t", "s", "t")]
    factors.config.UNIVERSE = ["BTCUSDT"]
    factors.db.get_factor_ts = lambda n, s: 0      # 永远"到期"
    factors.is_enabled = lambda _n: True
    factors.get_interval = lambda _n, d: 300
    try:
        n = await asyncio.wait_for(factors.collect_due(), timeout=5)
        check("退避中的因子不排队", n == 0, f"collect_due 返回 {n}")
        check("退避中的因子未进 _IN_FLIGHT", blocked not in factors._IN_FLIGHT)
    finally:
        factors.REGISTRY = orig_registry
        factors.config.UNIVERSE = orig_universe
        factors.db.get_factor_ts = orig_get_ts

    print("\n" + "=" * 50)
    if FAILS:
        print(f"✗ 失败 {len(FAILS)} 项 / 共 {len(OK) + len(FAILS)} 项")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print(f"✓ 全部通过（{len(OK)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
