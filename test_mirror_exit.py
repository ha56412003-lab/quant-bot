"""镜像出场单元测试（2026-11-08 浩哥定案的出场方案）

覆盖：
  做多/做空 各自的 3 个条件，逐条缺失都必须不出场
  半仓「再赚 1R 全平」
  同一根 K 线只判一次（防 0.5 秒反复触发）

用法：python test_mirror_exit.py
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

import backend.strategies as S


class _FakeDB:
    """落库打桩：单测不碰真库。"""
    def __init__(self, boom=False):
        self.boom = boom
        self.writes = []

    def update_position(self, pid, **kw):
        if self.boom:
            raise RuntimeError("模拟写库失败")
        self.writes.append((pid, kw))


S.db = _FakeDB()


class FakeCtx:
    def __init__(self, price):
        self._price = price

    def price(self, sym):
        return self._price

    async def klines(self, sym, iv, n):
        # 固定返回 200 根，这样方法里的 i = len(ks) - 2 = 198，
        # 与 patch_ind 构造的 200 长指标数组索引对齐。
        return [{"ts": i, "open": 1, "high": 1, "low": 1, "close": 1} for i in range(200)]


def make_strategy():
    st = S.S15CeZlsma.__new__(S.S15CeZlsma)
    st.p = {
        "kline_interval": "15m",
        "ce_len": 1, "ce_mult": 2.0, "zlsma_len": 50,
        "use_close": True, "use_ha": True,
        "mirror_exit": True,
        "bbw_pct_min": 70.0,
        "half_at_r": 2.0, "half_tp_r": 1.0,
    }
    return st


def patch_ind(zl=100.0, ha_open=100.0, ha_close=100.0,
              dir_now=1, dir_prev=1, bbw=80.0):
    """把 _ce_zlsma 换成返回固定指标，返回值长度 200（i = len-2 = 198）。"""
    n = 200
    ind = {
        "zlsma": [zl] * n,
        "atr": [1.0] * n,
        "dir": [dir_prev] * (n - 2) + [dir_now, dir_now],
        "close": [ha_close] * n,
        "open": [ha_open] * n,
        "bbw": [1.0] * n,
        "bbw_pct": [bbw] * n,
    }
    S._ce_zlsma = lambda *a, **k: ind
    S._S15_ZL_CACHE.clear()      # 每个用例必须从干净缓存开始，否则会命中上一个用例的指标
    S._S15_MIRROR_STATE.clear()  # 同理，逐仓位去重状态也必须清
    return ind


def pos(side="long", entry=100.0, stop=98.0, half=False):
    return {"id": 1, "symbol": "TESTUSDT", "side": side,
            "entry_price": entry, "stop_price": stop,
            "half_closed": 1 if half else 0}


CASES = []


def case(name, expect_hit):
    def deco(fn):
        CASES.append((name, expect_hit, fn))
        return fn
    return deco


# ── 做多 ──────────────────────────────────────────────────────────
@case("多头·CE翻空+HA阴线跌破ZLSMA+BBW80 → 出场", True)
async def _(st):
    patch_ind(zl=100.0, ha_open=100.5, ha_close=99.0, dir_now=-1, dir_prev=1, bbw=80.0)
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(101.0), pos("long"))


@case("多头·BBW只50（不够宽）→ 不出场", False)
async def _(st):
    patch_ind(zl=100.0, ha_open=100.5, ha_close=99.0, dir_now=-1, dir_prev=1, bbw=50.0)
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(101.0), pos("long"))


@case("多头·CE没翻转（一直多）→ 不出场", False)
async def _(st):
    patch_ind(zl=100.0, ha_open=100.5, ha_close=99.0, dir_now=1, dir_prev=1, bbw=80.0)
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(101.0), pos("long"))


@case("多头·HA是阳线（收>开）→ 不出场", False)
async def _(st):
    patch_ind(zl=100.0, ha_open=99.0, ha_close=99.5, dir_now=-1, dir_prev=1, bbw=80.0)
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(101.0), pos("long"))


@case("多头·HA收在ZLSMA上方 → 不出场", False)
async def _(st):
    patch_ind(zl=95.0, ha_open=100.5, ha_close=99.0, dir_now=-1, dir_prev=1, bbw=80.0)
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(101.0), pos("long"))


# ── 做空 ──────────────────────────────────────────────────────────
@case("空头·CE翻多+HA阳线站上ZLSMA+BBW80 → 出场", True)
async def _(st):
    patch_ind(zl=100.0, ha_open=99.5, ha_close=101.0, dir_now=1, dir_prev=-1, bbw=80.0)
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(99.0), pos("short", entry=100.0, stop=102.0))


@case("空头·CE没翻转（一直空）→ 不出场", False)
async def _(st):
    patch_ind(zl=100.0, ha_open=99.5, ha_close=101.0, dir_now=-1, dir_prev=-1, bbw=80.0)
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(99.0), pos("short", entry=100.0, stop=102.0))


@case("空头·HA是阴线 → 不出场", False)
async def _(st):
    patch_ind(zl=100.0, ha_open=101.0, ha_close=99.5, dir_now=1, dir_prev=-1, bbw=80.0)
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(99.0), pos("short", entry=100.0, stop=102.0))


# ── 半仓：再赚 1R 全平 ────────────────────────────────────────────
@case("半仓·现3.1R（平半@2R之后又赚1.1R）→ 全平", True)
async def _(st):
    patch_ind(zl=100.0, ha_open=100.0, ha_close=100.0, dir_now=1, dir_prev=1, bbw=10.0)
    S._S15_MIRROR_STATE.clear()
    # entry=100 stop=98 → 1R=2；3.1R → 价格 106.2
    return await st._mirror_exit(FakeCtx(106.2), pos("long", half=True))


@case("半仓·现2.5R（只多赚0.5R）→ 不出场", False)
async def _(st):
    patch_ind(zl=100.0, ha_open=100.0, ha_close=100.0, dir_now=1, dir_prev=1, bbw=10.0)
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(105.0), pos("long", half=True))


# ── 去重 ──────────────────────────────────────────────────────────
@case("同一根K线第二次调用 → 不再触发", False)
async def _(st):
    patch_ind(zl=100.0, ha_open=100.5, ha_close=99.0, dir_now=-1, dir_prev=1, bbw=80.0)
    S._S15_MIRROR_STATE.clear()
    ctx = FakeCtx(101.0)
    p = pos("long")
    first = await st._mirror_exit(ctx, p)
    second = await st._mirror_exit(ctx, p)
    return second if (first and not second) else first


# ── 落库失败不能拖垮出场判断 ──────────────────────────────────────
@case("写库抛异常 → 出场判断照常生效", True)
async def _(st):
    patch_ind(zl=100.0, ha_open=100.5, ha_close=99.0, dir_now=-1, dir_prev=1, bbw=80.0)
    old = S.db
    S.db = _FakeDB(boom=True)
    try:
        return await st._mirror_exit(FakeCtx(101.0), pos("long"))
    finally:
        S.db = old


# ── 数据不合法 ────────────────────────────────────────────────────
@case("止损价=0（脏数据）→ 不崩、不出场", False)
async def _(st):
    patch_ind()
    S._S15_MIRROR_STATE.clear()
    return await st._mirror_exit(FakeCtx(101.0), pos("long", stop=0.0))


def main():
    ok = fail = 0
    print("=" * 78)
    print("镜像出场单元测试")
    print("=" * 78)
    for name, expect, fn in CASES:
        st = make_strategy()
        try:
            got = asyncio.run(fn(st))
            hit = got is not None
            passed = hit == expect
            detail = f" → {got}" if got else " → None"
        except Exception as e:  # noqa: BLE001
            hit = None
            passed = False
            detail = f" → 异常 {type(e).__name__}: {e}"
        if passed:
            ok += 1
            print(f"  PASS  {name}{detail}")
        else:
            fail += 1
            print(f"  FAIL  {name}  期望 hit={expect}{detail}")
    print("-" * 78)
    print(f"  通过 {ok} / {ok + fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
