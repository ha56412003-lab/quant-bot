"""Order execution: paper (simulated fills, virtual balance) or live (Binance
USDⓈ-M market orders). Mode stored in DB meta; default paper. Every close is
appended to the trade journal (journal/) with entry/exit factor snapshots.
"""
import asyncio
import json
import logging
import math
import time

from . import binance, config, db, exchanges, factors, journal

log = logging.getLogger("executor")

# 每个仓位一把平仓锁。
# 为什么需要：部分平仓是「读快照 qty → 算 → 下单 → 写回 remain」，中间有 await。
# 引擎半平 + 手动半平 / 用户双击确认按钮，两个请求会拿到同一个 qty 各卖一次：
# 交易所已经清零，本地还剩一半，paper_balance 和 positions.pnl 双记。
# db 层的 status='open' 条件只能挡住「已平」，挡不住「同时两次部分平仓」。
_pos_locks: dict = {}


def _pos_lock(pid):
    lk = _pos_locks.get(pid)
    if lk is None:
        lk = asyncio.Lock()
        _pos_locks[pid] = lk
    return lk


def mode():
    return db.get_meta("mode", "paper")


def set_mode(m, force=False):
    """切换交易模式。

    ⚠ 2026-11-09 加：切回 paper 前必须确认实盘仓位已空。
    原因：db.open_positions() 按**当前模式**过滤，engine.manage_positions 正是
    用它遍历 —— 切到 paper 的那一刻，实盘仓位立刻失去硬止损 / 保本 1.5R /
    平半 1.2R / 全平 2.5R / 右侧止盈 的全部管理，而交易所侧没有止损单，等于永久裸奔。
    API 层也有一道同样的闸门，这里是防「绕过接口直接调」的兜底。
    force=True 仅供测试清理用，业务代码不许传。
    """
    target = "paper" if m != "live" else "live"
    if target == "paper" and not force and mode() == "live":
        live_open = db.open_positions(mode="live")
        if live_open:
            syms = ", ".join(p["symbol"] for p in live_open[:6])
            more = f" 等 {len(live_open)} 个" if len(live_open) > 6 else ""
            raise RuntimeError(
                f"实盘还有持仓（{syms}{more}），切回纸面前必须先全部平仓，"
                f"否则它们将失去全部止损管理")
    db.set_meta("mode", target)


# 账户持仓模式缓存：双向/单向是账户级设置，不会每笔单变。
# 300 秒内复用，避免每笔下单都多打一次接口；查不到时按单向处理
# （跟改动前的写法一致，不会比原来更差）。
_hedge_cache = {"ts": 0.0, "val": None}


async def _is_hedge():
    """账户是否双向持仓（hedge）。True/False。"""
    now = time.time()
    if _hedge_cache["val"] is not None and now - _hedge_cache["ts"] < 300:
        return _hedge_cache["val"]
    try:
        v = await binance.position_mode()
        _hedge_cache.update(ts=now, val=v)
        return v
    except Exception:  # noqa: BLE001
        return False


def _pside(side, hedge):
    """双向模式下要传的 positionSide；单向返回 None（不传这个字段）。"""
    if not hedge:
        return None
    return "LONG" if side == "long" else "SHORT"


def exchange():
    """Live execution venue: binance (default) | bitget | okx."""
    v = db.get_meta("executor_exchange", "binance")
    return v if v in exchanges.SUPPORTED else "binance"


def set_exchange(venue):
    if venue not in exchanges.SUPPORTED:
        raise ValueError(f"unsupported exchange {venue}")
    if not exchanges.has_credentials(venue):
        raise ValueError(f"{venue} credentials not configured")
    # ⚠ 2026-11-09：实盘有持仓时不许换交易所。
    # 换完之后新仓位开在新家，旧仓位还在老家 —— close_position 现在会按
    # positions.exchange 平回原交易所，但账户状态（保证金、可用余额、
    # 逐仓设置）已经分叉，体检和风控都只看得见一家，很容易出事。
    if mode() == "live" and venue != exchange():
        live_open = db.open_positions(mode="live")
        if live_open:
            cur = exchange()
            others = sorted({(p.get("exchange") or cur) for p in live_open})
            raise RuntimeError(
                f"实盘还有 {len(live_open)} 个持仓在 {', '.join(others)}，"
                f"全部平掉之后才能切换到 {venue}")
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
    # NaN 穿透陷阱：float('nan') <= 0 是 False，能绕过原来的 >0 校验，
    # 一路写进 sqlite 变成 NULL 撞 NOT NULL → IntegrityError。
    if not (math.isfinite(qty) and qty > 0
            and math.isfinite(price) and price > 0):
        return None
    # 先占位：下面写库失败的兜底分支要用 side/venue，而 mode() 每次调用都可能变，
    # 不能依赖「前面 if 里赋过值」。
    venue = ""
    side = ""
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
                _h = await _is_hedge()
                order = await binance.place_market_order(
                    sig.symbol, side, qty, position_side=_pside(sig.side, _h))
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
    # ⚠ 2026-11-09：db.open_position 原来在 try 外面，且纸面手续费在它之前就扣了。
    # 实测（tests/audit_live_open.py）：下单成功后写库抛 database is locked →
    # 钱在交易所、本地零记录 → 永久裸仓，服务重启也找不到它；
    # 纸面则是手续费白扣、仓位不存在。现在写库失败必须立刻反向平掉/退回钱。
    try:
        pid = db.open_position(
            strategy=sig.strategy, symbol=sig.symbol, side=sig.side, qty=qty,
            entry_price=fill, stop_price=sig.stop_price or None,
            max_hold_sec=sig.max_hold_sec,
            entry_factors=json.dumps(factors.snapshot_for(sig.symbol), ensure_ascii=False),
            reason=sig.reason, leverage=float(leverage or 0) or None,
            fee_rate=sig.fee_rate, slip_pct=sig.slip_pct,
            # VS15-1.5 第④条：硬止损收紧价，开仓算死（0 = 这笔没有第④条）
            stop_tight=float(getattr(sig, "stop_tight", 0.0) or 0.0) or None,
            # 记下这笔仓位到底开在哪家交易所。平仓必须平回同一家，
            # 否则切换执行交易所之后，旧仓位会被拿到新交易所去平。
            exchange=(venue or exchange()))
    except Exception as exc:  # noqa: BLE001
        db.log("error", f"开仓写库失败 {sig.symbol} {sig.side}，订单已发出，回滚中: {exc}")
        if mode() == "live":
            # 钱已经真的进去了 —— 必须立刻反向平掉，否则就是无人看管的裸仓
            _undo_side = "SELL" if side == "BUY" else "BUY"
            try:
                if venue == "binance":
                    _h2 = await _is_hedge()
                    await binance.place_market_order(
                        sig.symbol, _undo_side, qty, reduce_only=True,
                        position_side=_pside(sig.side, _h2))
                else:
                    await exchanges.place_market(venue, sig.symbol, _undo_side, qty,
                                                 reduce_only=True)
                db.log("error", f"已反向平掉裸仓 {sig.symbol} {sig.side} qty={qty:.6g}"
                                f"（本地写库失败，交易所订单已撤销）")
            except Exception as exc2:  # noqa: BLE001
                # 最坏情况：连平仓也失败 → 必须留红字让人立刻手动处理
                db.log("error", f"★★ 裸仓救援失败！交易所仍有 {sig.symbol} {sig.side} "
                                f"qty={qty:.6g} 且本地无记录，请立刻人工平仓: {exc2}")
        else:
            _adjust_paper_balance(fee)     # 纸面没成交，把刚扣的手续费退回去
        return None
    if mode() != "live":
        _adjust_paper_balance(-fee)        # 落库成功后才扣手续费
    db.log_trade(sig.strategy, sig.symbol, sig.side, qty, fill, fee, "open", pid, None, mode())
    db.log("info", f"开仓 [{sig.strategy}] {sig.symbol} {sig.side} qty={qty:.6g} @ {fill:.6g} — {sig.reason}")
    return pid


def _parse_fill(order, fallback_price):
    """从交易所下单回执里取 (真实成交数量, 真实成交均价)。

    返回 (qty, price, known)：
      qty   > 0 → 真实成交数量（可能小于下单数量，即部分成交）
      price > 0 → 真实成交均价
      known     → True 表示"回执里有成交明细"，False 表示交易所没返回明细

    三家格式不同，且只有币安的下单接口直接返回成交明细：
      Binance: {"executedQty","avgPrice","cummulativeQuoteQty","status"}
      OKX:     下单只返回 [{"ordId","sCode"}]，成交明细要另查 /api/v5/trade/order
      Bitget:  下单只返回 {"orderId"}，成交明细要另查 /api/v2/mix/order/detail

    ⚠ 语义约定（这是资金安全的关键）：
      - 返回 qty=0 且 known=False → 交易所没给明细，**不代表失败**
        （OKX/Bitget 拒单已经在外层 raise 掉了），调用方用本地价记账 + 记告警
      - 币安返回 qty=0 且 known=True → **真的没成交**，调用方必须当失败处理
    """
    order = order or {}
    # 关键：判断「有没有成交明细」要看字段存不存在，不是看值大不大。
    # 币安返回 executedQty="0" 表示「有明细，但一根没成交」——这是真失败；
    # OKX/Bitget 的下单回执里根本没有 executedQty 字段——这才是「没明细」。
    has_detail = "executedQty" in order or "cummulativeQuoteQty" in order
    try:
        q = float(order.get("executedQty") or 0)
    except (TypeError, ValueError, AttributeError):
        q = 0.0
    if q > 0:
        p = 0.0
        try:
            p = float(order.get("avgPrice") or 0)
            if p <= 0:
                quote = float(order.get("cummulativeQuoteQty") or 0)
                if quote > 0:
                    p = quote / q
        except (TypeError, ValueError, AttributeError):
            p = 0.0
        if p > 0:
            return q, p, True
        # 有数量没均价 → 有明细但价格缺失，退回本地价
        return q, float(fallback_price or 0), True
    # q=0：币安的话 has_detail=True → 调用方必须当「没成交」处理
    return 0.0, float(fallback_price or 0), has_detail


def _close_fee_rate(pos):
    """平仓手续费率：优先用开仓时记下的口径（S15 是 maker 0.02%），
    没有记录就回落到全局默认。"""
    v = pos.get("fee_rate")
    return float(v) if v is not None else config.PAPER_FEE_RATE


def _close_slip_pct(pos):
    v = pos.get("slip_pct")
    return float(v) if v is not None else config.PAPER_SLIPPAGE


async def close_position(pos, price, reason, fraction=1.0):
    """平仓入口。同一仓位的平仓请求串行执行（见 _pos_lock）。"""
    pid = pos["id"]
    async with _pos_lock(pid):
        try:
            # ⚠ 拿到锁之后必须重新读库：调用方传进来的 pos 是**快照**，
            # 排队期间前面的请求可能已经减过数量甚至平了仓。
            # 用旧快照算 remain 会把别人减掉的量又写回去（实测：两次 50%
            # 半平后本地仍是 50，而交易所已经卖光）。
            fresh = db.get_position(pid)
            if not fresh or fresh["status"] != "open":
                db.log("warn", f"平仓跳过：仓位 {pid} 已不在持仓中 — {reason}")
                return False
            try:
                if float(fresh.get("qty") or 0) <= 0:
                    db.log("warn", f"平仓跳过：仓位 {pid} 数量已是 0 — {reason}")
                    return False
            except (TypeError, ValueError):
                return False
            return await _close_position_inner(fresh, price, reason, fraction)
        finally:
            # 仓位已平 → 锁没人用了，清掉避免字典无限增长。
            # 注意：部分平仓时仓位仍是 open，锁要留着（下一次半平还要用）。
            cur = db.get_position(pid)
            if not cur or cur["status"] != "open":
                _pos_locks.pop(pid, None)


async def _close_position_inner(pos, price, reason, fraction=1.0):
    """平仓。fraction=1.0 → 全平；0<fraction<1 → 部分平仓，剩余仓位继续持有。"""
    full_qty = float(pos["qty"])
    # ⚠ 2026-11-09：原来是 float(fraction or 1.0) —— fraction=0 会变成 1.0 然后全平。
    # 0 是「平 0%」，无意义且危险，clamp 到最小值（1%）而不是翻成全平。
    try:
        _f = float(fraction) if fraction is not None else 1.0
    except (TypeError, ValueError):
        _f = 1.0
    frac = max(0.01, min(1.0, _f))
    partial = frac < 0.999
    qty, side = (full_qty * frac if partial else full_qty), pos["side"]
    if mode() == "live":
        # 平回**开仓那家**，不是「当前选中的那家」。切换执行交易所后，
        # 旧仓位仍在原交易所；用新 venue 去平会失败（或平错同名合约）。
        # 历史仓位 exchange 为空 → 退回当前设置（那时还没这个字段）。
        venue = (pos.get("exchange") or "").strip() or exchange()
        order_side = "SELL" if side == "long" else "BUY"
        try:
            if venue == "binance":
                step = await binance.qty_step(pos["symbol"])
                _h = await _is_hedge()
                _send_qty = binance.round_step(qty, step)
                if _send_qty <= 0:
                    # 舍入后变成 0 的仓位是「交易所允许的最小单位以下」，
                    # 下单会被拒（-4003 / -1013）。明确拒绝，不要把仓位标成已平。
                    db.log("error", f"live[binance] 平仓数量取整后为 0，无法下单 "
                                    f"{pos['symbol']} 理论 qty={qty:.10g} step={step}")
                    return False
                order = await binance.place_market_order(
                    pos["symbol"], order_side, _send_qty,
                    reduce_only=True, position_side=_pside(pos["side"], _h))
            else:
                order = await exchanges.place_market(venue, pos["symbol"], order_side,
                                                     qty, reduce_only=True)
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"live[{venue}] 平仓失败 {pos['symbol']}: {exc}")
            return False

        # ── 回执核对 ────────────────────────────────────────────────
        # 2026-11-09 修：此前这里直接 `fill = price` 并丢弃回执，
        # 结果是「交易所拒单 / 只成交一部分」都被当成全平成功：
        #   · 本地标记已平，交易所有仓 → 裸仓，无止损无出场
        #   · 盈亏用本地价记，和真实成交价偏差可达数倍 → 熔断/日目标判断失真
        if not order:
            # 回执是 None / 空 —— 交易所什么都没给。可能是拒单、可能是响应异常。
            # 无论哪种都**不能**当成平掉了：仓位保留，留给下一轮重试 / 人工处理。
            db.log("error", f"live[{venue}] 平仓无回执 {pos['symbol']}，"
                            f"无法确认是否成交，仓位保留")
            return False
        exec_qty, fill, known = _parse_fill(order, price)
        if known and exec_qty <= 0:
            db.log("error", f"live[{venue}] 平仓未成交 {pos['symbol']} "
                            f"status={(order or {}).get('status')}，仓位保留")
            return False
        if not known:
            # OKX / Bitget 的下单回执里没有成交明细（真正的拒单已经在外层 raise 掉了）。
            # 成交价只能沿用本地价 —— 必须留痕，否则账目会静默偏离真实余额。
            exec_qty = qty
            db.log("warn", f"live[{venue}] 平仓回执无成交明细 {pos['symbol']}，"
                           f"按本地价 {price:.6g} 记账（存在滑点偏差可能）")
        if exec_qty < qty - 1e-9:
            # 只成交了一部分：按真实卖掉的数量记账，剩下的继续持有。
            # 之前把整笔仓位标成已平 → 剩下的在交易所彻底失控。
            db.log("warn", f"live[{venue}] 平仓部分成交 {pos['symbol']} "
                           f"{exec_qty:.10g} / 应平 {qty:.10g}，剩余继续持有")
        qty = exec_qty
        if qty < full_qty - 1e-9:
            partial = True          # 部分成交 → 走部分平仓分支，仓位保持 open
        fee = fill * qty * _close_fee_rate(pos)
    else:
        _slip = _close_slip_pct(pos)
        _rate = _close_fee_rate(pos)
        slip = 1 - _slip if side == "long" else 1 + _slip
        fill = price * slip
        fee = fill * qty * _rate
    sign = 1 if side == "long" else -1
    pnl = (fill - pos["entry_price"]) * qty * sign - fee
    # ⚠ 2026-11-09 改成「先落库、后动钱」。
    # 原来是先 _adjust_paper_balance(pnl) 再落库，落库抛异常时（实测日志里真的有
    # database is locked）余额已经加了、仓位还 open → 0.5 秒后引擎重读到 open
    # 再平一次，钱加两遍。现在落库失败就直接返回，钱一分没动，不需要回滚。
    if partial:
        # 部分平仓：只减仓，剩余数量与追加保证金按比例保留，仓位保持 open
        # 注意：positions 表没有 margin 列（保证金是算出来的：qty*entry/leverage），只有 extra_margin
        remain = full_qty - qty
        old_extra = float(pos["extra_margin"] or 0)
        try:
            # 这两步都带 status='open' 条件：仓位可能已被另一条路径平掉
            # （引擎半平 / 手动半平 / 日目标全平之间都有 await 让步点）。
            # rowcount=0 说明没写进去，此时绝不能记账。
            ok_qty = db.update_position(
                pos["id"], qty=remain, extra_margin=old_extra * (1 - frac))
            ok_pnl = db.add_realized_pnl(pos["id"], pnl) if ok_qty else False
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"部分平仓落库失败 {pos['symbol']}，未记账: {exc}")
            return False
        if not (ok_qty and ok_pnl):
            db.log("warn", f"部分平仓跳过（仓位已平或已在处理中）[{pos['strategy']}] "
                           f"{pos['symbol']} — {reason}")
            return False
        if mode() != "live":
            _adjust_paper_balance(pnl)
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
        # 日目标全平之间都有 await 让步点）。close_position 带 status='open' 条件。
        try:
            closed_ok = db.close_position(pos["id"], fill, pnl, reason)
        except Exception as exc:  # noqa: BLE001
            db.log("error", f"平仓落库失败 {pos['symbol']}，未记账: {exc}")
            return False
        if not closed_ok:
            db.log("warn", f"平仓跳过（仓位已平或已在处理中）[{pos['strategy']}] "
                           f"{pos['symbol']} — {reason}")
            return False
        if mode() != "live":
            _adjust_paper_balance(pnl)
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
