"""Global risk layer — every entry signal passes through here.

Rules (from STRATEGIES.md): event quiet window, fear&greed sizing, per-coin
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


def daily_realized_pnl():
    return sum(p["pnl"] or 0 for p in db.closed_positions_today(_day_start()))


def halted():
    until = float(db.get_meta("halt_until", "0") or 0)
    return time.time() < until


def check_daily_halt(equity, cfg_global):
    pnl = daily_realized_pnl()
    if not (equity > 0 and pnl < 0 and abs(pnl) / equity * 100 >= cfg_global["daily_loss_halt_pct"]):
        return False
    # 已经熔断中就不要每 10 秒重写 meta、重打一条 warn（日志会被刷爆）
    if halted():
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
    missing, gross = [], 0.0
    for p in open_pos:
        px = factors.mark_price_of(p["symbol"])
        if not px or px <= 0:
            missing.append(p["symbol"])
            continue
        gross += abs(p["qty"] * px)
    if missing:
        return False, (f"有在仓币取不到行情({','.join(missing[:3])})，"
                       f"总敞口无法核算，暂停开仓"), 0
    if equity > 0 and gross / equity >= cfg_global["max_gross_leverage"]:
        return False, "总敞口已达杠杆上限", 0
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
    notional = min(notional, max_pos)                       # per-position cap
    notional = min(notional, max(capital * leverage - used, 0))  # strategy capacity left
    notional = min(notional, equity * cfg_global["max_position_pct_per_coin"] / 100
                   * leverage)                              # global per-coin cap (×lev)
    return notional / price
