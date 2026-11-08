# -*- coding: utf-8 -*-
"""新增两道闸的单元测试（2026-11-08 浩哥批准落地）

  闸一：进场偏离 —— 进场价离 ZLSMA 至少 min_dev_pct%（strategies.py evaluate ⑥）
  闸二：快速失败 —— 进场 quickfail_hours 内峰顶没到 quickfail_min_r R 就平（_quick_fail）

要验的六件事：
  A1 偏离不够 → 不开仓
  A2 偏离够   → 开仓
  A3 min_dev_pct=0（关闭）→ 偏离再小也开仓（回滚路径必须通）
  A4 边界 = 门槛整值 → 放过（判据是 < 才挡）
  A5 空头方向同样生效（多空对称）
  B1 未满 4 小时 → 不平
  B2 满 4 小时 + 峰顶低 → 平
  B3 满 4 小时 + 峰顶够 → 不平
  B4 quickfail_hours=0（关闭）→ 不平
  B5 已平过半仓（峰顶必然 >=2R）→ 不被误伤
  B6 优先级：4h+峰顶低+镜像条件也满足 → 必须报「快速失败」而不是镜像出场

用法：python test_new_gates.py
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

import backend.strategies as S


class _FakeDB:
    def __init__(self):
        self.writes = []

    def update_position(self, pid, **kw):
        self.writes.append((pid, kw))


S.db = _FakeDB()

PASS = []
FAIL = []


def chk(cond, label, extra=""):
    if cond:
        PASS.append(label)
        print(f"  PASS  {label}")
    else:
        FAIL.append(label)
        print(f"  FAIL  {label}   {extra}")


class FakeCtx:
    def __init__(self, px, ks):
        self._px = px
        self._ks = ks

    def has_position(self, sym, sid):
        return False

    async def klines(self, sym, iv, n):
        return self._ks

    def price(self, sym):
        return self._px


def make_ks(n=220, price=100.0):
    """真实 K 线：最后一根已收盘、ts 跟着当前窗口走（evaluate 会在意这个）。"""
    bar = 900
    cur = int(time.time() // bar) * bar
    base = cur - (n - 1) * bar
    return [{"ts": base + i * bar, "open": price, "high": price * 1.01,
             "low": price * 0.99, "close": price} for i in range(n)]


def patch_ind(zl=100.0, ha_open=100.0, ha_close=100.0, dir_now=1, dir_prev=-1,
              bbw=80.0, atr=1.0, n=220):
    """固定指标。i = len(ks)-2 = 218，所以 dir[218] 当前、dir[217] 上一根。"""
    d = [dir_prev] * n
    d[n - 2] = dir_now
    ind = {
        "zlsma": [zl] * n,
        "open": [ha_open] * n,
        "close": [ha_close] * n,
        "high": [ha_close] * n,
        "low": [ha_close] * n,
        "dir": d,
        "atr": [atr] * n,
        "bbw": [10.0] * n,
        "bbw_pct": [bbw] * n,
        "low_raw": [ha_close * 0.98] * n,
        "high_raw": [ha_close * 1.02] * n,
    }
    orig = S._ce_zlsma
    S._ce_zlsma = lambda *a, **k: ind
    return orig


def make_strategy(**over):
    st = S.S15CeZlsma.__new__(S.S15CeZlsma)
    st.p = {
        "kline_interval": "15m",
        "ce_len": 1, "ce_mult": 2.0, "zlsma_len": 50,
        "use_close": True, "use_ha": True,
        "swing_lookback": 20, "min_atr_pct": 0, "bbw_pct_min": 70.0,
        "max_risk_pct": 30.0, "fee_rate": 0.0005, "slip_pct": 0.0,
        "half_at_r": 2.0, "half_tp_r": 1.0,
        "min_dev_pct": 0.5, "quickfail_hours": 4.0, "quickfail_min_r": 0.5,
    }
    st.p.update(over)
    st.sid = "S15_CE_ZLSMA"
    st.max_hold = 16 * 3600
    st.syms = lambda ctx: ["TESTUSDT"]
    return st


# ══════════════════════════════════════════════════════
#  A 组：进场偏离闸
# ══════════════════════════════════════════════════════
def test_entry_dev():
    print("\n" + "=" * 78)
    print("A 组：进场偏离闸（进场价离 ZLSMA >= min_dev_pct%）")
    print("=" * 78)

    async def run(entry_px, zl, min_dev, side="long"):
        S._LAST_BAR.clear()
        S._GATE_STATE.clear()
        ks = make_ks(price=entry_px)
        ctx = FakeCtx(entry_px, ks)
        st = make_strategy(min_dev_pct=min_dev)
        if side == "long":
            # 多头：HA 阳线、站在均线上方、CE 由空翻多
            orig = patch_ind(zl=zl, ha_open=entry_px * 0.995,
                             ha_close=entry_px, dir_now=1, dir_prev=-1)
        else:
            orig = patch_ind(zl=zl, ha_open=entry_px * 1.005,
                             ha_close=entry_px, dir_now=-1, dir_prev=1)
        try:
            return await st.evaluate(ctx)
        finally:
            S._ce_zlsma = orig

    # A1 偏离 0.2% < 0.5% → 不开
    sig = asyncio.run(run(entry_px=100.2, zl=100.0, min_dev=0.5))
    chk(len(sig) == 0, "A1 偏离 0.2% < 门槛 0.5% → 不开仓", f"实际 {len(sig)} 个信号")

    # A2 偏离 0.8% > 0.5% → 开
    sig = asyncio.run(run(entry_px=100.8, zl=100.0, min_dev=0.5))
    chk(len(sig) == 1, "A2 偏离 0.8% > 门槛 0.5% → 开仓", f"实际 {len(sig)} 个信号")

    # A3 关闭（0）→ 偏离再小也开
    sig = asyncio.run(run(entry_px=100.05, zl=100.0, min_dev=0.0))
    chk(len(sig) == 1, "A3 min_dev_pct=0 关闭 → 偏离 0.05% 也开仓", f"实际 {len(sig)} 个信号")

    # A4 边界：偏离正好 0.5% → 放过（判据是 < 才挡）
    sig = asyncio.run(run(entry_px=100.5, zl=100.0, min_dev=0.5))
    chk(len(sig) == 1, "A4 偏离正好 0.5% → 放过（判据 < 才挡）", f"实际 {len(sig)} 个信号")

    # A5 空头同样生效
    sig_low = asyncio.run(run(entry_px=99.8, zl=100.0, min_dev=0.5, side="short"))
    chk(len(sig_low) == 0, "A5a 空头偏离 0.2% → 不开仓", f"实际 {len(sig_low)}")
    sig_ok = asyncio.run(run(entry_px=99.2, zl=100.0, min_dev=0.5, side="short"))
    chk(len(sig_ok) == 1, "A5b 空头偏离 0.8% → 开仓", f"实际 {len(sig_ok)}")

    # A6 诊断表要能说清卡在哪
    S._LAST_BAR.clear()
    S._GATE_STATE.clear()
    ks = make_ks(price=100.2)
    st = make_strategy()
    orig = patch_ind(zl=100.0, ha_open=99.5, ha_close=100.2, dir_now=1, dir_prev=-1)
    try:
        asyncio.run(st.evaluate(FakeCtx(100.2, ks)))
    finally:
        S._ce_zlsma = orig
    g = S._GATE_STATE.get("TESTUSDT") or {}
    chk("离均线" in str(g.get("why", "")),
        "A6 诊断表写明「离均线仅X%」", f"why={g.get('why')}")
    chk(g.get("dev") is not None, "A7 诊断表带 dev 字段（供前端显示）", f"dev={g.get('dev')}")


# ══════════════════════════════════════════════════════
#  B 组：快速失败闸
# ══════════════════════════════════════════════════════
def test_quick_fail():
    print("\n" + "=" * 78)
    print("B 组：快速失败闸（进场 4h 内峰顶没到 0.5R 就平）")
    print("=" * 78)

    def mkpos(opened_h_ago, entry=100.0, stop=99.0, side="long", half=0):
        return {
            "id": 1, "symbol": "TESTUSDT", "side": side,
            "entry_price": entry, "stop_price": stop,
            "opened_at": time.time() - opened_h_ago * 3600,
            "peak_r": 0.0, "mfe_pct": 0.0, "half_closed": half,
        }

    async def run(pos, px, hours=4.0, min_r=0.5):
        S._S15_POS_STATE.clear()
        S._S15_ZL_CACHE.clear()
        S._S15_MIRROR_STATE.clear()
        st = make_strategy(quickfail_hours=hours, quickfail_min_r=min_r)
        return await st.check_exit(FakeCtx(px, make_ks(price=px)), pos)

    # B1 只过了 1 小时 → 不平
    r = asyncio.run(run(mkpos(1.0), px=100.2))
    chk(r is None, "B1 持仓 1h（<4h）→ 不平仓", f"实际 {r}")

    # B2 过了 5 小时 + 峰顶只有 0.2R → 平
    r = asyncio.run(run(mkpos(5.0), px=100.2))
    chk(r is not None and "快速失败" in str(r), "B2 持仓 5h + 峰顶 0.2R → 平仓",
        f"实际 {r}")

    # B3 峰顶够（0.8R）→ 不平
    pos = mkpos(5.0)
    pos["peak_r"] = 0.8
    r = asyncio.run(run(pos, px=100.2))
    chk(r is None or "快速失败" not in str(r), "B3 持仓 5h + 峰顶 0.8R → 不快速失败",
        f"实际 {r}")

    # B4 关闭（hours=0）→ 不平
    r = asyncio.run(run(mkpos(5.0), px=100.2, hours=0.0))
    chk(r is None, "B4 quickfail_hours=0 关闭 → 不平仓", f"实际 {r}")

    # B5 已平半（half_closed=1，峰顶必然>=2R）→ 不误伤
    pos = mkpos(5.0)
    pos["half_closed"] = 1
    pos["peak_r"] = 2.2
    r = asyncio.run(run(pos, px=100.2))
    chk(r is None or "快速失败" not in str(r), "B5 已平半仓 → 不被快速失败误伤",
        f"实际 {r}")

    # B6 空头方向同样生效
    pos = mkpos(5.0, entry=100.0, stop=101.0, side="short")
    r = asyncio.run(run(pos, px=99.8))
    chk(r is not None and "快速失败" in str(r), "B6 空头 5h + 峰顶 0.2R → 平仓",
        f"实际 {r}")

    # B7 优先级：镜像条件也满足时，必须报「快速失败」
    S._S15_POS_STATE.clear()
    S._S15_ZL_CACHE.clear()
    S._S15_MIRROR_STATE.clear()
    orig = patch_ind(zl=101.0, ha_open=100.5, ha_close=99.0,
                     dir_now=-1, dir_prev=1)   # CE 由多翻空 + HA 阴线跌破均线
    try:
        st = make_strategy()
        pos = mkpos(5.0)
        r = asyncio.run(st.check_exit(FakeCtx(99.0, make_ks(price=99.0)), pos))
    finally:
        S._ce_zlsma = orig
    chk(r is not None and "快速失败" in str(r),
        "B7 两闸同时满足 → 优先报「快速失败」（顺序正确）", f"实际 {r}")

    # B8 峰顶刚够门槛（0.5R 整）→ 不平（判据是 peak < min_r 才平）
    pos = mkpos(5.0)
    pos["peak_r"] = 0.5
    r = asyncio.run(run(pos, px=100.2))
    chk(r is None or "快速失败" not in str(r), "B8 峰顶正好 0.5R → 不平", f"实际 {r}")


# ══════════════════════════════════════════════════════
#  C 组：配置真的读到了吗
# ══════════════════════════════════════════════════════
def test_config():
    print("\n" + "=" * 78)
    print("C 组：strategies.yaml 配置")
    print("=" * 78)
    import yaml
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    p = cfg["strategies"]["S15_CE_ZLSMA"]["params"]
    chk(p.get("min_dev_pct") == 0.5, "C1 min_dev_pct = 0.5", f"实际 {p.get('min_dev_pct')}")
    chk(p.get("quickfail_hours") == 4.0, "C2 quickfail_hours = 4.0",
        f"实际 {p.get('quickfail_hours')}")
    chk(p.get("quickfail_min_r") == 0.5, "C3 quickfail_min_r = 0.5",
        f"实际 {p.get('quickfail_min_r')}")
    # 红线：ce_len 不许动
    chk(p.get("ce_len") == 1, "C4 ce_len 仍为 1（红线未动）", f"实际 {p.get('ce_len')}")
    chk(p.get("ce_mult") == 2.0, "C5 ce_mult 仍为 2.0", f"实际 {p.get('ce_mult')}")
    chk(p.get("half_at_r") == 2.0, "C6 half_at_r 仍为 2.0（出场封版未动）",
        f"实际 {p.get('half_at_r')}")


def main():
    test_entry_dev()
    test_quick_fail()
    test_config()
    print("\n" + "=" * 78)
    total = len(PASS) + len(FAIL)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败  （共 {total} 项）")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  · {f}")
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
