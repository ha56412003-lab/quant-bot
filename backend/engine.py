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
import math
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


def _same_bar_allow(cnt, side, cap):
    """同一信号时点同向名额判定（纯函数，不改计数，调用方负责 +1）。

    cap <= 0 → 不限（恒 True）。
    口径与 scripts/exp_sameside_1111.py::cap_same_bar 完全一致：
    cap=2 表示「同一根 15m K 线同一方向最多开 2 笔」。
    ★ 抽成纯函数只是为了能单测（scripts/verify_same_bar_cap.py），
      逻辑本身没有任何隐藏状态。
    """
    return cap <= 0 or cnt.get(side, 0) < cap


def _hard_stop_reason(pos, price):
    """④ 硬止损判定（纯函数，抽取出来为了能单测）。

    2026-11-11 加了保本移损：峰顶到过 be_at_r 后 strategies.check_exit 会置
    be_on=1，这里就把触发价从「原始止损」换成「入场价」。
    ★ 为什么不直接改 pos["stop_price"]：它是全系统算 R 的分母
      （abs(entry-stop_price)），改成 entry 就除零，整套 R 体系崩掉。

    返回 (reason, be_triggered)：
      - 没触发     → (None, False)
      - 原始止损   → ("止损触发", False)
      - 第④条收紧 → ("时段翻转平", False)  ← VS15-1.5（判据=时段，不是 CE dir）
      - 保本止损   → ("保本止损", True)
    """
    stop = pos.get("stop_price")
    side = pos.get("side")
    if not stop or side not in ("long", "short"):
        return None, False
    be_on = int(pos.get("be_on") or 0)
    # VS15-1.5 第④条：时段已翻到反面且峰顶没到过 be_at_r → 用收紧价。
    #   与 be_on 互斥（be_on 是峰顶 ≥ be_at_r 才置位，第④条只在峰顶 < be_at_r 时武装），
    #   万一历史数据两条都为 1，保本优先（它对交易者更有利，且不会放宽止损）。
    _tight_on = int(pos.get("stop_tight_on") or 0)
    _tight = pos.get("stop_tight")
    try:
        if be_on:
            stop_eff = float(pos["entry_price"])
        elif _tight_on and _tight and float(_tight) > 0:
            stop_eff = float(_tight)
        else:
            stop_eff = float(stop)
    except (KeyError, TypeError, ValueError):
        return None, False
    if not stop_eff or not math.isfinite(stop_eff):
        return None, False
    hit = (price <= stop_eff) if side == "long" else (price >= stop_eff)
    if not hit:
        return None, False
    if be_on:
        return "保本止损", True
    if _tight_on and _tight and float(_tight) > 0:
        return "时段翻转平", False
    return "止损触发", False


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
        # 2026-11-10 B 档：时间止损整条删除（max_hold_sec = 0）。
        # ⚠ 这个 `pos["max_hold_sec"] and` 是安全阀，不是装饰：
        #   不留它，`now - opened_at >= 0` 恒真 → 每个仓位开出来就被立刻平掉。
        if pos["max_hold_sec"] and now - pos["opened_at"] >= pos["max_hold_sec"]:
            # 2026-11-09 浩哥定：去掉「平半豁免」，到点一律平。
            # 原先 half_closed=1 就永不受时间约束，导致平半线越低长尾越长：
            # 1.0 半时出现过 90.0 小时的单子，1.8 半也有 42.5 小时。
            # 回测（20币/60天/cap5%/1.8半/移动1.5-1.2）：
            #   去掉豁免 12h → +94.2R 最长 12.0h
            #   保留豁免 12h → +86.7R 最长 42.5h   （只差 0.8R，买掉 30 小时尾巴）
            # 「让利润奔跑」靠 ② 2.5R 兜底 + ③ 右侧止盈来做，不靠免死金牌。
            # （移动止盈已于 2026-11-10 整条删除，这里不再提它。）
            reason = "达到最长持仓时限"
        # ④ 硬止损 —— 判定抽成纯函数 _hard_stop_reason()，便于单测
        #    （2026-11-11 加保本移损后触发价有两种可能，必须能单独验）
        if not reason:
            reason, _be_hit = _hard_stop_reason(pos, price)
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


def _heartbeat(key, value):
    """写心跳。

    ★ 2026-11-10 修的 BUG：原来这里是裸的 db.set_meta()，一旦撞上写锁
    （sqlite3.OperationalError: database is locked —— 每天 08:16 质量/因子日刷新
    的长事务必撞），异常会一路冒出 tick 的 try 块 → **整个 0.5s 循环退出** →
    看门狗再把它拉起来，中间 20~30 秒里止损/止盈完全停摆。
    心跳只是给看门狗看的存活标记，写不进去最多让看门狗误判一次，
    绝不能因此拖死真正管钱的循环。所以这里吞掉异常，循环继续跑。
    """
    if not db.heartbeat_meta(key, value):
        log.warning("心跳写入失败(%s)：本轮跳过，循环继续（不影响止损/止盈判断）", key)


async def position_loop():
    log.info("position loop started (%.1fs, ws-priced)", LOOP_SEC)
    while True:
        started = time.monotonic()
        try:
            cfg = config.load_strategies()
            ctx = Ctx(config.UNIVERSE, quality.monitor_list())
            await manage_positions(ctx, _strategies(cfg))
            _heartbeat("last_manage_ts_ms", int(time.time() * 1000))
        except Exception as exc:  # noqa: BLE001
            log.exception("position loop tick failed")
            db.set_meta("last_error", f"{int(time.time())}: manage: {exc}")
        await asyncio.sleep(max(LOOP_SEC - (time.monotonic() - started), 0.05))


async def scan_loop():
    log.info("scan loop started (%.1fs scheduler)", LOOP_SEC)
    db.set_meta("started_at", int(time.time()))
    last_book = 0.0
    equity_cache = config.PAPER_START_BALANCE
    _boot_ts = time.time()      # 进程启动时刻（只用于「监控池等了多久」）
    _t0 = time.time()           # 每轮扫描计时（下面会被反复覆盖）
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
            # ⚠ 2026-11-09 修两处：
            #   ① 这里原来用 _t0，而 _t0 在下面扫描时被覆盖成「本轮开始时间」，
            #      一旦 READY 曾经 set 过又变回 clear，「等了 5 分钟」永远不成立 →
            #      永久静默不交易。现在改用独立的 _boot_ts。
            #   ② 原来等满 5 分钟会「放行全量扫描」。但 is_tradable 已改成
            #      空池 fail-closed，放行了也全被拦；而且风控没准备好时本来
            #      就不该开仓。现在改成持续告警 + 继续等待。
            if not quality.READY.is_set():
                waited = time.time() - _boot_ts
                if waited < 300:
                    await asyncio.sleep(3)
                    continue
                if not _READY_WARNED[0]:
                    msg = f"监控池启动 {int(waited)}s 仍未就绪，暂停开仓（其余风控照常）"
                    log.error(msg)
                    db.log("error", msg)
                    _READY_WARNED[0] = True
                await asyncio.sleep(3)
                continue
            if _READY_WARNED[0]:
                # 恢复后要能再报一次，否则下次故障就不告警了
                _READY_WARNED[0] = False
                db.log("info", "监控池已就绪，恢复扫描")

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
                # 择优入场的排序（2026-11-11 浩哥定 B 方案时一并改）。
                #   strength = |收盘 − ZLSMA50| / ATR = 「离均线多远」。
                #   ★ 以前是降序（strength 大的先开）—— 那是「专挑追得最狠的先上」，
                #     等于在最没空间的位置入场。改成升序：离均线最近的先开
                #     = 刚翻转、还没跑远的那笔，空间才大。
                #   fresh = 距信号触发已过几根 K 线，永远越小越好（所以单独升序放第二位）。
                #   实测（scripts/exp_sameside_order_1111.py，限 2 笔）：
                #     降序 54.8% / 1.34 / +44.33R  →  升序 56.3% / 1.43 / +53.75R
                signals.sort(key=lambda s: (
                    getattr(s, "strength", 0.0),
                    max(0, getattr(s, "fresh", 999))))
                # ── 同一信号时点同向限笔（2026-11-11 浩哥定：2 笔）
                #    同一根 15m K 线，40 个币会同时算出同向信号 —— 那不是 N 个独立
                #    机会，是 1 个机会 × N 个标的（信号共线）。11-10 23:45:22 因此
                #    一秒开了 6 个空，V 型反转全灭，已平 7 笔 −1,089.71U。
                #    ★ 只数「本轮 evaluate 产生的信号」，不和已有持仓合并 —— 与回测
                #      口径一致（scripts/exp_sameside_1111.py::cap_same_bar）。
                #    ★ 必须在 allowed 之后才计数：被风控否掉的信号不占名额。
                _msb = int((scfg.get("params") or {}).get("max_same_bar_side") or 0)
                _side_cnt = {"long": 0, "short": 0}
                _capped = []
                for sig in signals:
                    qok, qwhy = quality.is_tradable(sig.symbol)
                    if not qok:
                        db.log_signal(sig.strategy, sig.symbol, sig.side, sig.reason,
                                      False, f"杂讯过滤:{qwhy}")
                        continue
                    # ── 反手（2026-11-11 晚 浩哥定「反转后做反向单」）──────────
                    # 本时段方向与在管仓位相反时，evaluate 会把信号标 reverse=True。
                    # 这里必须先平掉旧仓：risk.allow_entry 有一条硬闸
                    # 「同币已有仓位(禁止对锁/加仓)」，不先平就永远开不出去。
                    # ⚠ 只在「确认根」之后才有信号，所以不会拿假穿越去反手。
                    if getattr(sig, "reverse", False):
                        _old = db.open_positions(strategy=sig.strategy, symbol=sig.symbol)
                        if _old:
                            _px0 = factors.mark_price_of(sig.symbol) or sig.stop_price
                            _rtag = f"反手：时段转{'多' if sig.side == 'long' else '空'}"
                            _rev_ok = await executor.close_position(_old[0], _px0, _rtag)
                            if not _rev_ok:
                                db.log_signal(sig.strategy, sig.symbol, sig.side,
                                              sig.reason, False, "反手：旧仓平仓失败")
                                continue
                            db.log("info", f"{_rtag} {sig.symbol} "
                                           f"{_old[0].get('side')} → {sig.side}")
                    allowed, why, mult = risk.allow_entry(sig, scfg, equity_cache, cfg_global)
                    # 影子模式：不管开没开仓，都记录三套因子规则的判定 + 虚拟跟踪
                    try:
                        _px = factors.mark_price_of(sig.symbol) or sig.stop_price
                        shadow.record(sig, _px, sig.stop_price, acted=allowed)
                    except Exception as _exc:  # noqa: BLE001
                        log.debug("shadow record failed: %s", _exc)
                    if not allowed:
                        db.log_signal(sig.strategy, sig.symbol, sig.side, sig.reason, False, why)
                        continue
                    if not _same_bar_allow(_side_cnt, sig.side, _msb):
                        # 名额已满：记一条信号日志，面板/信号流里能查到被谁挤掉
                        db.log_signal(sig.strategy, sig.symbol, sig.side, sig.reason,
                                      False, f"同一K线同向已满{_msb}笔")
                        _capped.append(sig.symbol)
                        continue
                    sig.size_mult *= mult
                    price = factors.mark_price_of(sig.symbol)
                    qty = risk.position_qty(sig, scfg, equity_cache, cfg_global, price)
                    leverage = int((scfg.get("risk") or {}).get("leverage")
                                   or cfg_global.get("max_gross_leverage", 3))
                    pid = await executor.open_position(sig, qty, price, leverage)
                    # ★ 名额只在「真开成了」之后才 +1。
                    #   放在开仓前会有两个错：① 资金不足/下单失败也占名额，把本该开的挤掉；
                    #   ② 与回测口径不一致（回测里每笔信号都算开成）。
                    if pid:
                        _side_cnt[sig.side] = _side_cnt.get(sig.side, 0) + 1
                    db.log_signal(sig.strategy, sig.symbol, sig.side, sig.reason,
                                  bool(pid), "" if pid else "执行失败/资金不足")
                if _msb > 0 and _capped:
                    log.info("同K线同向限笔 %d 笔：本轮开 %d（多%d/空%d），拦下 %s",
                             _msb, sum(_side_cnt.values()), _side_cnt.get("long", 0),
                             _side_cnt.get("short", 0), ",".join(s.replace("USDT", "") for s in _capped))
            try:
                shadow.settle(factors.mark_price_of)
            except Exception as _exc:  # noqa: BLE001
                log.debug("shadow settle failed: %s", _exc)
            _heartbeat("last_tick_ts", int(time.time()))
            # 只在权益可信时清错误。以前无条件清空，导致本 tick 前面刚写的
            # 「权益读取失败，暂停开仓」在同一轮就被抹掉，界面永远看不到原因。
            if _eq_ok:
                db.set_meta("last_error", "")
        except Exception as exc:  # noqa: BLE001
            log.exception("scan loop tick failed")
            db.set_meta("last_error", f"{int(time.time())}: {exc}")
        await asyncio.sleep(max(LOOP_SEC - (time.monotonic() - started), 0.05))
