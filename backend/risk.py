"""Global risk layer — every entry signal passes through here.

Rules: event quiet window, fear&greed sizing, per-coin
cap, gross leverage cap, daily loss halt, max concurrent coins, no hedged
same-coin positions.
"""
import time

from . import db, factors

# 事件静默豁免名单：这些策略在自己的信号里已经处理过宏观数据，不需要再静默。
# 2026-11-06 自检清理：原内容是 {"S09_EVENT_BREAKOUT"}，但该类早已删除
# （11-04 日志「策略实例 S09_EVENT_BREAKOUT 已删除」），yaml 里也没有 → 空集。
# S15 不在豁免名单里，所以重要数据前 30 分钟它会被正常拦下（这是想要的）。
QUIET_EXEMPT: set[str] = set()


def _day_start():
    """当日 0 点（本地时区）。

    2026-11-06 自检修复：原实现是 int(time.time()) // 86400 * 86400，
    即 UTC 0 点 —— 在北京时间等于早上 8:00 换日。而日目标与净值曲线
    用的是本地 0 点，导致同一页面上「今日已实现」和「净值变化」两个数
    各按各的日界统计，互相打架（实测 11-06：+7.51 vs −89.34）。
    现统一为本地 0 点，与日报、日目标、看板口径一致。
    """
    import datetime as _dt
    return int(_dt.datetime.now().replace(hour=0, minute=0, second=0,
                                          microsecond=0).timestamp())


def daily_realized_pnl(mode="auto"):
    """今日已实现（按模式分）。

    2026-11-07 浩哥发现：切到实盘后「今日已实现」还挂着纸面的 +182.87。
    根因是这里不接 mode，永远算当前运行模式。

    2026-11-09 改用 db.realized_pnl_since：原来对 positions.pnl 求和，
    而那个字段是「该仓位历史全部平仓盈亏之和」，跨日的仓位会整笔挪到
    平仓当天，昨天的亏损被抹掉（POLUSDT #19：昨天 −17.05、今天 +15.86，
    账面显示今天赚 15.86，真实是 −1.20）。对 trades 流水求和才按天平。
    """
    return db.realized_pnl_since(_day_start(), mode)


def _halt_until_ts():
    """日内熔断的截止时间戳（只读，不含手动暂停）。"""
    return float(db.get_meta("halt_until", "0") or 0)


def manual_paused():
    """手动暂停运营（VS15-1.1 新增）。

    用途：系统更新 / 改参数 / 换凭据前，在配置页点一下，立刻停开新仓，
    但不动已开的仓位（要不要平仓是另一个决定，走「一键平仓」）。

    为什么单独存一个 key：日内熔断（halt_until）是风控自动触发的，
    恢复运营不能顺手把熔断也解掉 —— 两个语义必须分开。
    """
    return db.get_meta("manual_pause", "0") == "1"


def halted():
    """停开新仓：手动暂停 或 日内熔断，任一成立即停。"""
    if manual_paused():
        return True
    return time.time() < _halt_until_ts()


def open_unrealized_pnl(mode="auto"):
    """当前所有持仓的浮动盈亏（U）。任一币取不到价 → 返回 None。

    2026-11-09 新增，配合浮亏闸使用。取不到价必须返回 None 而不是 0：
    0 会被读成「浮亏为 0、一切正常」，而实际是「根本不知道亏了多少」。
    """
    from . import factors as _fac
    total = 0.0
    for p in db.open_positions(mode=mode):
        px = _fac.mark_price_of(p["symbol"])
        if not px or px <= 0:
            return None
        sign = 1 if p["side"] == "long" else -1
        total += (px - p["entry_price"]) * p["qty"] * sign
    return total


def unrealized_loss_pct(equity, mode="auto"):
    """浮亏占净值的百分比（只在亏损时为正数，盈利返回 0）。取不到价 → None。

    equity 由调用方传入 —— executor.equity() 是 async，而这里是被 sync 的
    allow_entry / API 调用的同步函数，不能在里面 await。
    """
    upnl = open_unrealized_pnl(mode)
    if upnl is None or not equity or equity <= 0:
        return None
    return max(0.0, -upnl / equity * 100.0)


def check_daily_halt(equity, cfg_global):
    pnl = daily_realized_pnl()
    if not (equity > 0 and pnl < 0 and abs(pnl) / equity * 100 >= cfg_global["daily_loss_halt_pct"]):
        return False
    # 已经熔断中就不要每 10 秒重写 meta、重打一条 warn（日志会被刷爆）
    # ★ 只看熔断本身，不看 manual_paused：手动暂停中若真触发熔断，必须照常落库，
    #   否则「恢复运营」一按就把熔断也放开了。
    if time.time() < _halt_until_ts():
        return True
    tomorrow = _day_start() + 86400
    db.set_meta("halt_until", tomorrow)
    db.log("warn", f"日内熔断触发:已实现亏损 {pnl:.2f} ({abs(pnl)/equity*100:.1f}%),停止开仓至次日本地 0 点")
    return True


def event_quiet(cfg_global):
    """True while inside the pre-event quiet window of a high-importance event."""
    cal = (db.get_factor("econ_cal") or {}).get("events", [])
    now = time.time()
    window = cfg_global["event_quiet_minutes"] * 60
    for e in cal:
        imp = str(e.get("importance", ""))
        if any(x in imp for x in ("3", "高", "high", "High")) and 0 < e["ts"] - now <= window:
            return e.get("title", "event")
    return ""


def size_multiplier(side, cfg_global):
    fg = db.get_factor("fear_greed") or {}
    v = fg.get("latest")
    ext = cfg_global["fear_greed_extreme"]
    if v is None:
        return 1.0
    if side == "long" and v >= ext["greed"]:
        return ext["size_mult"]
    if side == "short" and v <= ext["fear"]:
        return ext["size_mult"]
    return 1.0


def _strategy_capacity(sig, scfg, equity, cfg_global):
    """(capital_base, leverage, max_position_usd, used_notional) for a strategy."""
    risk = scfg.get("risk") or {}
    capital = float(risk.get("capital_usd") or 0) or equity
    leverage = int(risk.get("leverage") or cfg_global.get("max_gross_leverage", 3))
    max_pos = float(risk.get("max_position_usd") or 0) or capital * leverage
    used = sum(abs(p["qty"] * factors.mark_price_of(p["symbol"]))
               for p in db.open_positions(strategy=sig.strategy))
    return capital, leverage, max_pos, used


def allow_entry(sig, scfg, equity, cfg_global):
    """Returns (allowed: bool, reason: str, size_mult: float)."""
    if halted():
        return False, "日内熔断中", 0
    from . import daily_target as _dtm
    if _dtm.blocked():
        return False, "已达日目标，今日停止开新仓", 0
    if sig.strategy not in QUIET_EXEMPT:
        ev = event_quiet(cfg_global)
        if ev:
            return False, f"事件静默({ev[:30]})", 0
    open_pos = db.open_positions()
    coins = {p["symbol"] for p in open_pos}
    if sig.symbol in coins:
        return False, "同币已有仓位(禁止对锁/加仓)", 0
    if len(coins) >= cfg_global["max_concurrent_coins"]:
        return False, "并发币数已达上限", 0
    # 方向上限：避免 6 单全空/全多，市场单边反转时同时扫损
    max_same = int(cfg_global.get("max_same_side") or 0)
    if max_same <= 0:
        max_same = max(1, int(cfg_global["max_concurrent_coins"]) * 2 // 3)
    same = sum(1 for p in open_pos if p["side"] == sig.side)
    if same >= max_same:
        side_cn = "多" if sig.side == "long" else "空"
        return False, f"{side_cn}头方向已达上限({max_same})", 0
    # 总敞口闸门。
    # ⚠ 2026-11-06 修：以前是 sum(qty * mark_price_of(...))，而 mark_price_of
    # 在拿不到行情时返回 0.0（不是 None）→ 断线时所有仓位都算出「0 敞口」，
    # 这道闸门连同下面策略资金闸门一起静默消失，可以一路开满仓。
    # 现在：任一在仓币取不到有效价格 → 无法核算总敞口 → 拒绝开新仓（fail-closed）。
    missing, gross, upnl = [], 0.0, 0.0
    for p in open_pos:
        px = factors.mark_price_of(p["symbol"])
        if not px or px <= 0:
            missing.append(p["symbol"])
            continue
        gross += abs(p["qty"] * px)
        _sign = 1 if p["side"] == "long" else -1
        upnl += (px - p["entry_price"]) * p["qty"] * _sign
    if missing:
        return False, (f"有在仓币取不到行情({','.join(missing[:3])})，"
                       f"总敞口无法核算，暂停开仓"), 0
    if equity > 0 and gross / equity >= cfg_global["max_gross_leverage"]:
        return False, "总敞口已达杠杆上限", 0
    # ── 浮亏闸（2026-11-09 · 决策 2 第一级）─────────────────────────
    # 为什么要有这一级：日熔断只看**已实现**亏损。8 个仓位各浮亏 1R(=150U)
    # = 净值 12% 时系统依然认为「今天没亏」，继续开新仓；等这些止损全部落地，
    # 亏损已经发生，熔断只是马后炮。
    # 这一级只管「不再加新风险」：已有仓位的硬止损 / 保本 1.5R / 平半 1.2R /
    # 全平 2.5R / 右侧止盈 全部照常运行，不减仓、不强平。
    # ⚠ 与 halted() 的区别：浮亏会随价格回升，所以**不落库、不过夜**，
    #    纯实时判断 —— 价格反弹回来就自动恢复开仓，不需要人工干预。
    _uh = float(cfg_global.get("unrealized_halt_pct") or 0)
    if _uh > 0 and equity > 0:
        loss_pct = -upnl / equity * 100
        if loss_pct >= _uh:
            return False, (f"浮亏已达净值 {loss_pct:.1f}%（阈值 {_uh:g}%），"
                           f"暂停开新仓（已有仓位照常管理）"), 0
    capital, leverage, _max_pos, used = _strategy_capacity(sig, scfg, equity, cfg_global)
    if used >= capital * leverage:
        return False, "策略分配资金已用满", 0
    return True, "", size_multiplier(sig.side, cfg_global)


def position_qty(sig, scfg, equity, cfg_global, price):
    """Per-strategy risk-based sizing:
    qty = (capital_base × risk_per_trade%) / stop_distance,
    then capped by the strategy's max_position_usd, its remaining
    capital×leverage capacity, and the global per-coin exposure limit."""
    if price <= 0:
        return 0
    # ⚠ 2026-11-06 修：以前没有止损价时按 price*1% 当「假想止损」算仓位，
    # 但 executor 存的是 stop_price=None → DB 里 NULL → 引擎的止损分支永不执行。
    # 结果：仓位没有止损，却按 1% 的风险预算放大了 10 倍敞口。
    # 现在直接拒绝开仓（宁可不开，不能裸奔）。
    if not sig.stop_price or sig.stop_price <= 0:
        return 0
    stop_dist = abs(price - sig.stop_price)
    if stop_dist <= 0:
        return 0
    risk = scfg.get("risk") or {}
    capital, leverage, max_pos, used = _strategy_capacity(sig, scfg, equity, cfg_global)
    rpt = float(risk.get("risk_per_trade_pct") or 0) or cfg_global["risk_per_trade_pct"]
    risk_usd = capital * rpt / 100 * sig.size_mult
    qty = risk_usd / stop_dist
    notional = qty * price
    # ★ 2026-11-12 埋点（只读，不改变任何计算）：
    #   实盘发现「1R 金额不恒定」（LTC 52U / WLD 141U，目标 132U），
    #   导致赢的单子仓位被打折、输的吃满。这套 min() 链里到底哪一道在砍、
    #   还是 size_mult 在打折，光看结果反推不出来 —— 必须记下每道的原值。
    #   写进 audit_log（action='sizing'），一天后用 grep 一次看全。
    _need = notional
    _cap1 = max_pos
    _cap2 = max(capital * leverage - used, 0)
    # ★ 2026-11-12「移动判断」（浩哥定）：不给每个币固定占比，而是
    #   剩余容量 ÷ 剩余名额 —— 小单省下的额度自动让给后面的大单，
    #   额度不浪费。固定占比只留作防呆上限（yaml: max_position_pct_per_coin）。
    _slots = max(1, int(cfg_global["max_concurrent_coins"]) - len(db.open_positions()))
    _cap3 = min(equity * cfg_global["max_position_pct_per_coin"] / 100 * leverage,
                _cap2 / _slots)
    notional = min(notional, _cap1)      # per-position cap
    notional = min(notional, _cap2)      # strategy capacity left
    notional = min(notional, _cap3)      # 剩余容量 ÷ 剩余名额（移动平分）
    try:
        _why = []
        if _need > _cap1 + 1e-9:
            _why.append("cap1_max_pos")
        if _need > _cap2 + 1e-9:
            _why.append("cap2_capacity")
        if _need > _cap3 + 1e-9:
            _why.append("cap3_per_coin")
        db.audit("sizing", target=sig.symbol, detail={
            "side": sig.side,
            "equity": round(equity, 2),
            "price": price,
            "stop": sig.stop_price,
            "stop_pct": round(stop_dist / price * 100, 4),
            "rpt": rpt,
            "size_mult": sig.size_mult,
            "risk_usd": round(risk_usd, 2),      # 设计上的 1R
            "capital": round(capital, 2),
            "leverage": leverage,
            "used": round(used, 2),
            "need": round(_need, 2),             # 按公式该买多少
            "cap1": round(_cap1, 2),
            "cap2": round(_cap2, 2),
            "cap3": round(_cap3, 2),
            "final": round(notional, 2),         # 实际买到多少
            "ratio": round(notional / _need, 4) if _need > 0 else 0.0,
            "actual_r1": round(notional * (stop_dist / price), 2),  # 实际 1R
            "cut": _why or ["none"],
        }, actor="engine")
    except Exception:  # noqa: BLE001
        pass
    return notional / price
