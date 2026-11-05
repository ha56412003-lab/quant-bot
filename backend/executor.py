"""Order execution: paper (simulated fills, virtual balance) or live (Binance
USDⓈ-M market orders). Mode stored in DB meta; default paper. Every close is
appended to the trade journal (journal/) with entry/exit factor snapshots.
"""
import json
import logging

from . import binance, config, db, exchanges, factors, journal

log = logging.getLogger("executor")


def mode():
    return db.get_meta("mode", "paper")


def set_mode(m):
    db.set_meta("mode", "paper" if m != "live" else "live")


def exchange():
    """Live execution venue: binance (default) | bitget | okx."""
    v = db.get_meta("executor_exchange", "binance")
    return v if v in exchanges.SUPPORTED else "binance"


def set_exchange(venue):
    if venue not in exchanges.SUPPORTED:
        raise ValueError(f"unsupported exchange {venue}")
    if not exchanges.has_credentials(venue):
        raise ValueError(f"{venue} credentials not configured")
    db.set_meta("executor_exchange", venue)


def paper_balance():
    v = db.get_meta("paper_balance", "")
    if not v:
        db.set_meta("paper_balance", config.PAPER_START_BALANCE)
        return config.PAPER_START_BALANCE
    return float(v)


def _adjust_paper_balance(delta):
    db.set_meta("paper_balance", paper_balance() + delta)


async def equity():
    """(equity, balance, unrealized) in USDT for the active mode.

    live 模式下取不到账户数据时返回 (0, 0, 0) —— 绝不回退到纸面余额。
    用纸面余额给实盘算仓位会导致超配（例如实盘只有 5000 却按 10000 下单）。
    调用方必须检查 equity<=0 并停止开仓（见 engine.py）。
    """
    if mode() == "live":
        try:
            venue = exchange()
            if venue == "binance":
                acc = await binance.account()
                bal = float(acc.get("totalWalletBalance") or 0)
                upnl = float(acc.get("totalUnrealizedProfit") or 0)
                return bal + upnl, bal, upnl
            total, _avail = await exchanges.equity(venue)
            return total, total, 0.0
        except Exception as exc:  # noqa: BLE001
            log.error("live account fetch failed, 本轮禁止开仓: %s", exc)
            db.log("error", f"实盘账户读取失败，暂停开仓: {exc}")
            return 0.0, 0.0, 0.0
    bal = paper_balance()
    upnl = 0.0
    for p in db.open_positions():
        price = factors.mark_price_of(p["symbol"])
        if price:
            sign = 1 if p["side"] == "long" else -1
            upnl += (price - p["entry_price"]) * p["qty"] * sign
    return bal + upnl, bal, upnl


async def open_position(sig, qty, price, leverage=None):
    """Fill an entry. Returns position id or None."""
    if qty <= 0 or price <= 0:
        return None
    if mode() == "live":
        venue = exchange()
        side = "BUY" if sig.side == "long" else "SELL"
        try:
            if venue == "binance":
                step = await binance.qty_step(sig.symbol)
                qty = binance.round_step(qty, step)
                if qty <= 0:
                    return None
                if leverage:
                    await binance.set_leverage(sig.symbol, leverage)
                await binance.place_market_order(sig.symbol, side, qty)
            else:
                if leverage:
                    await exchanges.set_leverage(venue, sig.symbol, leverage)
                await exchanges.place_market(venue, sig.symbol, side, qty)
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"live[{venue}] 开仓失败 {sig.symbol} {sig.side}: {exc}")
            return None
        fill = price
        fee = fill * qty * 0.0005
    else:
        slip = 1 + config.PAPER_SLIPPAGE if sig.side == "long" else 1 - config.PAPER_SLIPPAGE
        fill = price * slip
        fee = fill * qty * config.PAPER_FEE_RATE
        _adjust_paper_balance(-fee)
    pid = db.open_position(
        strategy=sig.strategy, symbol=sig.symbol, side=sig.side, qty=qty,
        entry_price=fill, stop_price=sig.stop_price or None,
        take_profit=sig.take_profit or None,
        trail_atr=sig.trail_dist or None,
        max_hold_sec=sig.max_hold_sec,
        time_exit_at=sig.time_exit_at or None,
        entry_factors=json.dumps(factors.snapshot_for(sig.symbol), ensure_ascii=False),
        reason=sig.reason, leverage=float(leverage or 0) or None)
    db.log_trade(sig.strategy, sig.symbol, sig.side, qty, fill, fee, "open", pid, None, mode())
    db.log("info", f"开仓 [{sig.strategy}] {sig.symbol} {sig.side} qty={qty:.6g} @ {fill:.6g} — {sig.reason}")
    return pid


async def close_position(pos, price, reason, fraction=1.0):
    """平仓。fraction=1.0 → 全平；0<fraction<1 → 部分平仓，剩余仓位继续持有。"""
    full_qty = float(pos["qty"])
    frac = max(0.01, min(1.0, float(fraction or 1.0)))
    partial = frac < 0.999
    qty, side = (full_qty * frac if partial else full_qty), pos["side"]
    if mode() == "live":
        venue = exchange()
        order_side = "SELL" if side == "long" else "BUY"
        try:
            if venue == "binance":
                step = await binance.qty_step(pos["symbol"])
                await binance.place_market_order(pos["symbol"], order_side,
                                                 binance.round_step(qty, step),
                                                 reduce_only=True)
            else:
                await exchanges.place_market(venue, pos["symbol"], order_side,
                                             qty, reduce_only=True)
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"live[{venue}] 平仓失败 {pos['symbol']}: {exc}")
            return False
        fill = price
        fee = fill * qty * 0.0005
    else:
        slip = 1 - config.PAPER_SLIPPAGE if side == "long" else 1 + config.PAPER_SLIPPAGE
        fill = price * slip
        fee = fill * qty * config.PAPER_FEE_RATE
    sign = 1 if side == "long" else -1
    pnl = (fill - pos["entry_price"]) * qty * sign - fee
    if mode() != "live":
        _adjust_paper_balance(pnl)
    if partial:
        # 部分平仓：只减仓，剩余数量与追加保证金按比例保留，仓位保持 open
        # 注意：positions 表没有 margin 列（保证金是算出来的：qty*entry/leverage），只有 extra_margin
        remain = full_qty - qty
        old_extra = float(pos["extra_margin"] or 0)
        db.update_position(pos["id"], qty=remain, extra_margin=old_extra * (1 - frac))
        db.log_trade(pos["strategy"], pos["symbol"],
                     "sell" if side == "long" else "buy", qty, fill, fee,
                     "partial_close", pos["id"], pnl, mode())
        journal.record_close({**pos, "qty": qty}, fill, pnl, fee,
                             f"{reason}（{frac*100:.0f}%）",
                             factors.snapshot_for(pos["symbol"]), mode())
        db.log("info", f"部分平仓 [{pos['strategy']}] {pos['symbol']} {frac*100:.0f}% "
                       f"pnl={pnl:+.2f} 剩余={remain:.6g} — {reason}")
    else:
        db.close_position(pos["id"], fill, pnl, reason)
        db.log_trade(pos["strategy"], pos["symbol"],
                     "sell" if side == "long" else "buy", qty, fill, fee,
                     "close", pos["id"], pnl, mode())
        journal.record_close(pos, fill, pnl, fee, reason,
                             factors.snapshot_for(pos["symbol"]), mode())
        db.log("info", f"平仓 [{pos['strategy']}] {pos['symbol']} {side} pnl={pnl:+.2f} — {reason}")
    return True


async def close_all(reason="一键全平"):
    """平掉所有持仓，返回 (成功数, 失败数)。

    拿不到价格的仓位：
      - paper 模式 → 无法模拟成交，记为失败并写日志（不能假装平掉了）
      - live  模式 → 下单不需要本地价格，用开仓价兜底（reduce_only 市价单真实成交）
    """
    closed = failed = 0
    for pos in db.open_positions():
        price = factors.mark_price_of(pos["symbol"])
        if not price:
            if mode() == "live":
                price = float(pos["entry_price"])
                db.log("warn", f"一键全平：{pos['symbol']} 无行情价，用开仓价记 PnL")
            else:
                db.log("error", f"一键全平跳过 {pos['symbol']}：无可用价格，无法模拟成交")
                failed += 1
                continue
        if await close_position(pos, price, reason):
            closed += 1
        else:
            failed += 1
    if failed:
        db.log("error", f"一键全平完成：成功 {closed}，失败 {failed}")
    return closed, failed
