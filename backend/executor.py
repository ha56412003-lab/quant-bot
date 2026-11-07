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


async def equity(m=None):
    """(equity, balance, unrealized) in USDT for the given mode (default: active).

    live 模式下取不到账户数据时返回 (0, 0, 0) —— 绝不回退到纸面余额。
    用纸面余额给实盘算仓位会导致超配（例如实盘只有 5000 却按 10000 下单）。
    调用方必须检查 equity<=0 并停止开仓（见 engine.py）。
    """
    m = m or mode()
    if m == "live":
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
    missing = []
    total = 0
    for p in db.open_positions(mode=m):
        total += 1
        price = factors.mark_price_of(p["symbol"])
        if price:
            sign = 1 if p["side"] == "long" else -1
            upnl += (price - p["entry_price"]) * p["qty"] * sign
        else:
            # 缺价时按开仓价估值（浮盈亏记 0）—— 这是保守做法，但净值会
            # 因此虚高或虚低，而日目标会拿这个净值判断"是否达标全平"。
            # 必须让调用方和用户知道这一轮净值不可信。
            missing.append(p["symbol"])
    if missing:
        db.set_meta("equity_missing_price",
                    f"{len(missing)}/{total} 仓缺价: {','.join(missing[:6])}")
    return bal + upnl, bal, upnl


async def open_position(sig, qty, price, leverage=None):
    """Fill an entry. Returns position id or None."""
    if qty <= 0 or price <= 0:
        return None
    if mode() == "live":
        venue = exchange()
        side = "BUY" if sig.side == "long" else "SELL"
        order = None
        try:
            if venue == "binance":
                step = await binance.qty_step(sig.symbol)
                qty = binance.round_step(qty, step)
                if qty <= 0:
                    return None
                # 逐仓是接实盘的生死线（浩哥 2026-11-07 定）：
                # 一个币爆仓不能带崩整个账户。设不了就不开这个仓 ——
                # 宁可错过一次机会，也不能在全仓里悄悄开出去。
                # 注：联保模式（多资产模式）下币安不允许逐仓，必须先切单币保证金模式。
                try:
                    await binance.set_margin_type(sig.symbol, "ISOLATED")
                except Exception as exc:  # noqa: BLE001
                    db.log("error",
                           f"live[binance] 设逐仓失败，放弃开仓 {sig.symbol} {sig.side}: {exc}"
                           f"（若提示联保模式，请先切「单币保证金模式」）")
                    return None
                if leverage:
                    await binance.set_leverage(sig.symbol, leverage)
                order = await binance.place_market_order(sig.symbol, side, qty)
                # 事后核对：钱已经进去了，万一实际不是逐仓只能告警 + 留痕，
                # 不能装作没发生。自检本身失败不阻断主流程。
                try:
                    actual = await binance.margin_of(sig.symbol)
                    if actual and actual != "isolated":
                        db.log("error",
                               f"live[binance] 严重 {sig.symbol} 实际保证金模式={actual}，"
                               f"不是逐仓！该仓暴露在全仓连带风险下，请立刻人工处理")
                except Exception:  # noqa: BLE001
                    pass
            else:
                if leverage:
                    await exchanges.set_leverage(venue, sig.symbol, leverage)
                order = await exchanges.place_market(venue, sig.symbol, side, qty)
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"live[{venue}] 开仓失败 {sig.symbol} {sig.side}: {exc}")
            return None
        # 成交价：优先用交易所回报的成交均价，取不到才回落到信号价。
        # （原代码此处引用了未定义的 pos 变量，实盘开仓会 NameError；
        #   且直接用指令价记账会在市价单里偏乐观。两处一并修掉。）
        fill = price
        if order:
            try:
                avg = float(order.get("avgPrice") or 0)
                if avg > 0:
                    fill = avg
                else:
                    executed = float(order.get("executedQty") or 0)
                    quote = float(order.get("cummulativeQuoteQty") or 0)
                    if executed > 0 and quote > 0:
                        fill = quote / executed
            except (TypeError, ValueError, AttributeError):
                pass
        # 费率：策略指定的优先（S15 是挂单策略，按 maker 记），否则用全局默认
        _live_fee = sig.fee_rate if sig.fee_rate is not None else config.PAPER_FEE_RATE
        fee = fill * qty * _live_fee
    else:
        # 策略可以用 sig.slip_pct / sig.fee_rate 覆盖全局口径（S15 是挂单策略，
        # 回测按 maker 0.02% + 零滑点算的，线上必须同口径才可比）
        _slip = config.PAPER_SLIPPAGE if sig.slip_pct is None else sig.slip_pct
        _fee_rate = config.PAPER_FEE_RATE if sig.fee_rate is None else sig.fee_rate
        slip = 1 + _slip if sig.side == "long" else 1 - _slip
        fill = price * slip
        fee = fill * qty * _fee_rate
        _adjust_paper_balance(-fee)
    pid = db.open_position(
        strategy=sig.strategy, symbol=sig.symbol, side=sig.side, qty=qty,
        entry_price=fill, stop_price=sig.stop_price or None,
        take_profit=sig.take_profit or None,
        trail_atr=sig.trail_dist or None,
        max_hold_sec=sig.max_hold_sec,
        time_exit_at=sig.time_exit_at or None,
        entry_factors=json.dumps(factors.snapshot_for(sig.symbol), ensure_ascii=False),
        reason=sig.reason, leverage=float(leverage or 0) or None,
        fee_rate=sig.fee_rate, slip_pct=sig.slip_pct)
    db.log_trade(sig.strategy, sig.symbol, sig.side, qty, fill, fee, "open", pid, None, mode())
    db.log("info", f"开仓 [{sig.strategy}] {sig.symbol} {sig.side} qty={qty:.6g} @ {fill:.6g} — {sig.reason}")
    return pid


def _close_fee_rate(pos):
    """平仓手续费率：优先用开仓时记下的口径（S15 是 maker 0.02%），
    没有记录就回落到全局默认。"""
    v = pos.get("fee_rate")
    return float(v) if v is not None else config.PAPER_FEE_RATE


def _close_slip_pct(pos):
    v = pos.get("slip_pct")
    return float(v) if v is not None else config.PAPER_SLIPPAGE


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
        fee = fill * qty * _close_fee_rate(pos)
    else:
        _slip = _close_slip_pct(pos)
        _rate = _close_fee_rate(pos)
        slip = 1 - _slip if side == "long" else 1 + _slip
        fill = price * slip
        fee = fill * qty * _rate
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
        # 部分平仓的盈亏必须累加进 positions.pnl。之前只写 trades 流水，
        # 等剩余仓位全平时被 close_position 覆盖 → 熔断低估亏损、页面显示反了。
        db.add_realized_pnl(pos["id"], pnl)
        db.log_trade(pos["strategy"], pos["symbol"],
                     "sell" if side == "long" else "buy", qty, fill, fee,
                     "partial_close", pos["id"], pnl, mode())
        journal.record_close({**pos, "qty": qty}, fill, pnl, fee,
                             f"{reason}（{frac*100:.0f}%）",
                             factors.snapshot_for(pos["symbol"]), mode())
        db.log("info", f"部分平仓 [{pos['strategy']}] {pos['symbol']} {frac*100:.0f}% "
                       f"pnl={pnl:+.2f} 剩余={remain:.6g} — {reason}")
    else:
        # 幂等：仓位可能已被另一条路径平掉（引擎 manage_positions / 手动 /
        # 日目标全平之间都有 await 让步点）。close_position 带 status='open' 条件，
        # 返回 False 表示没平成功 —— 此时必须撤销本轮的余额调整，否则会多记一次盈亏。
        closed_ok = db.close_position(pos["id"], fill, pnl, reason)
        if not closed_ok:
            if mode() != "live":
                _adjust_paper_balance(-pnl)
            db.log("warn", f"平仓跳过（仓位已平或已在处理中）[{pos['strategy']}] "
                           f"{pos['symbol']} — {reason}")
            return False
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
