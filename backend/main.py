"""FastAPI app: dashboard API + engine background task + static frontend."""
import asyncio
import faulthandler
import json
import logging
import signal
import time

if hasattr(signal, "SIGUSR1"):           # SIGUSR1 is Unix-only — Windows has no such signal
    faulthandler.register(signal.SIGUSR1)   # kill -USR1 <pid> dumps all stacks
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import binance, config, daily_target, db, engine, executor, factors, pool, quality, radar, risk, shadow, strategies, ws

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

# V2 已转正为正式版：静态根目录指向 frontend-v2/dist
DIST = Path(__file__).resolve().parent.parent / "frontend-v2" / "dist"


async def _prune_loop():
    """每天清理一次过期数据（factor_history 14 天 / logs 30 天）。"""
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            r = db.prune()
            if r.get("factor_history") or r.get("logs"):
                log.info("定时清理: %s", r)
                db.log("info", f"数据清理: 因子历史 -{r.get('factor_history', 0)} 行, "
                               f"日志 -{r.get('logs', 0)} 行")
        except Exception as exc:  # noqa: BLE001
            log.warning("数据清理失败: %s", exc)


def _mk_supervisor(state):
    """看门狗：循环异常退出或心跳停滞时自动重启。

    背景：scan_loop / position_loop / ws_loop 都是 `while True` + 内部
    try/except，单次异常能兜住，但有两种情况会让它们静默死亡或卡死：
      ① 抛出的异常不是 Exception 子类（如 asyncio.CancelledError 之外
         的 BaseException、MemoryError 等），任务直接结束；
      ② 卡在一个 await 上（DB 锁、网络挂起、同步推送阻塞事件循环）。
    两种情况进程都还活着、HTTP 还响应，用户以为系统在盯盘，实际
    止损/止盈已经停摆。这个看门狗每 30 秒检查一次任务状态和心跳
    meta，发现问题就 cancel + 重建任务。
    """
    async def _watch():
        await asyncio.sleep(60)          # 启动缓冲：ws/池子预热期间不判死
        while True:
            try:
                now = time.time()
                for name, s in state.items():
                    task = s["task"]
                    if task.done():
                        exc = None
                        if not task.cancelled():
                            try:
                                exc = task.exception()
                            except Exception:  # noqa: BLE001
                                exc = "unknown"
                        log.error("看门狗：%s 循环已退出(%s)，重启", name, exc)
                        db.log("error", f"看门狗：{name} 循环已退出，自动重启")
                        s["task"] = asyncio.create_task(s["fn"]())
                        continue
                    key = s.get("meta")
                    if not key:
                        continue
                    raw = db.get_meta(key)
                    if not raw:
                        continue
                    try:
                        v = int(float(raw))
                    except (TypeError, ValueError):
                        continue
                    ts = v / 1000.0 if key.endswith("_ms") else float(v)
                    age = now - ts
                    if age > s["stale_sec"]:
                        log.error("看门狗：%s 心跳停滞 %.0f 秒，重启", name, age)
                        db.log("error",
                               f"看门狗：{name} 心跳停滞 {int(age)} 秒，已重启")
                        db.set_meta("watchdog_last_restart",
                                    f"{name} @ {int(now)} (停滞 {int(age)}s)")
                        try:
                            task.cancel()
                        except Exception:  # noqa: BLE001
                            pass
                        s["task"] = asyncio.create_task(s["fn"]())
            except Exception as exc:  # noqa: BLE001
                log.warning("看门狗自身异常: %s", exc)
            await asyncio.sleep(30)
    return _watch()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    _prune_once = db.prune()   # 启动先清一次过期数据
    if _prune_once.get("factor_history") or _prune_once.get("logs"):
        log.info("启动清理: %s", _prune_once)
    # 第 1 层：候选池 —— 先从磁盘加载（每天凌晨定时重建）
    pool.load()
    _pool_cfg = config.pools().get("candidate", {})
    pool.CANDIDATE_SIZE = int(_pool_cfg.get("size", pool.CANDIDATE_SIZE))
    pool.REFRESH_HOUR = int(_pool_cfg.get("refresh_hour", pool.REFRESH_HOUR))
    pool.MIN_AMP_PCT = float(_pool_cfg.get("min_amp_pct", pool.MIN_AMP_PCT))
    pool.MAX_AMP_PCT = float(_pool_cfg.get("max_amp_pct", pool.MAX_AMP_PCT))
    pool.VOL_DAYS = int(_pool_cfg.get("vol_days", pool.VOL_DAYS - 1)) + 1
    # 让 config.UNIVERSE 立刻反映候选池（load_strategies 里也有同样逻辑）
    config.load_strategies()
    _wl = lambda: (config.pools().get("candidate", {}) or {}).get("whitelist") or []
    # 受监督的核心循环（看门狗按心跳判定是否卡死）
    supervised = {
        "ws":       {"fn": ws.ws_loop,       "task": None,
                     "meta": None,             "stale_sec": 120},
        "scan":     {"fn": engine.scan_loop, "task": None,
                     "meta": "last_tick_ts",   "stale_sec": 120},
        "position": {"fn": engine.position_loop, "task": None,
                     "meta": "last_manage_ts_ms", "stale_sec": 30},
    }
    for s in supervised.values():
        s["task"] = asyncio.create_task(s["fn"]())
    # 不受心跳监督的辅助循环
    tasks = [asyncio.create_task(pool.loop(_wl)),
             asyncio.create_task(_prune_loop()),
             asyncio.create_task(quality.loop(
                 lambda: config.UNIVERSE,
                 lambda: config.pools().get("monitor", {}))),
             # 持仓强弱雷达：只提示不干预，独立循环，不参与心跳监督
             asyncio.create_task(radar.loop()),
             _mk_supervisor(supervised)]
    try:
        yield
    finally:
        for s in supervised.values():
            s["task"].cancel()
        for t in tasks:
            t.cancel()
        await binance.aclose()      # 关掉复用的 HTTP 连接池


app = FastAPI(title="quant-bot", lifespan=lifespan)


# ── API 访问控制中间件 ───────────────────────────────────────────
# 修之前：全部 696 行路由零鉴权、零 Origin 校验、零限流。
# 虽然服务只绑 127.0.0.1，但 `POST /api/positions/close-all`、
# `POST /api/daily-target/reset` 这类无 body 的简单请求，任意网页用
# `fetch(url, {mode:'no-cors'})` 就能打出去 —— 打开一个恶意页面就能
# 一键清光持仓。加上 DNS rebinding（攻击者域名解析到 127.0.0.1，
# 浏览器判定同源）还能绕过 localhost 隔离。
#
# 三层防护，全部不需要前端改动：
#  ① Host 白名单 —— 挡 DNS rebinding
#  ② Sec-Fetch-Site / Origin —— 挡 CSRF（现代浏览器强制发送）
#  ③ 可选 token —— 上云后暴露公网时启用
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _api_guard(request: Request, call_next):
    allowed = config.API_ALLOWED_HOSTS

    # ① Host 白名单
    # IPv6 三种写法都要能正确取出主机名：
    #   [::1]:8899  → ::1      （带方括号+端口）
    #   [::1]       → ::1
    #   ::1         → ::1      （裸 IPv6，冒号多于一个）
    # 原实现用 rsplit(":",1) 会把 ::1 切成 ":"，导致本机 IPv6 访问整站 403。
    raw_host = (request.headers.get("host") or "").strip().lower()
    if raw_host.startswith("["):
        host = raw_host[1:].split("]", 1)[0]
    elif raw_host.count(":") > 1:
        host = raw_host
    else:
        host = raw_host.rsplit(":", 1)[0] if ":" in raw_host else raw_host
    if allowed and host and host not in allowed:
        log.warning("拒绝访问：Host=%s 不在白名单", host)
        return JSONResponse({"detail": f"host not allowed: {host}"}, status_code=403)

    # ② 写操作的跨站防护
    if request.method in _WRITE_METHODS:
        site = (request.headers.get("sec-fetch-site") or "").lower()
        origin = (request.headers.get("origin") or "").strip()
        if site == "cross-site":
            log.warning("拒绝跨站写请求: %s %s", request.method, request.url.path)
            return JSONResponse({"detail": "cross-site write rejected"}, status_code=403)
        if origin and site != "same-origin":
            ohost = ""
            try:
                from urllib.parse import urlparse
                ohost = (urlparse(origin).hostname or "").lower()
            except Exception:  # noqa: BLE001
                ohost = ""
            if allowed and ohost and ohost not in allowed:
                log.warning("拒绝跨站写请求: origin=%s", origin)
                return JSONResponse({"detail": "cross-site origin rejected"}, status_code=403)

        # ③ token（配置了才校验）
        if config.API_TOKEN:
            tok = (request.headers.get("x-api-token") or "").strip()
            if not tok:
                auth = (request.headers.get("authorization") or "").strip()
                if auth.lower().startswith("bearer "):
                    tok = auth[7:].strip()
            if tok != config.API_TOKEN:
                return JSONResponse({"detail": "invalid api token"}, status_code=401)

    return await call_next(request)


@app.get("/api/status")
async def api_status(mode: str = ""):
    """mode: 留空 = 当前实际运行模式；传 paper/live = 看指定模式的口径。

    2026-11-07 浩哥发现：切到「实盘 LIVE」看曲线时，权益卡浮框里的
    「今日已实现 +182.87」还挂着纸面的数。因为这里不接 mode，
    永远返回当前运行模式（paper）的数字。前端 EquityChart 现在会
    按自己当前选中的模式来问这个接口。
    """
    m = mode if mode in ("paper", "live") else None
    eq, bal, upnl = await executor.equity(m)
    cfg = config.load_strategies()
    day_pnl = risk.daily_realized_pnl(m or "auto")
    return {
        "mode": m or executor.mode(),
        "equity": eq, "balance": bal, "unrealized": upnl,
        "daily_realized_pnl": day_pnl,
        "halted": risk.halted(),
        "event_quiet": risk.event_quiet(cfg["global"]),
        "open_positions": len(db.open_positions(mode=m or "auto")),
        "universe": config.UNIVERSE,
        # 2026-11-06 自检修复：前端把 universe（候选池 200）当"监控标的"显示，
        # 实际监控池是 quality 维护的 ≤23 名单。这里单独给一个字段。
        "monitor_n": len(quality.monitor_list() or []),
        # 扫描节拍从配置读，别让前端写死 —— 2026-11-06 就是写死 15m
        # 而配置实际是 2m，ui 和真实行为对不上。
        "scan_sec": engine._interval_sec(
            (cfg.get("strategies", {}).get("S15_CE_ZLSMA", {}) or {}).get("scan_interval", "15m")),
        "last_tick_ts": int(db.get_meta("last_tick_ts", "0") or 0),
        "last_manage_ms": int(db.get_meta("last_manage_ts_ms", "0") or 0),
        "ws_symbols": len(ws.PRICES),
        "started_at": int(db.get_meta("started_at", "0") or 0),
        "last_error": db.get_meta("last_error", ""),
        "now": int(time.time()),
        "now_ms": int(time.time() * 1000),
        "daily_target": daily_target.summary(
            eq, db.day_start_equity(), daily_target.cfg(cfg["global"])),
    }


@app.get("/api/events")
async def api_events():
    """经济日历：未来 48 小时事件 + 当前静默状态。

    浩哥 2026-11-06 要求「事件要在界面上看得见」。
    此前只有 /api/status.event_quiet 一个字符串，且**只在静默时才非空** ——
    平时页面上完全看不到这个机制存在，也不知道下一个事件什么时候来。

    同一时刻的多个事件合并成一行：非农一次会返回 6 条（非农就业/失业率/
    时薪/就业人口…），分开列会把表格灌满，实际它们是同一个时刻同一个闸门。
    """
    cfg = config.load_strategies()
    g = cfg.get("global") or {}
    window_min = int(g.get("event_quiet_minutes", 30) or 0)
    now = time.time()

    raw = (db.get_factor("econ_cal") or {}).get("events") or []
    horizon = now + 48 * 3600
    # 略早于 now 的也留着（正在静默窗口里的事件需要显示「已进入静默」）
    fut = [e for e in raw if now - window_min * 60 < float(e.get("ts") or 0) <= horizon]
    fut.sort(key=lambda e: float(e["ts"]))

    groups: dict[int, list] = {}
    for e in fut:
        groups.setdefault(int(e["ts"]), []).append(e)

    rows = []
    for ts in sorted(groups):
        items = groups[ts]
        top = max(items, key=lambda x: int(x.get("importance") or 0))
        dt = ts - now
        rows.append({
            "ts": ts,
            "title": top.get("title", ""),
            "importance": int(top.get("importance") or 0),
            "count": len(items),
            "minutes": round(dt / 60),
            # 必须和 risk.event_quiet() 用同一个判据：0 < ts-now <= window
            # 也就是「只在事件之前静默」。写成前后对称会让界面显示「静默中」
            # 而引擎其实照常开仓 —— 界面和真实行为对不上比不显示更糟。
            "in_window": bool(0 < dt <= window_min * 60),
        })

    nxt = next((r for r in rows if r["minutes"] > 0), None)
    return {
        "quiet": risk.event_quiet(g),          # 非空 = 正在静默，值是事件名
        "window_min": window_min,
        "now": int(now),
        "next_ts": nxt["ts"] if nxt else 0,
        "next_title": nxt["title"] if nxt else "",
        "next_minutes": nxt["minutes"] if nxt else None,
        "rows": rows[:40],
        "total": len(rows),
    }


@app.get("/api/radar")
async def api_radar(symbol: str = ""):
    """持仓强弱雷达：每币最新一条分数 + 最近 3 小时走势。

    只读，不影响交易。空仓时返回空列表。
    """
    try:
        if symbol:
            rows = radar.latest(symbol)
            return {"rows": rows, "history": radar.history(symbol, 36)}
        rows = radar.latest()
        out = []
        for r in rows:
            r = dict(r)
            try:
                det = json.loads(r.get("detail") or "{}")
            except Exception:  # noqa: BLE001
                det = {}
            r["reason"] = radar.reason_of(det)
            r["history"] = radar.history(r["symbol"], 36)
            out.append(r)
        return {"rows": out}
    except Exception as exc:  # noqa: BLE001
        return {"rows": [], "error": str(exc)}


@app.get("/api/radar/alerts")
async def api_radar_alerts(limit: int = 50, symbol: str = ""):
    """雷达警报记录：什么时候叫过、什么建议、后来准不准。

    eval_1h_r / eval_4h_r = 提醒后朝【持仓有利方向】的涨跌幅(%)。
      负数 -> 之后继续不利 -> 提醒是对的
      正数 -> 之后反弹了   -> 提醒早了
    """
    try:
        return {"rows": radar.alerts(limit=limit, symbol=symbol or None)}
    except Exception as exc:  # noqa: BLE001
        return {"rows": [], "error": str(exc)}


@app.get("/api/positions")
async def api_positions():
    cfg = config.load_strategies()
    glev = int((cfg.get("global") or {}).get("max_gross_leverage", 3) or 3)
    mmr = 0.005          # 维持保证金率近似值（币安 USDT 永续小额档 ~0.4-0.5%）
    out = []
    for p in db.open_positions():
        price = factors.mark_price_of(p["symbol"]) or p["entry_price"]
        sign = 1 if p["side"] == "long" else -1
        upnl = (price - p["entry_price"]) * p["qty"] * sign
        scfg = (cfg.get("strategies") or {}).get(p["strategy"], {}) or {}
        # 策略参数都挂在 params: 下面；直接 scfg.get() 取不到，会一直吃默认值
        sparams = scfg.get("params") or {}
        # 优先用该仓位自己的杠杆（可在持仓表手动改），否则回退到策略/全局配置
        lev = int(p.get("leverage") or ((scfg.get("risk") or {}).get("leverage")) or glev)
        lev = max(1, lev)
        extra = float(p.get("extra_margin") or 0.0)
        qty_abs = abs(p["qty"]) or 1e-12
        # 爆仓价（含维持保证金率 + 追加的保证金会推远爆仓价）
        entry = p["entry_price"]
        if p["side"] == "long":
            liq = entry * (1 - 1.0 / lev + mmr) - extra / qty_abs
            liq_dist = (price - liq) / price * 100 if price else 0.0
        else:
            liq = entry * (1 + 1.0 / lev - mmr) + extra / qty_abs
            liq_dist = (liq - price) / price * 100 if price else 0.0
        stop = p.get("stop_price") or 0.0
        stop_dist = (abs(price - stop) / price * 100) if (price and stop) else 0.0

        # ── S15 出场状态：连续破均线计数 / 峰顶 R ──
        # 这两样原来是 check_exit 的私有内存态（_S15_POS_STATE），卡片看不到。
        # 现在透出来，前端才能画「破均线 N/2」和「从峰顶回撤 X/0.80R」。
        # 注意：cnt / peak 都是内存态，服务重启后归零重数（不影响出场判断本身）。
        st = strategies._S15_POS_STATE.get(
            (p["symbol"], p["side"], p.get("opened_at"))) or {}
        ma_confirm = max(1, int((sparams.get("ma_confirm_bars") or 2)))
        ma_break_cnt = int(st.get("cnt") or 0)
        # 峰顶优先用内存态；进程刚重启时内存还没重建，退回库里的 peak_r
        peak_r_mem = st.get("peak")
        if peak_r_mem is None:
            try:
                peak_r_mem = float(p.get("peak_r") or 0.0)
            except (TypeError, ValueError):
                peak_r_mem = None
        risk_abs = abs(p["entry_price"] - stop) if (p["entry_price"] and stop) else 0.0
        drawdown_r = (float(peak_r_mem) - ((price - p["entry_price"]) *
                      sign / risk_abs)) if (peak_r_mem is not None and risk_abs > 0) else None

        # ── K5 分段回撤：当前这一刻实际要求「破几根线 + 回撤多少 R」──
        # 峰顶不足 trail_pct_max_r 时走比例（留住一半），满 2R 后走固定 trail_r。
        # 前端按这两个值显示目标，不然卡片上永远写「0.80R」会误导。
        _pct = float(sparams.get("trail_pct") or 0.0)
        _pct_max = float(sparams.get("trail_pct_max_r") or 2.0)
        _pct_cap = float(sparams.get("trail_pct_cap") or 0.0)
        _trail_r = float(sparams.get("trail_r") or 0.8)
        _bars_lt = int(sparams.get("ma_bars_lt") or ma_confirm)
        # peak<=0.05 时代码会算出 0 门槛 → 前端显示「回撤目标 0.00」，
        # 实际出场判定也走固定 trail_r（见 strategies.py 同样的保护）
        # 根数只看「峰顶是否满 2R」，与门槛分开判（否则新仓会显示成 0/2）
        _pk = float(peak_r_mem or 0.0)
        use_pct = bool(_pct > 0 and peak_r_mem is not None
                       and 0.05 < _pk < _pct_max)
        ma_bars_need = _bars_lt if _pk < _pct_max else ma_confirm
        if use_pct:
            trail_target_r = _pk * _pct
            if _pct_cap > 0:
                trail_target_r = min(trail_target_r, _pct_cap)
        else:
            trail_target_r = _trail_r

        out.append({**p, "mark_price": price, "unrealized_pnl": upnl,
                    "notional": abs(p["qty"] * price),
                    "leverage": lev, "liq_price": liq, "liq_dist_pct": liq_dist,
                    "stop_dist_pct": stop_dist,
                    # 出场条件可视化用（内存态，None 表示本轮尚未算过）
                    "ma_break_cnt": ma_break_cnt,
                    "ma_confirm_bars": ma_bars_need,      # 当前要求的穿线根数（2 或 5）
                    "ma_confirm_bars_base": ma_confirm,   # 满 2R 之后回归的基准值
                    "trail_target_r": trail_target_r,     # 当前要求的回撤 R（动态）
                    "trail_is_pct": use_pct,              # True = 按浮盈百分比算的
                    "peak_r_mem": float(peak_r_mem) if peak_r_mem is not None else None,
                    "drawdown_r": drawdown_r,
                    # 占用保证金按【当前价】计算，与币安口径一致（仓位价值随价格浮动）
                    "margin": abs(p["qty"] * price) / lev + extra,
                    "margin_at_entry": abs(p["qty"] * entry) / lev + extra,
                    "extra_margin": extra,
                    "age_sec": int(time.time()) - p["opened_at"]})
    return {"positions": out}


@app.post("/api/positions/{pid}/close")
async def api_close_position(pid: int, body: dict = Body(default=None)):
    pos = db.get_position(pid)
    if not pos or pos["status"] != "open":
        raise HTTPException(404, "position not open")
    price = factors.mark_price_of(pos["symbol"])
    if not price:
        raise HTTPException(503, "no live price available")
    fraction = float((body or {}).get("fraction", 1.0) or 1.0)
    fraction = max(0.01, min(1.0, fraction))
    label = "手动全平" if fraction >= 0.999 else f"手动平仓 {fraction*100:.0f}%"
    ok = await executor.close_position(pos, price, label, fraction)
    return {"ok": ok, "fraction": fraction, "symbol": pos["symbol"]}


@app.post("/api/daily-target/reset")
async def api_dt_reset(payload: dict | None = None):
    """手动重置日目标。

    skip_today（默认 true）：
      true  = 今日重置 —— 解除封锁 + 当天不再自动触发（平完想继续做）
      false = 只清状态，达标后会重新触发
    """
    skip = True
    if isinstance(payload, dict) and "skip_today" in payload:
        skip = bool(payload["skip_today"])
    daily_target.reset(skip_today=skip)
    return {"ok": True, "skipped_today": daily_target.skipped_today()}


@app.post("/api/positions/close-all")
async def api_close_all():
    closed, failed = await executor.close_all("手动一键全平")
    if closed:
        daily_target.mark_done("手动一键全平")
    return {"ok": failed == 0, "closed": closed, "failed": failed}


@app.post("/api/positions/{pid}/leverage")
async def api_set_position_leverage(pid: int, payload: dict):
    """手动调整单个仓位的杠杆：只改保证金占用与爆仓价，不改仓位数量。"""
    pos = db.get_position(pid)
    if not pos or pos["status"] != "open":
        raise HTTPException(404, "position not open")
    try:
        lev = int(payload.get("leverage") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "leverage must be an integer")
    if lev < 1 or lev > 125:
        raise HTTPException(400, "leverage must be 1-125")
    old = pos.get("leverage")
    db.set_position_field(pid, "leverage", float(lev))
    db.log("info", f"手动调整杠杆 {pos['symbol']} {old or '-'}x → {lev}x")
    return {"ok": True, "leverage": lev, "previous": old}


@app.post("/api/positions/{pid}/margin")
async def api_add_position_margin(pid: int, payload: dict):
    """追加保证金：从可用保证金划入该仓位，推远爆仓价。"""
    pos = db.get_position(pid)
    if not pos or pos["status"] != "open":
        raise HTTPException(404, "position not open")
    try:
        amt = float(payload.get("amount") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "amount must be a number")
    if amt <= 0:
        raise HTTPException(400, "amount must be positive")
    # 校验可用保证金
    eq, _bal, _upnl = await executor.equity()
    used = 0.0
    for q in db.open_positions():
        px = factors.mark_price_of(q["symbol"]) or q["entry_price"]
        scfg = (config.load_strategies().get("strategies") or {}).get(q["strategy"], {}) or {}
        gl = int((config.load_strategies().get("global") or {}).get("max_gross_leverage", 3) or 3)
        lv = max(1, int(q.get("leverage") or ((scfg.get("risk") or {}).get("leverage")) or gl))
        used += abs(q["qty"] * px) / lv + float(q.get("extra_margin") or 0)
    free = eq - used
    if amt > free + 1e-9:
        raise HTTPException(400, f"可用保证金不足（可用 {free:.2f} U）")
    new_extra = db.add_extra_margin(pid, amt)
    db.log("info", f"追加保证金 {pos['symbol']} +{amt:.2f}U（累计 {new_extra:.2f}U）")
    return {"ok": True, "extra_margin": new_extra, "free_margin": free - amt}


@app.get("/api/shadow")
async def api_shadow():
    """影子模式：规则统计 + 最近记录。"""
    return {"stats": db.shadow_stats(),
            "recent": db.shadow_recent(60),
            "thresholds": {"funding_hot_long": shadow.FUNDING_HOT_LONG,
                           "funding_hot_short": shadow.FUNDING_HOT_SHORT,
                           "taker_confirm": shadow.TAKER_CONFIRM}}


@app.get("/api/trades")
async def api_trades(limit: int = 100, mode: str = "auto"):
    m = None if mode in ("all", "") else mode
    return {"trades": db.recent_trades(limit, mode=m), "mode": executor.mode()}


@app.get("/api/signals")
async def api_signals(limit: int = 50):
    return {"signals": db.recent_signals(limit)}


@app.get("/api/gates")
async def api_gates():
    """三道光闸诊断：监控池每个币最新一根 15m 的三道闸状态。

    数据来自策略扫描时留在内存里的中间结果（strategies._GATE_STATE），
    不额外拉 K 线，零流量成本。回答的问题是：今天没开仓，是
    「真没机会」还是「门槛卡死了」。
    """
    gs = strategies._GATE_STATE
    mon = set(quality.monitor_list() or [])
    rows = []
    bar = None
    for sym in config.UNIVERSE:
        v = gs.get(sym)
        if not v:
            continue
        if v.get("ts") and (bar is None or v["ts"] > bar):
            bar = v["ts"]
        rows.append({"symbol": sym, "in_monitor": sym in mon, **v})
    # 有戏的排前面：已开仓 > CE 已翻 > 其它
    rows.sort(key=lambda r: (not r.get("ok"), not r.get("ce"),
                             not r.get("ha"),
                             -(r.get("bbw") or 0)))
    return {
        "ts": bar,
        "rows": rows,
        "total": len(rows),
        "passed": sum(1 for r in rows if r.get("ok")),
        "ce_n": sum(1 for r in rows if r.get("ce")),
        "ha_n": sum(1 for r in rows if r.get("ha")),
        "bbw_n": sum(1 for r in rows
                     if r.get("bbw") is not None
                     and r["bbw"] >= (r.get("bbw_thr") or 70)),
        "bbw_thr": next((r.get("bbw_thr") for r in rows if r.get("bbw_thr")), 70),
    }


@app.get("/api/equity")
async def api_equity(limit: int = 2880, mode: str = "auto"):
    m = None if mode in ("all", "") else mode
    shown = executor.mode() if mode == "auto" else (m or "all")
    return {"series": db.equity_series(limit, mode=m), "mode": shown,
            "active_mode": executor.mode()}


@app.get("/api/factors")
async def api_factors():
    return {"factors": db.all_factors_snapshot(), "now": int(time.time())}


@app.get("/api/factor-history")
async def api_factor_history(factor: str, symbol: str = "", limit: int = 200):
    return {"history": db.factor_history(factor, symbol, limit)}


@app.get("/api/factor-config")
async def api_factor_config():
    return {"factors": factors.config_snapshot()}


@app.post("/api/factor-config/{name}")
async def api_factor_config_set(name: str, payload: dict):
    try:
        factors.set_config(name,
                           interval_sec=payload.get("interval_sec"),
                           enabled=payload.get("enabled"))
    except KeyError:
        raise HTTPException(404, f"unknown factor {name}")
    db.log("info", f"因子 {name} 配置更新: {payload}")
    return {"ok": True}


@app.get("/api/strategies")
async def api_strategies():
    from .strategies import REGISTRY, strategy_meta
    cfg = config.load_strategies()
    meta = strategy_meta()
    factor_labels = {r[0]: r[4] for r in factors.REGISTRY}

    def deps_of(base):
        cls = REGISTRY.get(base)
        return [{"name": f, "label": factor_labels.get(f, f),
                 "enabled": factors.is_enabled(f)}
                for f in getattr(cls, "FACTORS", [])] if cls else []

    trades = db.recent_trades(500, mode="auto")   # stats follow the active mode
    stats = {}
    for t in trades:
        if t["kind"] != "close":
            continue
        s = stats.setdefault(t["strategy"], {"closed": 0, "wins": 0, "pnl": 0.0})
        s["closed"] += 1
        s["pnl"] += t["pnl"] or 0
        if (t["pnl"] or 0) > 0:
            s["wins"] += 1
    return {"global": cfg["global"],
            "templates": [{"id": tid, "meta": m} for tid, m in meta.items()],
            "strategies": [{"id": iid, **scfg,
                            "base": scfg.get("base") or iid,
                            "stats": stats.get(iid),
                            "factors": deps_of(scfg.get("base") or iid),
                            "meta": meta.get(scfg.get("base") or iid, {})}
                           for iid, scfg in cfg.get("strategies", {}).items()]}


@app.post("/api/strategies/{sid}/config")
async def api_strategy_config(sid: str, payload: dict):
    try:
        if "scan_interval_sec" in payload:
            config.set_strategy_interval(sid, payload["scan_interval_sec"])
        if "symbols" in payload:
            config.set_strategy_symbols(sid, payload["symbols"])
        if "risk" in payload:
            config.set_strategy_risk(sid, payload["risk"])
        if "params" in payload:
            config.set_strategy_params(sid, payload["params"])
        if "display_name" in payload:
            config.set_strategy_display_name(sid, payload["display_name"])
    except KeyError:
        raise HTTPException(404, f"unknown strategy {sid}")
    except (TypeError, ValueError):
        raise HTTPException(400, "invalid config value")
    db.log("info", f"策略 {sid} 配置更新")
    return {"ok": True}


@app.post("/api/strategies/create")
async def api_strategy_create(payload: dict):
    base = str(payload.get("base") or "")
    try:
        iid = config.create_strategy_instance(base, payload.get("name") or "")
    except KeyError:
        raise HTTPException(404, f"unknown template {base}")
    db.log("info", f"新建策略实例 {iid}(模板 {base})")
    return {"ok": True, "id": iid}


@app.delete("/api/strategies/{sid}")
async def api_strategy_delete(sid: str):
    if db.open_positions(strategy=sid, mode=None):   # any mode blocks deletion
        raise HTTPException(409, "该策略实例仍有未平仓位,请先平仓")
    try:
        config.delete_strategy_instance(sid)
    except KeyError:
        raise HTTPException(404, f"unknown strategy {sid}")
    db.log("warn", f"策略实例 {sid} 已删除")
    return {"ok": True}


# -- risk / universe / credentials -------------------------------------------

@app.get("/api/risk")
async def api_risk_get():
    cfg = config.load_strategies()
    eq, _, _ = await executor.equity()
    strat = []
    for sid, scfg in cfg.get("strategies", {}).items():
        risk = scfg.get("risk", {})
        used = sum(abs(p["qty"] * factors.mark_price_of(p["symbol"]))
                   for p in db.open_positions(strategy=sid))
        strat.append({"id": sid, "meta_name": (scfg.get("params") and ""),
                      "enabled": scfg.get("enabled"), "risk": risk, "used_notional": used})
    return {"global": cfg["global"], "equity": eq, "strategies": strat}


@app.post("/api/risk/global")
async def api_risk_global(payload: dict):
    try:
        _cfg, changed = config.set_global_risk(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.log("info", "全局风控参数更新: " + ", ".join(f"{k}={v}" for k, v in changed.items()))
    return {"ok": True, "changed": changed}


@app.get("/api/universe")
async def api_universe_get():
    return {"universe": config.UNIVERSE, "known": config.KNOWN_SYMBOLS}


@app.get("/api/pool")
async def api_pool():
    """监控池全景（三层）：候选池 100 → 监控池 30 → 持仓。"""
    snap = quality.snapshot()
    data = snap["data"]
    monitor = set(snap.get("monitor") or [])
    held = {p["symbol"]: p for p in db.open_positions()}
    last_sig = {}
    for r in db.recent_signals(limit=400):
        last_sig.setdefault(r["symbol"], r)
    rows = []
    scan = list(config.UNIVERSE)
    for s in held:                      # 持仓币必须在列表里，哪怕已掉出候选池
        if s not in scan:
            scan.append(s)
    for sym in scan:
        q = data.get(sym) or {}
        p = held.get(sym)
        sig = last_sig.get(sym)
        rows.append({
            "symbol": sym,
            "price": factors.mark_price_of(sym),
            "qv": q.get("qv"), "atr_pct": q.get("atr_pct"), "spike": q.get("spike"),
            "ok": q.get("ok"), "why": q.get("why", ""),
            "in_monitor": sym in monitor,
            "in_candidate": sym in config.UNIVERSE,
            "held": bool(p),
            "side": p["side"] if p else None,
            "upnl": p.get("unrealized_pnl") if p else None,
            "last_sig_side": sig["side"] if sig else None,
            "last_sig_ts": sig["ts"] if sig else None,
            "last_sig_acted": bool(sig["acted"]) if sig else None,
            "last_sig_skip": sig.get("skip_reason") if sig else None,
            # acted 只代表「当时开了仓」，仓位现在还在不在看 pos_status
            "last_sig_pos": sig.get("pos_status") if sig else None,
            "last_sig_pnl": sig.get("pos_pnl") if sig else None,
        })
    rows.sort(key=lambda r: -(r["qv"] or 0))
    pool_snap = pool.snapshot()
    return {"ts": snap["ts"], "min_quote_vol": snap["min_quote_vol"],
            "max_spike": snap["max_spike"],
            "atr_enter_min": snap.get("atr_enter_min"),
            "atr_exit_max": snap.get("atr_exit_max"),
            "min_dwell_sec": snap.get("min_dwell_sec"),
            "atr_interval": snap.get("atr_interval"),
            "scan_sec": snap.get("scan_sec"),
            "refresh_sec": snap.get("refresh_sec"), "rows": rows,
            "pass_n": sum(1 for r in rows if r["ok"]),
            # 三层池规模
            "candidate_n": len(config.UNIVERSE),
            "candidate_size": pool_snap.get("size"),
            "candidate_meta": pool_snap.get("meta") or {},
            "monitor_n": snap.get("monitor_n"),
            "monitor_size": snap.get("monitor_size"),
            "monitor": list(monitor),
            "position_n": len(held),
            "position_max": (config.load_strategies().get("global", {})
                             .get("max_concurrent_coins")
                             or (config.pools().get("position", {}) or {}).get("max_concurrent", 8))}


@app.get("/api/pool/candidates")
async def api_candidates():
    """候选池详情：每天筛出的 100 只 + 构建元数据。"""
    snap = pool.snapshot()
    meta = snap.get("meta") or {}
    amps = meta.get("amps") or {}
    return {
        "symbols": snap["symbols"],
        "size": snap["size"],
        "built_ts": snap["built_ts"],
        "built_at": meta.get("built_at"),
        "elapsed_sec": meta.get("elapsed_sec"),
        "total_tradable": meta.get("total_tradable"),
        "scanned": meta.get("scanned"),
        "whitelist": meta.get("whitelist_used") or [],
        "whitelist_missing": meta.get("whitelist_missing") or [],
        "rejected_n": meta.get("rejected_n"),
        "rejected_sample": meta.get("rejected_sample") or [],
        "min_amp_pct": meta.get("min_amp_pct"),
        "max_amp_pct": meta.get("max_amp_pct"),
        "refresh_hour": snap.get("refresh_hour"),
        "amps": amps,
    }


@app.post("/api/pool/candidates/rebuild")
async def api_candidates_rebuild():
    """手动触发候选池重建（每天凌晨也会自动跑一次）。"""
    wl = (config.pools().get("candidate", {}) or {}).get("whitelist") or []
    st = await pool.build(wl)
    config.load_strategies()
    db.log("info", f"候选池手动重建：{len(st['symbols'])} 只")
    return {"ok": True, "size": len(st["symbols"]), "meta": st.get("meta") or {}}


@app.post("/api/pool/whitelist")
async def api_whitelist_set(payload: dict):
    """设置自选合约白名单（优先级最高，无条件进候选池）。"""
    wl = [str(s).strip().upper() for s in (payload.get("symbols") or []) if str(s).strip()]
    cfg = config.load_strategies()
    cfg.setdefault("pools", {}).setdefault("candidate", {})["whitelist"] = wl
    config.save_strategies(cfg)
    db.log("info", f"白名单更新为：{','.join(wl) or '(空)'}")
    return {"ok": True, "whitelist": wl}


@app.post("/api/universe")
async def api_universe_set(payload: dict):
    try:
        syms = config.set_universe(payload.get("symbols") or [])
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.log("info", f"监控币种更新为 {','.join(syms)}")
    return {"ok": True, "universe": syms}


@app.get("/api/credentials")
async def api_credentials_get():
    return config.credentials_masked()


@app.post("/api/credentials")
async def api_credentials_set(payload: dict):
    masked = config.save_credentials(payload)
    db.log("warn", "API 凭据已更新(热生效)")
    return {"ok": True, **masked}


@app.post("/api/credentials/test")
async def api_credentials_test(payload: dict):
    """Live connectivity check for the Config page."""
    which = payload.get("which", "both")
    result = {}
    if which in ("clawby", "both"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{config.CLAWBY_BASE}/api/account",
                                headers={"X-API-Key": config.CLAWBY_API_KEY})
                d = r.json()
            result["clawby"] = {"ok": r.status_code == 200,
                                "plan": d.get("plan"), "balance": d.get("payg_balance")}
        except Exception as e:  # noqa: BLE001
            result["clawby"] = {"ok": False, "error": str(e)[:120]}
    if which in ("binance", "both"):
        try:
            acc = await binance.account()
            result["binance"] = {"ok": True, "can_trade": acc.get("canTrade"),
                                 "wallet_usdt": acc.get("totalWalletBalance"),
                                 "available": acc.get("availableBalance")}
        except Exception as e:  # noqa: BLE001
            result["binance"] = {"ok": False, "error": str(e)[:120]}
    from . import exchanges
    for venue, tester in (("bitget", exchanges.bitget_test),
                          ("okx", exchanges.okx_test)):
        if which in (venue, "both") and exchanges.has_credentials(venue):
            try:
                result[venue] = await tester()
            except Exception as e:  # noqa: BLE001
                result[venue] = {"ok": False, "error": str(e)[:120]}
    return result


@app.post("/api/strategies/{sid}/toggle")
async def api_toggle(sid: str, payload: dict):
    try:
        config.set_strategy_enabled(sid, bool(payload.get("enabled")))
    except KeyError:
        raise HTTPException(404, f"unknown strategy {sid}")
    db.log("info", f"策略 {sid} {'启用' if payload.get('enabled') else '停用'}")
    return {"ok": True}


@app.get("/api/executor-exchange")
async def api_executor_exchange_get():
    from . import exchanges
    cred = config.credentials_masked()
    return {"exchange": executor.exchange(),
            "supported": [{"id": v, "has_credentials":
                           exchanges.has_credentials(v)} for v in exchanges.SUPPORTED],
            "has_bitget": cred["has_bitget"], "has_okx": cred["has_okx"]}


@app.post("/api/executor-exchange")
async def api_executor_exchange_set(payload: dict):
    venue = str(payload.get("exchange") or "")
    try:
        executor.set_exchange(venue)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.log("warn", f"实盘执行交易所切换为 {venue}")
    return {"ok": True, "exchange": venue}


# ── 实盘前置体检 ────────────────────────────────────────────────
# 浩哥 2026-11-07 定：切「实盘 LIVE」那一刻系统自己查，不对就拦住并说明原因。
# required=True 的项全过才算 ok；持仓模式只记录不拦截（下单代码自动适配单向/双向）。
async def _live_precheck():
    items = []

    def add(key, ok, label, detail, fix="", required=True):
        items.append({"key": key, "ok": bool(ok), "label": label,
                      "detail": detail, "fix": fix, "required": required})

    has_key = (bool(getattr(config, "BINANCE_API_KEY", ""))
               and bool(getattr(config, "BINANCE_SECRET_KEY", "")))
    add("apikey", has_key, "币安 API key",
        "已配置" if has_key else "还没配，实盘下不了单",
        "" if has_key else "写进 E:\\Hermes\\.env：BINANCE_API_KEY / BINANCE_SECRET_KEY"
                           "（只勾「读取 + 合约交易」，绝不勾提币，加 IP 白名单）")

    if not has_key:
        # 没 key 后面全查不了。不拿「查不到」冒充「没问题」。
        return {"ok": False, "items": items}

    try:
        multi = await binance.multi_assets_mode()
        ok = multi is False
        add("multi", ok, "联保模式",
            "已关闭（单币保证金模式），逐仓可以设" if ok
            else "还开着 —— 联保模式下币安不允许设逐仓",
            "" if ok else "App：合约 → U 本位合约 → 点右上【单】/【联】→ 选「单币保证金模式」"
                          "（不用平仓；关掉后 BTC/ETH 不能再当保证金）")
    except Exception as exc:  # noqa: BLE001
        add("multi", False, "联保模式", f"查不了：{exc}",
            "确认 API key 有读取权限、IP 白名单已加")

    try:
        hedge = await binance.position_mode()
        add("hedge", True, "持仓模式",
            "双向 —— 下单会自动带 positionSide" if hedge
            else "单向 —— 下单不带 positionSide", "", required=False)
    except Exception:  # noqa: BLE001
        add("hedge", True, "持仓模式", "查不到，按开仓时实测", "", required=False)

    return {"ok": all(i["ok"] for i in items if i["required"]), "items": items}


@app.get("/api/live/precheck")
async def api_live_precheck():
    """切实盘前的自查。required 项全过才返回 ok=true。"""
    return await _live_precheck()


@app.post("/api/mode")
async def api_mode(payload: dict):
    m = payload.get("mode")
    if m not in ("paper", "live"):
        raise HTTPException(400, "mode must be paper|live")
    if m == "live":
        if payload.get("confirm") != "LIVE":
            raise HTTPException(400, "switching to live requires confirm=LIVE")
        # 硬闸门：账户状态不对不许进实盘。绕过界面直接调这个接口也一样被拦。
        pre = await _live_precheck()
        if not pre["ok"]:
            bad = [{"label": i["label"], "detail": i["detail"], "fix": i["fix"]}
                   for i in pre["items"] if i["required"] and not i["ok"]]
            raise HTTPException(400, {"error": "实盘前置检查未通过", "problems": bad})
    executor.set_mode(m)
    db.log("warn", f"交易模式切换为 {m}")
    return {"ok": True, "mode": m}


@app.get("/api/klines")
async def api_klines(symbol: str = "BTCUSDT", interval: str = "15m", limit: int = 200):
    ks = await binance.klines(symbol, interval, limit)
    marks = []
    for t in db.recent_trades(300):
        if t["symbol"] == symbol:
            marks.append({"ts": t["ts"], "price": t["price"], "kind": t["kind"],
                          "side": t["side"], "strategy": t["strategy"]})
    return {"klines": ks, "marks": marks}


@app.get("/api/logs")
async def api_logs(limit: int = 100):
    return {"logs": db.recent_logs(limit)}


# -- trade journal (JSONL files under journal/) -------------------------------

def _journal_rows(strategy="", symbol="", day="", limit=0, mode=""):
    import json as _json

    from .journal import JOURNAL_DIR
    rows = []
    if not JOURNAL_DIR.exists():
        return rows
    files = sorted(JOURNAL_DIR.glob("trades-*.jsonl"), reverse=True)
    if day:
        files = [f for f in files if f.stem == f"trades-{day}"]
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                r = _json.loads(line)
            except ValueError:
                continue
            if strategy and r.get("strategy") != strategy:
                continue
            if symbol and r.get("symbol") != symbol.upper():
                continue
            if mode and r.get("mode") != mode:
                continue
            rows.append(r)
            if limit and len(rows) >= limit:
                return rows
    return rows


@app.get("/api/journal")
async def api_journal(limit: int = 200, strategy: str = "", symbol: str = "",
                      day: str = "", mode: str = ""):
    from .journal import JOURNAL_DIR
    days = sorted((f.stem.replace("trades-", "")
                   for f in JOURNAL_DIR.glob("trades-*.jsonl")), reverse=True) \
        if JOURNAL_DIR.exists() else []
    return {"rows": _journal_rows(strategy, symbol, day, limit, mode), "days": days}


@app.get("/api/journal/export")
async def api_journal_export(strategy: str = "", symbol: str = "", day: str = "",
                             mode: str = ""):
    import csv
    import io
    import json as _json

    from fastapi.responses import StreamingResponse
    rows = _journal_rows(strategy, symbol, day, mode=mode)
    cols = ["closed_at", "opened_at", "strategy", "symbol", "side", "qty",
            "entry_price", "close_price", "pnl", "pnl_pct", "fee",
            "hold_minutes", "signal_reason", "close_reason", "mfe_pct",
            "mae_pct", "mode", "entry_factors", "exit_factors"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        r = dict(r)
        for k in ("entry_factors", "exit_factors"):
            if isinstance(r.get(k), dict):
                r[k] = _json.dumps(r[k], ensure_ascii=False)
        w.writerow(r)
    buf.seek(0)
    name = f"journal-{day or 'all'}{'-' + strategy if strategy else ''}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{name}"'})


@app.get("/health")
async def health():
    return {"ok": True, "last_tick_ts": db.get_meta("last_tick_ts", "0")}


# V2 转正后 /v2 变成正式根路径；旧链接 302 到根，避免书签失效
@app.get("/v2", include_in_schema=False)
@app.get("/v2/{path:path}", include_in_schema=False)
async def legacy_v2(path: str = ""):
    return RedirectResponse(url="/", status_code=302)


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        # 安全：path 来自 URL 且已被 Starlette 解码，直接 DIST/path 拼接能被
        # `..%2F..%2F.env` 穿越出 dist 目录，读走任意文件（实测可读到 .env 里的
        # 交易所私钥）。这里先规范化再做前缀校验，不在 dist 内的路径一律 404。
        if path:
            base = DIST.resolve()
            try:
                target = (base / path).resolve()
                target.relative_to(base)      # 越界会抛 ValueError
            except (ValueError, OSError, RuntimeError):
                log.warning("拒绝越界静态路径: %s", path)
                return JSONResponse({"detail": "not found"}, status_code=404)
            if target.is_file():
                return FileResponse(target)
        return FileResponse(DIST / "index.html")
