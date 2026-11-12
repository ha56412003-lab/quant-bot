# -*- coding: utf-8 -*-
"""新增两道闸的单元测试（2026-11-08 浩哥批准落地）

  闸一：进场偏离 —— **2026-11-11 晚物理删除，VS15-1.2 连配置键一起删**
  闸二：快速失败 —— **2026-11-10 B 档整条删除**（方法已删，不再测）

要验的四件事（VS15-1.2 口径）：
  A1 偏离再小也开仓 —— 闸确实不在了（回滚护栏）
  A2 诊断表里不再有 dev / dev_thr 字段（面板那一列也删了）
  A3 strategies.yaml 里不再有 min_dev_pct / dev_atr_mult 两个键（不留死键）
  A4 config.py 不再校验这两个键（改 yaml 也不会因为超范围被拒）
    （B 组已随快速失败一起删除；原 B1~B8 见 git 历史 b654707）

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
    def __init__(self, px, ks, pos_side=None):
        self._px = px
        self._ks = ks
        self._pos = pos_side      # None = 无仓；'long'/'short' = 在管仓位方向

    def has_position(self, sym, sid):
        return self._pos is not None

    def open_side(self, sym, sid=None):
        # 2026-11-11 晚：evaluate 改用 open_side 区分「同向/反向」后，
        # 测试替身必须同步 —— 否则 evaluate 直接抛 AttributeError，整只币跳过。
        return self._pos

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
    # ★ 2026-11-11 晚：触发口径改成「ZLSMA 时段」后，evaluate 不再看 ind["dir"]，
    #   而是调 _zlsma_legs()。测试用的是平线 K 线（永远不穿越均线），
    #   不伪造它就一律判「时段未确认」→ 所有开仓类用例 0 信号。
    #   这里直接把「本根时段方向」焊成 dir_now，等价于旧 dir 翻转的语义。
    _leg = [0] * n
    _leg[n - 2] = dir_now
    _lts = [0] * n
    _lts[n - 2] = 1
    _side_arr = [dir_now] * n
    _starts = [(n - 2, dir_now, n - 3)] if dir_now else []
    S._zlsma_legs = lambda *a, **k: (_leg, _lts, _side_arr, _starts)
    # ★ 伪造的 leg_ts 恒为 1，_LEG_ENTERED 会在第一个用例之后就永久记住它，
    #   导致后面所有开仓用例都被「本时段已入场」挡掉。每个用例必须从零点开始。
    S._LEG_ENTERED.clear()
    S._LAST_BAR.clear()
    return orig


def make_strategy(**over):
    st = S.S15CeZlsma.__new__(S.S15CeZlsma)
    st.p = {
        "kline_interval": "15m",
        "ce_len": 1, "ce_mult": 2.0, "zlsma_len": 50,
        "use_close": True, "use_ha": True,
        "swing_lookback": 20, "min_atr_pct": 0, "bbw_pct_min": 70.0,
        "max_risk_pct": 30.0, "fee_rate": 0.0005, "slip_pct": 0.0,
        "half_at_r": 1.2,
        # ★ VS15-1.2：这两个闸的代码默认值已对齐定版（True）。
        #   A/C/E/F 各组测的是别的规则，必须显式关掉，不能靠默认值——
        #   否则以后默认值一改，这些用例会莫名其妙地失败（本次就踩到了）。
        "key_bar": False, "stop_key": False,
        # 止损距离下限也显式关掉：A/C/E/F 各组用的是合成 K 线，
        # 止损天然很近，会被 0.6% 下限挡掉（VS15-1.2 默认值已改为 0.6）。
        "min_stop_dist_pct": 0.0,
        # 趋势效率门槛同理：合成数据 EFF 很低，默认 0.3 会把它们全挡掉
        "eff_min_signal": 0.0,
        "reverse_enabled": False,                  # 反手默认关（回测已证伪）
    }
    st.p.update(over)
    st.sid = "S15_CE_ZLSMA"
    st.max_hold = 16 * 3600
    st.syms = lambda ctx: ["TESTUSDT"]
    return st


# ══════════════════════════════════════════════════════
#  A 组：进场偏离闸 —— 2026-11-11 晚已【物理删除】
# ══════════════════════════════════════════════════════
def test_entry_dev():
    """偏离闸已按 2026-11-11 晚浩哥的决定物理删除；VS15-1.2 把配置键和面板列也删了。

    旧用例 A1~A6 全部作废 —— 它们验的是「偏离不够 → 不开仓」，
    而这条拦截现在根本不存在了，留着只会测出一个假失败，比没测试更糟。
    现在只验「删干净了没有」：代码不拦、诊断表不带字段、yaml 没有死键。
    """
    print("\n" + "=" * 78)
    print("A 组：偏离闸已删除 —— 验代码不拦 + 字段和死键都没了")
    print("=" * 78)

    S._LAST_BAR.clear()
    S._GATE_STATE.clear()
    S._LEG_ENTERED.clear()
    ks = make_ks(price=100.2)
    st = make_strategy()
    orig = patch_ind(zl=100.0, ha_open=99.5, ha_close=100.2, dir_now=1, dir_prev=-1)
    try:
        sig = asyncio.run(st.evaluate(FakeCtx(100.2, ks)))
    finally:
        S._ce_zlsma = orig
    # 关键行为：偏离只有 0.2%，旧口径下会被门槛挡掉，现在必须照开
    chk(len(sig) == 1, "A0 偏离 0.2% 仍开仓 —— 闸真的删了", f"实际 {len(sig)} 个信号")
    g = S._GATE_STATE.get("TESTUSDT") or {}
    chk("dev" not in g and "dev_thr" not in g,
        "A1 诊断表里没有 dev / dev_thr 字段（VS15-1.2 删净）",
        f"实际键 {sorted(g.keys())}")


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
    # 2026-11-11 晚 浩哥定：恢复指标原版 2.0（此前 B 档为追单量调到 1.5）
    chk(p.get("ce_mult") == 2.0, "C2 ce_mult = 2.0（指标原版，已恢复）",
        f"实际 {p.get('ce_mult')}")
    chk(p.get("swing_lookback") == 8, "C3 swing_lookback = 8（双止损最优）",
        f"实际 {p.get('swing_lookback')}")
    # ④ 偏离闸（自适应）—— 2026-11-11 晚物理删除；VS15-1.2 连两个配置键一起删
    chk("min_dev_pct" not in p and "dev_atr_mult" not in p,
        "C4 yaml 里没有 min_dev_pct / dev_atr_mult 死键（VS15-1.2 已删净）",
        f"实际多出 {[k for k in p if 'dev' in k]}")
    from backend import config as C
    _b = C._STRAT_PARAM_BOUNDS
    chk("min_dev_pct" not in _b and "dev_atr_mult" not in _b,
        "C5 config.py 也不再校验这两个键（不留半截开关）",
        f"实际 _STRAT_PARAM_BOUNDS 里还有 {[k for k in _b if 'dev' in k]}")
    # ②b 关键K线
    chk(p.get("key_bar") is True, "C6 key_bar = true（关键K线闸开）",
        f"实际 {p.get('key_bar')}")
    # 2026-11-11 晚 浩哥定：恢复指标原版 1.0（此前 B 档为追单量调到 0.7）
    chk(p.get("key_mult") == 1.0, "C7 key_mult = 1.0（指标原版，已恢复）",
        f"实际 {p.get('key_mult')}")
    chk(p.get("key_closepos") == 0.66, "C8 key_closepos = 0.66", f"实际 {p.get('key_closepos')}")
    chk(p.get("key_bodyfrac") == 0.5, "C9 key_bodyfrac = 0.5", f"实际 {p.get('key_bodyfrac')}")
    chk(p.get("stop_key") is True, "C10 stop_key = true（止损含关键K线低/高点）",
        f"实际 {p.get('stop_key')}")
    # 出场四条
    chk(p.get("half_at_r") == 1.2, "C11 half_at_r = 1.2（平半线，转折点）",
        f"实际 {p.get('half_at_r')}")
    chk("half_tp_r" not in p, "C12 half_tp_r 键已从 yaml 删除（旧「平半后再赚X全平」）",
        f"实际还在: {p.get('half_tp_r')}")
    chk(p.get("full_at_r") == 2.5, "C13 full_at_r = 2.5（兜底全平，2026-11-12 由 2.0 上调）", f"实际 {p.get('full_at_r')}")
    chk(p.get("right_exit") is True, "C14 right_exit = true（右侧止盈开）",
        f"实际 {p.get('right_exit')}")
    chk(p.get("right_exit_strict") is True, "C15 right_exit_strict = true（必须严格）",
        f"实际 {p.get('right_exit_strict')}")
    chk("mirror_exit" not in p, "C16 mirror_exit 键已删除（镜像出场整条删）",
        f"实际还在: {p.get('mirror_exit')}")
    chk("trail_arm" not in p, "C17 trail_arm 键已删除（移动止盈已删）",
        f"实际还在: {p.get('trail_arm')}")
    chk("trail_dd" not in p, "C18 trail_dd 键已删除（移动止盈已删）",
        f"实际还在: {p.get('trail_dd')}")
    chk("quickfail_hours" not in p, "C19 quickfail_hours 键已删除（快速失败已删）",
        f"实际还在: {p.get('quickfail_hours')}")
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
    chk(mon.get("size") == 50, "C25 池子 size = 50（11-11 定档：50 档 +55.88R > 40 档 +45.93R）",
        f"实际 {mon.get('size')}")
    # 2026-11-10 定档：止损距离闸整条删除（BTC/ETH 曾被它挡在池外）
    chk("stop_min_pct" not in mon and "stop_exit_pct" not in mon,
        "C26 池子止损距离闸已删除（两键不该存在）",
        f"实际 {[k for k in ('stop_min_pct', 'stop_exit_pct') if k in mon]}")
    chk(float(p.get("dual_stop_min_pct") or 0) == 0.5,
        "C27 两档止损分界 dual_stop_min_pct = 0.5", f"实际 {p.get('dual_stop_min_pct')}")
    chk(mon.get("eff_min") == 0.0 and mon.get("eff_exit") == 0.0,
        "C28 池子 EFF 闸已关闭（挪到进场闸）",
        f"实际 {mon.get('eff_min')} / {mon.get('eff_exit')}")


# ══════════════════════════════════════════════════════
#  D 组：关键K线闸（偏离闸 2026-11-11 已删，见 A 组）
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
    print("D 组：关键K线闸（HA 判方向，原始K线判力度）")
    print("=" * 78)

    # 基线参数：其余闸全放行，只留关键K线 + 三取近止损
    # max_risk_pct 放到 10：这组是造出来的大 K 线，止损天然偏远，
    # 用 5% 会被「止损太远」提前挡掉，测不到要测的闸。
    BASE = dict(key_bar=True, key_mult=0.7, key_closepos=0.66, key_bodyfrac=0.5,
                bbw_pct_min=70.0,
                eff_min_signal=0.0, swing_lookback=8, max_risk_pct=10.0,
                # D8a~d 验的是「三条候选取最近」的纯逻辑，先把 ATR 缓冲关掉；
                # 缓冲本身由 D8e 单独验（0.2 ATR 会把关键K线止损推得更远）。
                stop_key_atr_buf=0.0,
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

    # D8e ATR 缓冲（VS15-1.2 新增 0.2）：关键K线止损必须被推得更远
    sig0 = asyncio.run(run(ks, 100.0, low_raw=96.0))
    sig2 = asyncio.run(run(ks, 100.0, low_raw=96.0,
                           base=dict(BASE, stop_key_atr_buf=0.2)))
    if sig0 and sig2:
        gap = sig0[0].stop_price - sig2[0].stop_price
        chk(gap > 0,
            "D8e 0.2 ATR 缓冲把多单止损推得更远（更低）—— VS15-1.2 定版核心",
            f"无缓冲 {sig0[0].stop_price:.4f} → 有缓冲 {sig2[0].stop_price:.4f}")
    else:
        chk(False, "D8e 缓冲开/关都必须出信号",
            f"buf0={bool(sig0)} buf0.2={bool(sig2)}")

    # D9 自适应偏离：2026-11-11 晚偏离闸物理删除 → 整组作废
    #   （旧用例验的是「门槛随 ATR20 缩放」，现在门槛恒为 0，这条拦截不存在了）
    ks_small = make_ks_kb(base=100.0, big_at=218, side="long", amp=0.001)
    sig_small = asyncio.run(run(ks_small, 100.0))
    chk(len(sig_small) == 1,
        "D9 偏离闸已删：ATR20 极小的币也照常开仓", f"实际 {len(sig_small)}")
    g = S._GATE_STATE.get("TESTUSDT") or {}
    chk("dev_thr" not in g,
        "D10 诊断表里不再有 dev_thr（VS15-1.2 把字段删净）",
        f"实际键 {sorted(g.keys())}")


# ══════════════════════════════════════════════════════
#  E 组：出场链（保本1.5R → 平半1.2R → 全平2.5R → 右侧止盈；镜像/快速失败/移动止盈已整条删除）
# ══════════════════════════════════════════════════════
def test_exit_chain():
    print("\n" + "=" * 78)
    print("E 组：出场链（保本 1.5R → 平半 1.2R → 兜底 2.5R → 右侧止盈 → 硬止损）")
    print("=" * 78)

    EXIT_BASE = dict(half_at_r=1.2, full_at_r=2.5,
                     right_exit=True, right_exit_strict=True,
                     key_mult=0.7, key_closepos=0.66, key_bodyfrac=0.5)

    def mkpos(half=0, entry=100.0, stop=99.0, side="long"):
        return {"id": 1, "symbol": "TESTUSDT", "side": side,
                "entry_price": entry, "stop_price": stop,
                "opened_at": time.time() - 3600, "peak_r": 0.0,
                "mfe_pct": 0.0, "half_closed": half}

    def clear():
        S._S15_POS_STATE.clear()
        S._S15_ZL_CACHE.clear()
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

    # E3 已平半 + 到 2.5R → 兜底全平（VS15-1.2：门槛 2.0 → 2.5）
    r = asyncio.run(run(102.5, mkpos(half=1)))
    chk(r is not None and "兜底" in str(r), "E3 已平半 + 到 2.5R → 兜底全平",
        f"实际 {r}")

    # E3b 已平半 + 只有 2.0R → 不许触发（证明确实是 2.5 而不是 2.0）
    r = asyncio.run(run(102.0, mkpos(half=1)))
    chk(r is None, "E3b 已平半 + 仅 2.0R → 不触发兜底（门槛是 2.5R）", f"实际 {r}")

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

    # E10 max_hold_hours=0 不得「开仓即平」—— engine 的 0 值保护
    import backend.engine as E
    import inspect
    src = inspect.getsource(E)
    chk('pos["max_hold_sec"] and' in src,
        "E10 engine 时间止损有 0 值保护（否则 max_hold_hours=0 会开仓即平）")
    chk(True, "E11 时间止损/快速失败/移动止盈/镜像：代码已删、yaml 键已删（C 组锁定）")


# ══════════════════════════════════════════════════════
#  F 组：反向开仓（2026-11-11 晚 浩哥定「反转之后可以做反向单」）
# ══════════════════════════════════════════════════════
def test_reverse():
    print("\n" + "=" * 78)
    print("F 组：反向开仓（时段方向 vs 在管仓位方向）")
    print("=" * 78)

    def run(pos_side, dir_now, price=100.8, fresh=True, rev=True):
        if fresh:
            S._LAST_BAR.clear()
            S._GATE_STATE.clear()
            S._LEG_ENTERED.clear()
        ks = make_ks(price=price)
        st = make_strategy(reverse_enabled=rev)
        ctx = FakeCtx(price, ks, pos_side=pos_side)
        if dir_now == 1:
            orig = patch_ind(zl=100.0, ha_open=price * 0.995, ha_close=price, dir_now=1)
        else:
            orig = patch_ind(zl=101.0, ha_open=price * 1.005, ha_close=price, dir_now=-1)
        try:
            return asyncio.run(st.evaluate(ctx))
        finally:
            S._ce_zlsma = orig

    # F1 无仓 + 多头时段 → 普通开仓，不是反手
    sig = run(None, 1)
    chk(len(sig) == 1 and sig[0].reverse is False,
        "F1 无仓 → 普通开仓（reverse=False）",
        f"实际 {len(sig)} 个，reverse={sig[0].reverse if sig else None}")

    # F2 持多 + 多头时段 → 同向，不重复开
    sig = run("long", 1)
    chk(len(sig) == 0, "F2 持多 + 多头时段 → 同向，不重复开仓", f"实际 {len(sig)} 个")
    g = S._GATE_STATE.get("TESTUSDT") or {}
    chk("同向" in str(g.get("why", "")), "F3 诊断写明「同向已有仓位」", f"why={g.get('why')}")

    # F4 持多 + 空头时段 → 反向，发 reverse 信号
    sig = run("long", -1)
    chk(len(sig) == 1 and sig[0].reverse is True,
        "F4 持多 + 时段转空 → 发反手信号（reverse=True）",
        f"实际 {len(sig)} 个，reverse={sig[0].reverse if sig else None}")
    chk(bool(sig) and sig[0].side == "short", "F5 反手信号方向 = short",
        f"实际 {sig[0].side if sig else None}")
    chk(bool(sig) and sig[0].stop_price > 100.8,
        "F6 反手信号的止损在当前价上方（空单止损方向对）",
        f"stop={sig[0].stop_price if sig else None} 现价=100.8")

    # F7 持空 + 多头时段 → 反向
    sig = run("short", 1)
    chk(len(sig) == 1 and sig[0].reverse is True,
        "F7 持空 + 时段转多 → 发反手信号（reverse=True）",
        f"实际 {len(sig)} 个，reverse={sig[0].reverse if sig else None}")

    # F8 反手成功后 _LEG_ENTERED 记住本时段（不然下一根会再反一次）
    run("long", -1)
    chk(bool(S._LEG_ENTERED), "F8 反手后 _LEG_ENTERED 已记账（本时段不再重复反手）",
        f"_LEG_ENTERED={S._LEG_ENTERED}")

    # F9/F10 engine 侧：reverse 分支必须存在，且必须在 allow_entry 之前
    import backend.engine as E
    import inspect
    src = inspect.getsource(E)
    chk('getattr(sig, "reverse", False)' in src,
        "F9 engine 已接 reverse 分支（不接就永远开不出反手单）")
    chk(0 <= src.find('getattr(sig, "reverse"') < src.find("risk.allow_entry(sig"),
        "F10 reverse 平仓发生在 allow_entry 之前（顺序反了会被「同币已有仓位」硬闸拒掉）")

    # F11 开关关闭（默认）→ 持多 + 时段转空也不开仓，等于回到「有仓就跳过」的旧行为
    sig = run("long", -1, rev=False)
    chk(len(sig) == 0, "F11 反手开关关闭 → 持多遇时段转空不开仓（默认行为）",
        f"实际 {len(sig)} 个")
    # F12 开关关闭时，无仓照常开仓（别把正常开仓也关掉）
    sig = run(None, 1, rev=False)
    chk(len(sig) == 1 and sig[0].reverse is False,
        "F12 反手开关关闭不影响正常开仓", f"实际 {len(sig)} 个")



def test_bbw_exempt():
    """G 组：顶级关键K线豁免 BBW 的「面板标记」（2026-11-12 夜浩哥要求）。

    要解决的自相矛盾：BBW 分位 3%（灰色、明显没过），结论却说「全闸通过·待成交」，
    面板上看不出是靠顶级K线跳过去的。现在豁免成功必须打 bbw_exempt 标记，
    前端据此显示「3%豁」+ 琥珀色，结论列写「全闸通过·BBW豁免」。

    顺带验：豁免关闭（mult=0）和「普通K线 + BBW 不够」这两种情况不能被误标成豁免。
    """
    # 普通档（0.7/0.66/0.5）；豁免档用 strategies.py 的兜底值 1.0/0.8/0.8
    BASE = dict(key_bar=True, key_mult=0.7, key_closepos=0.66, key_bodyfrac=0.5,
                bbw_pct_min=70.0,
                eff_min_signal=0.0, swing_lookback=8, max_risk_pct=10.0,
                stop_key_atr_buf=0.0, stop_key=False,
                min_stop_dist_pct=0.0)

    async def run(ks, entry_px, bbw, base=None, side="long", zl=None):
        params = dict(BASE if base is None else base)
        S._LAST_BAR.clear()
        S._GATE_STATE.clear()
        ctx = FakeCtx(entry_px, ks)
        st = make_strategy(**params)
        _zl = zl if zl is not None else entry_px * (0.98 if side == "long" else 1.02)
        if side == "long":
            orig = patch_ind(zl=_zl, ha_open=entry_px * 0.995, ha_close=entry_px,
                             dir_now=1, dir_prev=-1, bbw=bbw, atr=1.0)
        else:
            orig = patch_ind(zl=_zl, ha_open=entry_px * 1.005, ha_close=entry_px,
                             dir_now=-1, dir_prev=1, bbw=bbw, atr=1.0)
        try:
            sig = await st.evaluate(ctx)
        finally:
            S._ce_zlsma = orig
        g = S._GATE_STATE.get("TESTUSDT") or {}
        return sig, g

    def mk_ks_top(base=100.0, n=220):
        """i=218 是顶级关键K线：实体 5.0(=5×ATR)、收盘位 0.917、实体占比 0.833。"""
        bar = 900
        cur = int(time.time() // bar) * bar
        start = cur - (n - 1) * bar
        out = []
        for i in range(n):
            if i == n - 2:
                o, c, h, l = base * 0.97, base * 1.02, base * 1.025, base * 0.965
            else:
                o = c = base
                h, l = base * 1.005, base * 0.995
            out.append({"ts": start + i * bar, "open": o, "high": h, "low": l, "close": c})
        return out

    def mk_ks_normal(base=100.0, n=220):
        """i=218 只是「普通」关键K线：实体 1.2、收盘位 0.774、实体占比 0.774。
        → 过 0.66/0.50 那条普通线，但过不了 0.80/0.80 的豁免线。"""
        bar = 900
        cur = int(time.time() // bar) * bar
        start = cur - (n - 1) * bar
        out = []
        for i in range(n):
            if i == n - 2:
                o, c, h, l = 98.8, 100.0, 100.35, 98.8
            else:
                o = c = base
                h, l = base * 1.005, base * 0.995
            out.append({"ts": start + i * bar, "open": o, "high": h, "low": l, "close": c})
        return out

    # ── G1/G2：BBW 不合格 + 顶级K线 → 豁免放行并打标记 ──
    ks = mk_ks_top()
    sig, g = asyncio.run(run(ks, 100.0, bbw=3.0))
    chk(len(sig) == 1, "G1 BBW 分位 3%（远低于 70）+ 顶级关键K线 → 豁免放行、开出信号",
        f"实际 {len(sig)} 个信号, why={g.get('why')}")
    chk(g.get("bbw_exempt") is True, "G2 豁免成立时 bbw_exempt 标记为 True（面板靠它显示「豁」）",
        f"实际 bbw_exempt={g.get('bbw_exempt')!r}, why={g.get('why')}")
    chk(g.get("bbw") == 3.0, "G2b 诊断里 bbw 仍是原始分位 3.0（不篡改事实，只加标记）",
        f"实际 bbw={g.get('bbw')!r}")
    chk(g.get("ok") is True, "G2c 豁免成立后 ok=True（闸门整体通过）",
        f"实际 ok={g.get('ok')!r}, why={g.get('why')}")

    # ── G3：BBW 不合格 + 只是普通K线 → 必须被 BBW 拦住，且不能标成豁免 ──
    ks = mk_ks_normal()
    sig, g = asyncio.run(run(ks, 100.0, bbw=3.0))
    chk(len(sig) == 0, "G3 BBW 3% + 普通关键K线 → 被 BBW 闸拦下（豁免不成立）",
        f"实际 {len(sig)} 个信号")
    chk(g.get("bbw_exempt") is not True, "G3b 被拦时 bbw_exempt 不能是 True",
        f"实际 bbw_exempt={g.get('bbw_exempt')!r}")
    chk("BBW分位" in str(g.get("why", "")), "G3c 诊断写明卡在「BBW分位」",
        f"why={g.get('why')}")

    # ── G4：豁免关掉（mult=0）+ 顶级K线 → 同样被 BBW 拦，且不标豁免（回滚路径） ──
    ks = mk_ks_top()
    sig, g = asyncio.run(run(ks, 100.0, bbw=3.0,
                             base=dict(BASE, bbw_exempt_mult=0.0)))
    chk(len(sig) == 0, "G4 bbw_exempt_mult=0（关闭豁免）+ 顶级K线 → 退回纯 BBW 口径、被拦",
        f"实际 {len(sig)} 个信号")
    chk(g.get("bbw_exempt") is not True, "G4b 关闭豁免时 bbw_exempt 保持 False",
        f"实际 bbw_exempt={g.get('bbw_exempt')!r}")

    # ── G5：BBW 本来就合格 → 不标豁免（避免把正常放行也说成豁免） ──
    ks = mk_ks_top()
    sig, g = asyncio.run(run(ks, 100.0, bbw=85.0))
    chk(len(sig) == 1, "G5 BBW 85%（合格）→ 正常放行", f"实际 {len(sig)} 个信号")
    chk(g.get("bbw_exempt") is not True, "G5b BBW 自己就够了 → 不能标成豁免",
        f"实际 bbw_exempt={g.get('bbw_exempt')!r}")


def main():
    test_entry_dev()
    test_config()
    test_key_bar()
    test_bbw_exempt()
    test_exit_chain()
    test_reverse()
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
