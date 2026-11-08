"""Engine: two independent 0.5s loops.

- position_loop: every 0.5s manage open positions (stops / take-profit /
  trailing / time exits / 24h cap) against real-time WebSocket marks.
- scan_loop: every 0.5s refresh due factors and fire strategies whose own
  scan_interval has elapsed (data granularity still gates each strategy;
  trigger latency is now sub-second instead of a 30s tick).
Bookkeeping (equity snapshot, halt check, meta) is throttled to ~10s.
"""
import asyncio
import logging
import time

from . import config, db, executor, factors, risk, quality, shadow, daily_target
from .strategies import REGISTRY, Ctx

log = logging.getLogger("engine")

LOOP_SEC = 0.5
_READY_WARNED = [False]   # 监控池未就绪的告警只打一次
# 平仓失败的退避表 {position_id: 下次允许尝试的时间戳}
_CLOSE_BACKOFF: dict = {}
_CLOSE_RETRY_SEC = 30

_INTERVALS = {"1s": 1, "5s": 5, "15s": 15, "30s": 30, "1m": 60, "5m": 300,
              "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
_last_scan = {}

# K 线边界后等多久才扫（秒）。币安 K 线在整点收盘，但数据落库有 1~2 秒延迟，
# 留这点余量确保拿到的确实是收盘价，不是还在变的盘中价。
ALIGN_GRACE = 10


def _interval_sec(v):
    if isinstance(v, (int, float)):
        return max(1, int(v))
    return _INTERVALS.get(str(v), 900)


def _due(sid, scan_interval):
    """按 K 线边界对齐扫描（每根 K 线收盘后 ALIGN_GRACE 秒跑一次）。

    以前是「服务启动后每 N 秒跑一次」，相位由启动时刻决定、完全随机：
    18:08:25 启动 → 之后永远在 :08:25 / :23:25 / :38:25 / :53:25 扫。
    15m K 线在整点收盘，于是每次扫描都固定比收盘晚 8 分 25 秒，
    白白晚进场 8 分钟（2026-11-08 BEAMX 就是一例）。

    现在改成对齐：每根 15m K 线一收盘（19:00:00）+ 10 秒 = 19:00:10 扫，
    不管服务几点启动，相位都固定，K 线一收完就能判。

    grace 取 min(ALIGN_GRACE, iv//4)：防止 scan_interval 比 grace 还短时
    （例如 5s）每轮 tick 都判定成「该扫了」。
    """
    iv = _interval_sec(scan_interval)
    grace = min(ALIGN_GRACE, max(1, iv // 4))
    now = time.time()
    slot = int(now // iv) * iv + grace     # 本轮触发点
    if now < slot:                          # 还没到，用上一个边界
        slot -= iv
    if _last_scan.get(sid, 0) >= slot:
        return False
    _last_scan[sid] = now
    return True


def _strategies(cfg):
    out = {}
    for iid, scfg in cfg.get("strategies", {}).items():
        cls = REGISTRY.get(scfg.get("base") or iid)
        if cls:
            out[iid] = cls({**scfg, "_iid": iid})
    return out


# ── 缺价告警（限频，避免刷屏）────────────────────────────────────
# manage_positions 每 0.5 秒跑一次。缺价时旧代码直接 `continue`，
# 既不记日志也不告警：喂价一断，该仓位的止损/止盈/移动止损/持仓时限
# 全部不再执行，敞口无人看守，而系统看上去一切正常。
# 这里按 symbol 限频告警，并把"连续缺价超时"写进 meta 让前端能看见。
_PRICE_MISS = {}              # symbol -> [last_warn_ts, first_miss_ts, alerted]
_PRICE_MISS_WARN_GAP = 60     # 同一币两次告警的最小间隔（秒）
_PRICE_MISS_ALERT_SEC = 60    # 连续缺价超过这个时长升级为 error


def _note_price_miss(symbol, now):
    st = _PRICE_MISS.get(symbol)
    if st is None:
        _PRICE_MISS[symbol] = [now, now, False]
        db.log("warn", f"缺价：{symbol} 拿不到价格，该仓位的止损/止盈本轮未判断")
        return
    if now - st[0] >= _PRICE_MISS_WARN_GAP:
        st[0] = now
        db.log("warn", f"缺价持续 {int(now - st[1])} 秒：{symbol} 止损/止盈未生效")
    if not st[2] and now - st[1] >= _PRICE_MISS_ALERT_SEC:
        st[2] = True
        db.set_meta("last_price_miss",
                    f"{symbol} 缺价 {int(now - st[1])} 秒 @ {int(now)}")
        db.log("error", f"缺价超时 {int(now - st[1])} 秒：{symbol} 无法判断出场")


def _clear_price_miss(symbol):
    if _PRICE_MISS.pop(symbol, None) is not None:
        db.log("info", f"行情恢复：{symbol}")


async def manage_positions(ctx, strategies):
    for pos in db.open_positions():
        price = factors.mark_price_of(pos["symbol"])
        if not price:
            # 缺价不代表没有风险：仓位还在，只是这一轮没法判断出场。
            # 必须留下痕迹（限频），否则喂价断掉后系统会安静地裸奔。
            _note_price_miss(pos["symbol"], time.time())
            continue
        _clear_price_miss(pos["symbol"])
        entry = pos["entry_price"]
        if not entry or entry <= 0:
            # 脏数据（历史导入/手工补单）会让下面的除法抛 ZeroDivisionError，
            # 而外层只有一个整体 try → 所有仓位当轮全部不做风控。
            db.log("error", f"持仓 {pos['id']} {pos['symbol']} entry_price 非法({entry})，跳过")
            continue
        side, now = pos["side"], time.time()
        # MFE/MAE excursion tracking (monotonic -> sparse writes) for the journal
        sign = 1 if side == "long" else -1
        exc_pct = (price - entry) / entry * 100 * sign
        if exc_pct > (pos.get("mfe_pct") or 0):
            db.update_position(pos["id"], mfe_pct=exc_pct)
            pos["mfe_pct"] = exc_pct
        if exc_pct < (pos.get("mae_pct") or 0):
            db.update_position(pos["id"], mae_pct=exc_pct)
            pos["mae_pct"] = exc_pct
        reason = None
        if now - pos["opened_at"] >= pos["max_hold_sec"]:
            # ⚠ 到过 2R（half_closed=1）的单子豁免时间止损。
            # 不然「让利润奔跑」会被 16 小时硬砍 —— 回测里时间止损的条件是
            # peak_r < rr_target，也就是没到过止盈价的才按时间撤。
            # 实盘原来是无条件平，和回测口径不一致，会把正在走的趋势单砍掉。
            if not int(pos.get("half_closed") or 0):
                reason = "达到最长持仓时限"
        elif pos["time_exit_at"] and now >= pos["time_exit_at"]:
            reason = "策略时间出场"
        if not reason and pos["trail_atr"]:
            extreme = pos["extreme"] or pos["entry_price"]
            if side == "long":
                if price > extreme:
                    db.update_position(pos["id"], extreme=price)
                    extreme = price
                if extreme - price >= pos["trail_atr"]:
                    reason = "移动止损触发"
            else:
                if price < extreme:
                    db.update_position(pos["id"], extreme=price)
                    extreme = price
                if price - extreme >= pos["trail_atr"]:
                    reason = "移动止损触发"
        if not reason and pos["stop_price"]:
            if (side == "long" and price <= pos["stop_price"]) or \
               (side == "short" and price >= pos["stop_price"]):
                reason = "止损触发"
        if not reason and pos["take_profit"]:
            if (side == "long" and price >= pos["take_profit"]) or \
               (side == "short" and price <= pos["take_profit"]):
                reason = "止盈触发"
        if not reason:
            strat = strategies.get(pos["strategy"])
            if strat:
                try:
                    reason = await strat.check_exit(ctx, pos)
                except Exception as exc:  # noqa: BLE001
                    log.warning("check_exit %s failed: %s", pos["strategy"], exc)
        if reason:
            pid = pos["id"]
            # 退避：平仓失败后 0.5 秒又会被扫到，live 下会疯狂重复下单
            # （第一次可能其实成交了、只是回执超时 → 反复砸单）。
            if time.time() < _CLOSE_BACKOFF.get(pid, 0):
                continue
            # 策略层用 "HALF:" 前缀表示「只平一半，剩下继续持有」。
            # executor.close_position 早就支持 fraction，这里只需要分流。
            frac, label = 1.0, reason
            if reason.startswith("HALF:"):
                frac, label = 0.5, reason[5:]
            ok = await executor.close_position(pos, price, label, fraction=frac)
            if ok:
                _CLOSE_BACKOFF.pop(pid, None)
                if frac < 1.0:
                    # 落库标记，重启后靠它防止再平一次（否则仓被砍成 1/4）
                    db.update_position(pid, half_closed=1)
                    db.log("info", f"平一半 [{pos['strategy']}] {pos['symbol']} "
                                   f"{pos['side']} 剩余 qty={pos['qty'] * (1 - frac):.6g} "
                                   f"— {label}")
            else:
                _CLOSE_BACKOFF[pid] = time.time() + _CLOSE_RETRY_SEC
                db.log("warn", f"平仓未成功（{label}）{pos['symbol']}，"
                               f"{_CLOSE_RETRY_SEC}s 后重试")


async def position_loop():
    log.info("position loop started (%.1fs, ws-priced)", LOOP_SEC)
    while True:
        started = time.monotonic()
        try:
            cfg = config.load_strategies()
            ctx = Ctx(config.UNIVERSE, quality.monitor_list())
            await manage_positions(ctx, _strategies(cfg))
            db.set_meta("last_manage_ts_ms", int(time.time() * 1000))
        except Exception as exc:  # noqa: BLE001
            log.exception("position loop tick failed")
            db.set_meta("last_error", f"{int(time.time())}: manage: {exc}")
        await asyncio.sleep(max(LOOP_SEC - (time.monotonic() - started), 0.05))


async def scan_loop():
    log.info("scan loop started (%.1fs scheduler)", LOOP_SEC)
    db.set_meta("started_at", int(time.time()))
    last_book = 0.0
    equity_cache = config.PAPER_START_BALANCE
    _t0 = time.time()
    while True:
        started = time.monotonic()
        try:
            cfg = config.load_strategies()
            cfg_global = cfg["global"]
            _eq_ok = True          # 本轮权益读取是否可信（决定能不能清空 last_error）

            n = await factors.collect_due()
            if n:
                log.info("factors refreshed: %d", n)

            # bookkeeping ~10s: equity snapshot + halt check
            if time.time() - last_book >= 10:
                eq, bal, upnl = await executor.equity()
                # ⚠ 2026-11-06 修：live 取数失败时 equity() 返回 0，以前这个 0
                # 会被写进 equity_curve 当成「今日开盘净值」→ day_start_equity()=0
                # → 下面 _dse > 0 不成立 → 日目标/落袋/超时全平整天一次都不跑，
                # 而且不报错。现在：拿不到有效权益就整轮跳过记账，并把开仓停掉。
                if eq and eq > 0:
                    equity_cache = eq
                    db.record_equity(eq, bal, upnl)
                    risk.check_daily_halt(eq, cfg_global)
                    # 日目标（落袋）状态机
                    try:
                        _dse = db.day_start_equity()
                        _dtc = daily_target.cfg(cfg_global)
                        if _dse > 0:
                            await daily_target.tick(eq, _dse, executor.close_all, _dtc)
                    except Exception as _exc:  # noqa: BLE001
                        log.debug("daily_target tick failed: %s", _exc)
                else:
                    _eq_ok = False
                    equity_cache = 0
                    msg = f"{int(time.time())}: 权益读取失败(eq={eq})，暂停开仓"
                    db.set_meta("last_error", msg)
                    db.log("warn", msg)
                    log.warning(msg)
                last_book = time.time()

            # 池子还没算完就先别扫策略 —— 否则 monitor_list() 为空，
            # syms() 会回落到全量候选池 200 个币，既浪费流量又灌噪音信号。
            # 5 分钟后仍没就绪则放行（宁可回到老行为，也不能一直不交易）。
            if not quality.READY.is_set():
                if time.time() - _t0 < 300:
                    await asyncio.sleep(3)
                    continue
                if not _READY_WARNED[0]:
                    log.warning("监控池 5 分钟仍未就绪，放行全量扫描")
                    _READY_WARNED[0] = True

            strategies = _strategies(cfg) if equity_cache > 0 else {}
            ctx = Ctx(config.UNIVERSE, quality.monitor_list())
            for sid, scfg in cfg.get("strategies", {}).items():
                if not scfg.get("enabled") or sid not in strategies:
                    continue
                if not _due(sid, scfg.get("scan_interval", "15m")):
                    continue
                try:
                    _t0 = time.time()
                    signals = await strategies[sid].evaluate(ctx)
                    # 扫描成功以前是静默的，出不出信号都查不到「到底扫了没」，
                    # 排查「为什么这单晚了」时很被动。记一行节奏日志。
                    log.info("scan %s: %d 币 → %d 个信号，用时 %.1fs",
                             sid, len(ctx.monitor or ctx.universe),
                             len(signals), time.time() - _t0)
                except Exception as exc:  # noqa: BLE001
                    log.exception("strategy %s evaluate failed", sid)
                    db.log("error", f"{sid} 评估异常: {exc}")
                    continue
                # 择优入场：强度高且新鲜（翻转后根数少）的信号优先，
                # 避免"列表顺序先到先得"把额度浪费在弱信号上。
                signals.sort(key=lambda s: -(
                    getattr(s, "strength", 0.0)
                    / (1.0 + 0.3 * max(0, getattr(s, "fresh", 999)))))
                for sig in signals:
                    qok, qwhy = quality.is_tradable(sig.symbol)
                    if not qok:
                        db.log_signal(sig.strategy, sig.symbol, sig.side, sig.reason,
                                      False, f"杂讯过滤:{qwhy}")
                        continue
                    allowed, why, mult = risk.allow_entry(sig, scfg, equity_cache, cfg_global)
                    # 影子模式：不管开没开仓，都记录三套因子规则的判定 + 虚拟跟踪
                    try:
                        _px = factors.mark_price_of(sig.symbol) or sig.stop_price
                        shadow.record(sig, _px, sig.stop_price, sig.take_profit, acted=allowed)
                    except Exception as _exc:  # noqa: BLE001
                        log.debug("shadow record failed: %s", _exc)
                    if not allowed:
                        db.log_signal(sig.strategy, sig.symbol, sig.side, sig.reason, False, why)
                        continue
                    sig.size_mult *= mult
                    price = factors.mark_price_of(sig.symbol)
                    qty = risk.position_qty(sig, scfg, equity_cache, cfg_global, price)
                    leverage = int((scfg.get("risk") or {}).get("leverage")
                                   or cfg_global.get("max_gross_leverage", 3))
                    pid = await executor.open_position(sig, qty, price, leverage)
                    db.log_signal(sig.strategy, sig.symbol, sig.side, sig.reason,
                                  bool(pid), "" if pid else "执行失败/资金不足")
            try:
                shadow.settle(factors.mark_price_of)
            except Exception as _exc:  # noqa: BLE001
                log.debug("shadow settle failed: %s", _exc)
            db.set_meta("last_tick_ts", int(time.time()))
            # 只在权益可信时清错误。以前无条件清空，导致本 tick 前面刚写的
            # 「权益读取失败，暂停开仓」在同一轮就被抹掉，界面永远看不到原因。
            if _eq_ok:
                db.set_meta("last_error", "")
        except Exception as exc:  # noqa: BLE001
            log.exception("scan loop tick failed")
            db.set_meta("last_error", f"{int(time.time())}: {exc}")
        await asyncio.sleep(max(LOOP_SEC - (time.monotonic() - started), 0.05))
