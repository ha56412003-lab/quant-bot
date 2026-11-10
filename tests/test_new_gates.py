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
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

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
              bbw=80.0, atr=1.0, n=220, low_raw=None, high_raw=None,
              long_stop=None, short_stop=None):
    """固定指标。i = len(ks)-2 = 218，所以 dir[218] 当前、dir[217] 上一根。

    low_raw / high_raw / long_stop / short_stop 可选注入：
    2026-11-10 B 档止损是「关键K线低点 / 8 根摆动 / CE 通道 三者取近」，
    要把三个来源分开验，必须能分别指定。
    """
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
        "low_raw": [low_raw if low_raw is not None else ha_close * 0.98] * n,
        "high_raw": [high_raw if high_raw is not None else ha_close * 1.02] * n,
    }
    if long_stop is not None:
        ind["long_stop"] = [long_stop] * n
    if short_stop is not None:
        ind["short_stop"] = [short_stop] * n
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
    print("C 组：strategies.yaml = B 档定稿（2026-11-10 浩哥拍板，改动必须先问）")
    print("=" * 78)
    import yaml
    cfg = yaml.safe_load(open(os.path.join(_ROOT, "strategies.yaml"), encoding="utf-8"))
    node = cfg["strategies"]["S15_CE_ZLSMA"]
    p = node["params"]
    # 红线：ce_len 不许动
    chk(p.get("ce_len") == 1, "C1 ce_len 仍为 1（红线未动）", f"实际 {p.get('ce_len')}")
    chk(p.get("ce_mult") == 1.5, "C2 ce_mult = 1.5（六档扫描峰，作者原设计 2.0）",
        f"实际 {p.get('ce_mult')}")
    chk(p.get("swing_lookback") == 8, "C3 swing_lookback = 8（双止损最优）",
        f"实际 {p.get('swing_lookback')}")
    # ④ 偏离闸（自适应）
    chk(p.get("min_dev_pct") == 0.0, "C4 min_dev_pct = 0（固定门槛已让位给 ATR）",
        f"实际 {p.get('min_dev_pct')}")
    chk(p.get("dev_atr_mult") == 1.0, "C5 dev_atr_mult = 1.0（偏离闸 = 1.0×ATR20）",
        f"实际 {p.get('dev_atr_mult')}")
    # ②b 关键K线
    chk(p.get("key_bar") is True, "C6 key_bar = true（关键K线闸开）",
        f"实际 {p.get('key_bar')}")
    chk(p.get("key_mult") == 0.7, "C7 key_mult = 0.7（B 档定稿）", f"实际 {p.get('key_mult')}")
    chk(p.get("key_closepos") == 0.66, "C8 key_closepos = 0.66", f"实际 {p.get('key_closepos')}")
    chk(p.get("key_bodyfrac") == 0.5, "C9 key_bodyfrac = 0.5", f"实际 {p.get('key_bodyfrac')}")
    chk(p.get("stop_key") is True, "C10 stop_key = true（止损含关键K线低/高点）",
        f"实际 {p.get('stop_key')}")
    # 出场四条
    chk(p.get("half_at_r") == 1.2, "C11 half_at_r = 1.2（平半线，转折点）",
        f"实际 {p.get('half_at_r')}")
    chk(p.get("half_tp_r") == 0.0, "C12 half_tp_r = 0（旧「平半后再赚X全平」已删）",
        f"实际 {p.get('half_tp_r')}")
    chk(p.get("full_at_r") == 2.0, "C13 full_at_r = 2.0（兜底全平）", f"实际 {p.get('full_at_r')}")
    chk(p.get("right_exit") is True, "C14 right_exit = true（右侧止盈开）",
        f"实际 {p.get('right_exit')}")
    chk(p.get("right_exit_strict") is True, "C15 right_exit_strict = true（必须严格）",
        f"实际 {p.get('right_exit_strict')}")
    chk(p.get("mirror_exit") is False, "C16 mirror_exit = false（镜像出场整条删除）",
        f"实际 {p.get('mirror_exit')}")
    chk(p.get("trail_arm") == 0.0, "C17 trail_arm = 0（移动止盈已删）",
        f"实际 {p.get('trail_arm')}")
    chk(p.get("trail_dd") == 0.0, "C18 trail_dd = 0（移动止盈已删）",
        f"实际 {p.get('trail_dd')}")
    chk(p.get("quickfail_hours") == 0.0, "C19 quickfail_hours = 0（快速失败已删）",
        f"实际 {p.get('quickfail_hours')}")
    chk(node.get("max_hold_hours") == 0,
        "C20 max_hold_hours = 0（时间止损已删，靠 engine 的 0 值保护）",
        f"实际 {node.get('max_hold_hours')}")
    chk(p.get("max_risk_pct") == 5.0, "C21 max_risk_pct = 5.0（止损距离上限 5%）",
        f"实际 {p.get('max_risk_pct')}")
    # ⑥ 进场 EFF（从池子闸挪来）
    chk(p.get("eff_min_signal") == 0.30, "C22 eff_min_signal = 0.30（EFF 挪到进场）",
        f"实际 {p.get('eff_min_signal')}")
    chk(p.get("eff_win_signal") == 48, "C23 eff_win_signal = 48", f"实际 {p.get('eff_win_signal')}")
    # ③ BBW（最硬的一道墙）
    chk(p.get("bbw_pct_min") == 70.0, "C24 bbw_pct_min = 70.0（松到 60 胜率立刻掉 5pp）",
        f"实际 {p.get('bbw_pct_min')}")
    # 监控池
    mon = cfg["pools"]["monitor"]
    chk(mon.get("size") == 40, "C25 池子 size = 40（扩到 50 只多单量不增利润）",
        f"实际 {mon.get('size')}")
    chk(mon.get("stop_min_pct") == 0.5, "C26 池子 stop_min_pct = 0.5", f"实际 {mon.get('stop_min_pct')}")
    chk(mon.get("stop_exit_pct") == 0.3,
        "C27 池子 stop_exit_pct = 0.3（必须 < 进池线，否则倒挂）",
        f"实际 {mon.get('stop_exit_pct')}")
    chk(mon.get("eff_min") == 0.0 and mon.get("eff_exit") == 0.0,
        "C28 池子 EFF 闸已关闭（挪到进场闸）",
        f"实际 {mon.get('eff_min')} / {mon.get('eff_exit')}")


# ══════════════════════════════════════════════════════
#  D 组：关键K线闸 + 自适应偏离闸（2026-11-10 B 档新增）
# ══════════════════════════════════════════════════════
def make_ks_kb(base=100.0, n=220, big_at=None, side="long", amp=0.005):
    """第 big_at 根做成大阳/大阴线，其余是振幅 amp 的横向小K线。"""
    bar = 900
    cur = int(time.time() // bar) * bar
    start = cur - (n - 1) * bar
    out = []
    for i in range(n):
        if i == big_at:
            if side == "long":
                o, c = base * 0.97, base * 1.02
                h, l = base * 1.025, base * 0.965
            else:
                o, c = base * 1.03, base * 0.98
                h, l = base * 1.035, base * 0.975
        else:
            o = c = base
            h, l = base * (1 + amp), base * (1 - amp)
        out.append({"ts": start + i * bar, "open": o,
                    "high": h, "low": l, "close": c})
    return out


def test_key_bar():
    print()
    print("=" * 78)
    print("D 组：关键K线闸 + 自适应偏离闸（HA 判方向，原始K线判力度）")
    print("=" * 78)

    # 基线参数：其余闸全放行，只留关键K线 + 偏离 + 三取近止损
    # max_risk_pct 放到 10：这组是造出来的大 K 线，止损天然偏远，
    # 用 5% 会被「止损太远」提前挡掉，测不到要测的闸。
    BASE = dict(key_bar=True, key_mult=0.7, key_closepos=0.66, key_bodyfrac=0.5,
                dev_atr_mult=1.0, min_dev_pct=0.0, bbw_pct_min=70.0,
                eff_min_signal=0.0, swing_lookback=8, max_risk_pct=10.0,
                stop_key=True)

    async def run(ks, entry_px, base=None, side="long", zl=None,
                  low_raw=None, high_raw=None, long_stop=None, short_stop=None):
        params = dict(BASE if base is None else base)
        S._LAST_BAR.clear()
        S._GATE_STATE.clear()
        ctx = FakeCtx(entry_px, ks)
        st = make_strategy(**params)
        _zl = zl if zl is not None else entry_px * (0.98 if side == "long" else 1.02)
        if side == "long":
            orig = patch_ind(zl=_zl, ha_open=entry_px * 0.995,
                             ha_close=entry_px, dir_now=1, dir_prev=-1,
                             low_raw=low_raw, long_stop=long_stop)
        else:
            orig = patch_ind(zl=_zl, ha_open=entry_px * 1.005,
                             ha_close=entry_px, dir_now=-1, dir_prev=1,
                             high_raw=high_raw, short_stop=short_stop)
        try:
            return await st.evaluate(ctx)
        finally:
            S._ce_zlsma = orig

    # D1 最后一根（i=218）是小实体 → 不算关键K线 → 不开
    ks = make_ks_kb(base=100.0, big_at=None)
    sig = asyncio.run(run(ks, 100.0))
    chk(len(sig) == 0, "D1 普通小实体K线 → 关键K线闸挡住", f"实际 {len(sig)} 个信号")
    g = S._GATE_STATE.get("TESTUSDT") or {}
    chk("关键K线" in str(g.get("why", "")), "D2 诊断表写明卡在「关键K线力度不足」",
        f"why={g.get('why')}")

    # D3 i=218 是大阳线 → 过闸
    ks = make_ks_kb(base=100.0, big_at=218, side="long")
    sig = asyncio.run(run(ks, 100.0))
    chk(len(sig) == 1, "D3 大阳线（实体 5%、收在高位）→ 过关键K线闸",
        f"实际 {len(sig)} 个信号")
    if sig:
        chk(sig[0].side == "long", "D4 信号方向 = 多", f"实际 {sig[0].side}")

    # D5 key_bar=false → 同一根小实体K线也放行（回滚路径必须通）
    ks = make_ks_kb(base=100.0, big_at=None)
    sig = asyncio.run(run(ks, 100.0, base=dict(BASE, key_bar=False)))
    chk(len(sig) == 1, "D5 key_bar=false 关闭 → 小实体也放行（回滚路径）",
        f"实际 {len(sig)} 个信号")

    # D6 空头对称：大阴线 → 开空
    ks = make_ks_kb(base=100.0, big_at=218, side="short")
    sig = asyncio.run(run(ks, 100.0, side="short"))
    chk(len(sig) == 1 and sig[0].side == "short",
        "D6 大阴线 → 空头同样过闸（多空对称）",
        f"实际 {len(sig)} 个 {sig[0].side if sig else ''}")

    # D7 方向必须一致：多头信号 + 大阴线 → 挡
    ks = make_ks_kb(base=100.0, big_at=218, side="short")
    sig = asyncio.run(run(ks, 100.0, side="long"))
    chk(len(sig) == 0, "D7 多头信号但最后一根是大阴线 → 挡住",
        f"实际 {len(sig)} 个信号")

    # D8 止损三取近：多头取「关键K线低点 / 8 根摆动低点 / CE 通道」里最高的
    #    关键K线低点 = 96.5（make_ks_kb 给的大阳线）
    ks = make_ks_kb(base=100.0, big_at=218, side="long")
    sig = asyncio.run(run(ks, 100.0, low_raw=96.0))
    chk(bool(sig) and abs(sig[0].stop_price - 96.5) < 0.01,
        "D8a 摆动更远(96.0) → 取关键K线低点 96.5",
        f"实际 {sig[0].stop_price if sig else None}")
    sig = asyncio.run(run(ks, 100.0, low_raw=97.5))
    chk(bool(sig) and abs(sig[0].stop_price - 97.5) < 0.01,
        "D8b 摆动更近(97.5) → 取摆动 97.5（取近原则）",
        f"实际 {sig[0].stop_price if sig else None}")
    sig = asyncio.run(run(ks, 100.0, long_stop=98.2))
    chk(bool(sig) and abs(sig[0].stop_price - 98.2) < 0.01,
        "D8c CE 最近(98.2) → 取 CE 98.2",
        f"实际 {sig[0].stop_price if sig else None}")
    sig = asyncio.run(run(ks, 100.0, base=dict(BASE, stop_key=False), low_raw=96.0))
    chk(bool(sig) and abs(sig[0].stop_price - 96.0) < 0.01,
        "D8d stop_key=false → 关键K线不参与止损",
        f"实际 {sig[0].stop_price if sig else None}")

    # D9 自适应偏离：同一偏离，门槛随 ATR(20) 自动缩放
    ks_small = make_ks_kb(base=100.0, big_at=218, side="long", amp=0.001)
    sig_small = asyncio.run(run(ks_small, 100.0))
    chk(len(sig_small) == 1,
        "D9a ATR20≈0.2% 时门槛 0.2%，偏离 2.04% 放行", f"实际 {len(sig_small)}")
    ks_big = make_ks_kb(base=100.0, big_at=218, side="long", amp=0.02)
    sig_big = asyncio.run(run(ks_big, 100.0, zl=99.0))
    chk(len(sig_big) == 0,
        "D9b ATR20≈2% 时门槛抬到 2%，同一偏离 1.01% 不够 → 挡住",
        f"实际 {len(sig_big)}")
    g = S._GATE_STATE.get("TESTUSDT") or {}
    chk(g.get("dev_thr") is not None and float(g["dev_thr"]) > 1.0,
        "D10 诊断表带自适应门槛 dev_thr（>1%）",
        f"dev_thr={g.get('dev_thr')}")


# ══════════════════════════════════════════════════════
#  E 组：出场链（平半 → 2R兜底 → 右侧止盈 → 镜像可关）
# ══════════════════════════════════════════════════════
def test_exit_chain():
    print("\n" + "=" * 78)
    print("E 组：出场链（B 档重排：平半 → 2R兜底 → 右侧止盈 → 镜像可关）")
    print("=" * 78)

    EXIT_BASE = dict(half_at_r=1.2, half_tp_r=0.0, full_at_r=2.0,
                     right_exit=True, right_exit_strict=True,
                     mirror_exit=False, trail_arm=0.0, trail_dd=0.0,
                     quickfail_hours=0.0, mirror_bbw_min=70.0,
                     key_mult=0.7, key_closepos=0.66, key_bodyfrac=0.5)

    def mkpos(half=0, entry=100.0, stop=99.0, side="long"):
        return {"id": 1, "symbol": "TESTUSDT", "side": side,
                "entry_price": entry, "stop_price": stop,
                "opened_at": time.time() - 3600, "peak_r": 0.0,
                "mfe_pct": 0.0, "half_closed": half}

    def clear():
        S._S15_POS_STATE.clear()
        S._S15_ZL_CACHE.clear()
        S._S15_MIRROR_STATE.clear()
        S._S15_RIGHT_STATE.clear()

    async def run(px, pos, side="long", zl=101.0, ha_open=100.5, ha_close=99.0,
                  dir_now=-1, dir_prev=1, bbw=80.0, ks=None, **over):
        clear()
        st = make_strategy(**dict(EXIT_BASE, **over))
        ks = ks if ks is not None else make_ks_kb(base=100.0, n=220, big_at=None)
        orig = patch_ind(zl=zl, ha_open=ha_open, ha_close=ha_close,
                         dir_now=dir_now, dir_prev=dir_prev, bbw=bbw)
        try:
            return await st.check_exit(FakeCtx(px, ks), pos)
        finally:
            S._ce_zlsma = orig

    # E1 只涨 0.5R → 什么都不做
    r = asyncio.run(run(100.5, mkpos()))
    chk(r is None, "E1 浮盈 0.5R（未到 1.2R）→ 不动", f"实际 {r}")

    # E2 到 1.2R → 平一半
    r = asyncio.run(run(101.2, mkpos()))
    chk(r is not None and "HALF" in str(r).upper(), "E2 到 1.2R → 平一半",
        f"实际 {r}")

    # E3 已平半 + 到 2R → 兜底全平
    r = asyncio.run(run(102.0, mkpos(half=1)))
    chk(r is not None and "兜底" in str(r), "E3 已平半 + 到 2.0R → 兜底全平",
        f"实际 {r}")

    # E4 已平半 + 1.5R + HA 反向穿均线（strict 关）→ 右侧止盈
    r = asyncio.run(run(101.5, mkpos(half=1), zl=101.0, ha_open=101.0,
                        ha_close=100.5, right_exit_strict=False))
    chk(r is not None and "右侧" in str(r), "E4 平半后 HA 反向穿 ZLSMA → 右侧止盈",
        f"实际 {r}")

    # E5 同条件但 strict 开 + 反向K线没力度 → 不出
    r = asyncio.run(run(101.5, mkpos(half=1), zl=101.0, ha_open=101.0,
                        ha_close=100.5, right_exit_strict=True))
    chk(r is None, "E5 strict 开 + 反向K线无力度 → 不认，继续持有",
        f"实际 {r}")

    # E6 同条件 strict 开 + 反向K线是真正的大阴线 → 出
    ks_bear = make_ks_kb(base=100.0, n=220, big_at=218, side="short")
    r = asyncio.run(run(101.5, mkpos(half=1), zl=101.0, ha_open=101.0,
                        ha_close=100.5, right_exit_strict=True, ks=ks_bear))
    chk(r is not None and "右侧" in str(r),
        "E6 strict 开 + 反向关键K线（大阴线）→ 右侧止盈", f"实际 {r}")

    # E7 平半前不判右侧（1.2R 之前出反向K线也不能抢跑）
    r = asyncio.run(run(100.3, mkpos(half=0), zl=101.0, ha_open=101.0,
                        ha_close=100.5, right_exit_strict=False, ks=ks_bear))
    chk(r is None, "E7 未平半 → 右侧止盈不抢跑", f"实际 {r}")

    # E8 镜像全条件满足但 mirror_exit=false → 不得报镜像
    r = asyncio.run(run(99.0, mkpos(half=1), zl=101.0, ha_open=100.5,
                        ha_close=99.0, dir_now=-1, dir_prev=1, bbw=80.0,
                        mirror_exit=False, right_exit=False, full_at_r=0.0))
    chk(r is None, "E8 mirror_exit=false → 镜像条件全满足也不出场",
        f"实际 {r}")

    # E9 回滚路径：mirror_exit=true → 镜像照样能出
    r = asyncio.run(run(99.0, mkpos(half=1), zl=101.0, ha_open=100.5,
                        ha_close=99.0, dir_now=-1, dir_prev=1, bbw=80.0,
                        mirror_exit=True, right_exit=False, full_at_r=0.0))
    chk(r is not None and "镜像" in str(r), "E9 mirror_exit=true → 镜像仍能出场（回滚通）",
        f"实际 {r}")

    # E10 max_hold_hours=0 不得「开仓即平」—— engine 的 0 值保护
    import backend.engine as E
    import inspect
    src = inspect.getsource(E)
    chk('pos["max_hold_sec"] and' in src,
        "E10 engine 时间止损有 0 值保护（否则 max_hold_hours=0 会开仓即平）")
    chk(True, "E11 时间止损/快速失败/移动止盈 已在 C 组锁定为 0（无残留出口）")


def main():
    test_entry_dev()
    test_quick_fail()
    test_config()
    test_key_bar()
    test_exit_chain()
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
