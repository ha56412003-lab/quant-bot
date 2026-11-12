"""FastAPI app: dashboard API + engine background task + static frontend."""
import asyncio
import contextvars
import faulthandler
import json
import logging
import os
import signal
import threading
import time

import yaml

if hasattr(signal, "SIGUSR1"):           # SIGUSR1 is Unix-only — Windows has no such signal
    faulthandler.register(signal.SIGUSR1)   # kill -USR1 <pid> dumps all stacks
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import (binance, config, daily_target, db, engine, executor, factors,
               livekeys, netwatch, pool, quality, radar, risk, shadow, strategies, ws)

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
    # 把 uvicorn 的事件循环登记给 netwatch —— 网络告警的飞书推送要在
    # 「网络已挂」的环境里派发任务，不能依赖调用方的 loop 还活着。
    try:
        netwatch.set_loop(asyncio.get_running_loop())
        netwatch.conn()          # 预热 + 建 system_alerts 表
    except Exception as exc:  # noqa: BLE001
        log.warning("netwatch 初始化失败: %s", exc)
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
             asyncio.create_task(_mk_supervisor(supervised))]
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

# ══════════════════════════════════════════════════════════════════
# 限流 + 读鉴权（2026-11-11 · 浩哥要求）
#   ① 同一 IP 每分钟最多 N 次请求，超了返 429 —— 防高频刷 GET 拖垮单 worker
#   ② /api/* 的 GET 必须带 x-api-token（完整权限）或 x-view-key（只读观战）
#   ③ token 轮换宽限期：旧 token 放在 QB_API_TOKEN_PREV 里还能用
#
# ★ 设计原则：任一配置留空 → 对应的闸门退化回旧行为，
#   绝对不会因为「忘了配」而把操作者锁在门外。
# ══════════════════════════════════════════════════════════════════
try:
    _RATE_LIMIT_PER_MIN = int(os.environ.get("QB_RATE_LIMIT_PER_MIN") or "120")
except ValueError:
    _RATE_LIMIT_PER_MIN = 120
_RATE_WINDOW_SEC = 60.0
_rate_buckets: dict = {}          # ip -> [timestamp, ...]
_rate_lock = threading.Lock()

# ── 审计上下文（2026-11-11）──────────────────────────────────────────
# config.py 的写函数不知道 HTTP 层的 actor/ip（它是纯配置层），所以两边靠
# 这个 ContextVar + 一个注入的钩子打通：
#   _api_guard 每个请求进来先 set 一次 → 下游路由函数调 config.set_* →
#   config._audit() → _audit_bridge() → 从这两个变量取 actor/ip → db.audit()
# actor 只留前 4 位：足够区分「是不是同一个人在改」，又不构成凭据泄露。
# 后台线程（engine tick）不在请求上下文里 → get() 返回空 → 落 "system"。
_ACTOR = contextvars.ContextVar("qb_actor", default="")
_IP = contextvars.ContextVar("qb_ip", default="")


def _audit_bridge(action, target, detail, ok, err):
    """config.set_audit_hook 注入的实现：把配置层审计落到 db。"""
    db.audit(action, target, detail, ok, err,
             actor=_ACTOR.get() or "system", ip=_IP.get() or "")


# 接上钩子。config.py 不 import db（db 已经 import config，会循环），
# 只能在 main 这一侧把实现塞进去。放在模块级 = 任何一次写接口调用都不会漏记。
config.set_audit_hook(_audit_bridge)


def _client_ip(request: Request) -> str:
    """真实客户端 IP。经 Caddy 反代时 X-Real-IP 由 Caddy 覆写，客户端伪造无效。"""
    xr = (request.headers.get("x-real-ip") or "").strip()
    if xr:
        return xr
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    return request.client.host if request.client else "unknown"


def _rate_hit(ip: str):
    """滑动窗口限流。返回 (是否放行, 建议等待秒数)。"""
    if _RATE_LIMIT_PER_MIN <= 0:
        return True, 0
    now = time.time()
    cutoff = now - _RATE_WINDOW_SEC
    with _rate_lock:
        q = _rate_buckets.get(ip)
        if q is None:
            q = []
            _rate_buckets[ip] = q
        if q and q[0] < cutoff:
            q[:] = [t for t in q if t >= cutoff]
        if len(q) >= _RATE_LIMIT_PER_MIN:
            return False, max(1, int(_RATE_WINDOW_SEC - (now - q[0])) + 1)
        q.append(now)
        # ★ 字典别无限长：超过 4000 个 IP 时踢掉「最久没活动」的那批。
        #   （2026-11-11 修的一个 BUG：原先只清「空桶」，但发过请求的桶永远非空，
        #     等于一个都清不掉 —— 大量不同 IP 打进来会无限增长。）
        if len(_rate_buckets) > 4000:
            dead = sorted(_rate_buckets.items(), key=lambda kv: kv[1][-1] if kv[1] else 0)
            for k, _v in dead[:2000]:
                _rate_buckets.pop(k, None)
    return True, 0


def _take_token(request: Request) -> str:
    """从 x-api-token 或 Authorization: Bearer 取操作 token。"""
    tok = (request.headers.get("x-api-token") or "").strip()
    if not tok:
        auth = (request.headers.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    return tok


def _token_ok(tok: str) -> bool:
    """校验操作 token。支持轮换宽限期：旧 token 在 PREV 里还能用。

    ★ 2026-11-11 修过的一个坑：最初写成
        return tok in (config.API_TOKEN, config.API_TOKEN_PREV)
      当 API_TOKEN_PREV 留空时，空 token "" 会命中 ""，等于「无凭据也能写」。
      测试脚本抓出来了。现在显式拒绝空值。

    ★ 2026-11-11 第二次改：改走 livekeys 热加载，换 token 不用重启服务。
      文件 data/livekeys 里没有对应键时自动回退到 config 里的环境变量值。
    """
    cur = livekeys.api_token()
    if not tok or not cur:
        return False
    if tok == cur:
        return True
    prev = livekeys.api_token_prev()
    return bool(prev) and tok == prev


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

    path = request.url.path

    # ★ ①.5 限流（2026-11-11）：防高频刷 GET 拖垮单 worker。
    #   豁免两类：
    #     ① 本机直连（127.0.0.1 / ::1）—— SSH 排查、本机脚本不受影响
    #     ② 带有效操作 token 的请求 —— 你自己的看板每 5 秒轮询多个面板，
    #        加上静态资源，120 次/分钟根本不够，按 IP 限会把你自家人拦住。
    #        只限「没凭据的陌生请求」和「观战者」。
    cip = _client_ip(request)
    _tok_hdr = _take_token(request)
    _is_operator = bool(livekeys.api_token()) and _token_ok(_tok_hdr)

    # 审计上下文：下游 config 写函数靠它记「谁改的、从哪个 IP」。
    # 三档角色：op（操作 token）/ view（观战 key）/ anon（裸访问，本该被拦）。
    _vk = (request.headers.get("x-view-key") or "").strip()
    if _is_operator:
        _ACTOR.set("op:" + _tok_hdr[:4])
    elif _vk:
        _ACTOR.set("view:" + _vk[:4])
    else:
        _ACTOR.set("anon")
    _IP.set(cip)
    if cip not in ("127.0.0.1", "::1", "localhost") and not _is_operator:
        ok_rate, retry_after = _rate_hit(cip)
        if not ok_rate:
            log.warning("限流：ip=%s path=%s", cip, path)
            return JSONResponse(
                {"detail": f"请求过于频繁，请 {retry_after} 秒后再试"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

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

        # ③ 写操作 token（配置了才校验；支持轮换宽限期 QB_API_TOKEN_PREV）
        #    ★ 走 livekeys 热加载：换 token 立即生效，不用重启服务。
        if livekeys.api_token() and not _token_ok(_take_token(request)):
            return JSONResponse({"detail": "invalid api token"}, status_code=401)

    # ★ ④ 读操作鉴权（2026-11-11）
    #   配了 QB_VIEW_KEY 之后，/api/* 的 GET/HEAD 必须带
    #   x-api-token（完整权限）或 x-view-key（只读观战），否则 401。
    #   ★ 静态资源（/assets/*）与首页 HTML 不拦 —— 否则页面本身都打不开。
    #   ★ QB_VIEW_KEY 留空时整段跳过 = 旧行为，不会把操作者锁在门外。
    #   ★ 走 livekeys 热加载：换观战 key 立即生效，不用重启服务（浩哥硬要求）。
    elif livekeys.view_key() and path.startswith("/api/"):
        vk = livekeys.view_key()
        if livekeys.api_token() and _token_ok(_take_token(request)):
            pass                                          # 操作者，放行
        elif (request.headers.get("x-view-key") or "").strip() == vk:
            pass                                          # 观战者，放行
        else:
            log.warning("拒绝未授权的读请求: %s %s", request.method, path)
            return JSONResponse({"detail": "need view key or api token"}, status_code=401)

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
    # 浮亏闸状态（2026-11-09 决策 2 第一级）。单独算：executor.equity() 在
    # 某个币缺价时会把该笔按 0 计入，而这里缺价直接返 None（不知道就不猜）。
    _uh = float(cfg["global"].get("unrealized_halt_pct") or 0)
    _uloss = risk.unrealized_loss_pct(eq, m or "auto")
    return {
        "mode": m or executor.mode(),
        "equity": eq, "balance": bal, "unrealized": upnl,
        "daily_realized_pnl": day_pnl,
        "halted": risk.halted(),
        # VS15-1.1：halted 现在 = 手动暂停 或 日内熔断。两者必须分开给前端，
        # 否则界面上会把「我手动按的暂停」显示成「风控熔断」。
        "manual_paused": risk.manual_paused(),
        "manual_pause_ts": int(db.get_meta("manual_pause_ts", "0") or 0),
        "halt_until_ts": int(risk._halt_until_ts()),
        "unrealized_halt_pct": _uh,
        "unrealized_loss_pct": _uloss,
        # True = 当前正被浮亏闸挡着，开不了新仓（这是实时状态，价格反弹就恢复）
        "unrealized_halted": bool(_uh > 0 and _uloss is not None and _uloss >= _uh),
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
        # 2026-11-11 本地/服务器身份分离：前端顶栏按 env_label 挂标签
        # 本地 run_server.bat 设 QB_ENV_LABEL=本地测试；服务器不设 → 空串（正式盘）
        "env_label": (os.environ.get("QB_ENV_LABEL") or "").strip(),
        # 当前实际用的数据库文件名 —— 排查"两边数据对不上"时先看这个
        "db_name": Path(config.DB_PATH).name,
        # 版本号（唯一真源 backend/config.py::APP_VERSION），前端顶栏直接显示
        "version": config.APP_VERSION,
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
        # 2026-11-08 修 BUG：radar_history 只增不删 —— 平掉的仓历史记录留在表里，
        # 直接取「每币最新一条」会把早就平掉的币一直画在雷达面板上
        # （实测：表里 12 个币，当前只持 4 个 → 多出 8 张幽灵卡；持仓清零也不消失）。
        # 按当前持仓的 (symbol, side) 过滤，只留真在仓的。
        # 传 symbol 参数的单币查询不过滤（那是主动查历史，不是列表）。
        alive = {(x["symbol"], x.get("side")) for x in db.open_positions()}
        out = []
        for r in rows:
            r = dict(r)
            if (r.get("symbol"), r.get("side")) not in alive:
                continue
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


# ── 网络健康告警（2026-11-08 浩哥定）────────────────────────────────────
@app.get("/api/netstatus")
async def api_netstatus():
    """当前网络健康状态：ok / down。前端可以据此显示红点。"""
    try:
        return netwatch.status()
    except Exception as exc:  # noqa: BLE001
        return {"status": "ok", "error": str(exc)}


@app.get("/api/system-alerts")
async def api_system_alerts(limit: int = 20, only_unacked: bool = False):
    """系统级告警（网络中断 / 恢复）。

    前端轮询这个接口：有未读的 critical 就弹窗。
    不复用 radar_alerts —— 那张表是币种+评分语义，混进来雷达面板会读到脏数据。
    """
    try:
        c = netwatch.conn()
        q = "SELECT id, ts, kind, severity, message, detail, acked FROM system_alerts"
        if only_unacked:
            q += " WHERE acked = 0"
        q += " ORDER BY id DESC LIMIT ?"
        rows = [dict(r) for r in c.execute(q, (int(limit),))]
        unacked_critical = sum(
            1 for r in rows if r["severity"] == "critical" and not r["acked"])
        return {"rows": rows, "unacked_critical": unacked_critical,
                "net": netwatch.status()}
    except Exception as exc:  # noqa: BLE001
        return {"rows": [], "unacked_critical": 0, "error": str(exc)}


@app.post("/api/system-alerts/{alert_id}/ack")
async def api_system_alert_ack(alert_id: int):
    """前端点「知道了」→ 置 acked=1，不再弹。"""
    try:
        c = netwatch.conn()
        c.execute("UPDATE system_alerts SET acked = 1 WHERE id = ?", (int(alert_id),))
        c.commit()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.get("/api/positions")
async def api_positions():
    cfg = config.load_strategies()
    glev = int((cfg.get("global") or {}).get("max_gross_leverage", 3) or 3)
    mmr = 0.005          # 维持保证金率近似值（币安 USDT 永续小额档 ~0.4-0.5%）
    out = []
    for p in db.open_positions():
        # ⚠ 2026-11-09：原来 mark_price_of 返回 None 时用开仓价顶替，
        # 于是 upnl=0、liq_dist 看着「离爆仓很远」——断线/新币还没订阅价时，
        # 页面上就是一片「盈亏 0、安全」的假象，实际上可能在爆仓边缘。
        # 现在把「没有真实价」这件事显式带出去，前端显示 --。
        real_px = factors.mark_price_of(p["symbol"])
        stale = not real_px
        price = real_px or p["entry_price"]
        sign = 1 if p["side"] == "long" else -1
        upnl = (price - p["entry_price"]) * p["qty"] * sign
        scfg = (cfg.get("strategies") or {}).get(p["strategy"], {}) or {}
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

        # ── S15 出场状态：只剩峰顶 R 和距峰顶回撤 ──
        # 现役出场只有五条（硬止损 → 保本 1.5R → 平半 1.2R → 全平 2.5R → 右侧止盈），
        # 没有「回撤出局线」这个概念：右侧止盈看的是 HA 反向 + 跌回 ZLSMA 下方，
        # 不是回撤多少个 R 就走。
        # 所以 ma_break_cnt / ma_confirm_bars / trail_target_r 等展示字段一并
        # 删除：它们对应的参数在 yaml 里也已不存在，读出来永远是默认值，纯属空转。
        # peak_r / drawdown_r 仍然真实保留：这两个数在卡片上有意义
        # （能看到「曾经赚到多少、现在回吐了多少」），但它们**不参与任何决策**。
        st = strategies._S15_POS_STATE.get(
            (p["symbol"], p["side"], p.get("opened_at"))) or {}
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

        # ── B 档出场可视化参数（2026-11-10）────────────────────────────
        # 现役出场只有四条：硬止损 / 1.2R 平半 / 2.5R 兜底全平 / 右侧止盈。
        # 前三条是「价位触发」，前端用 R 轴刻度画；第四条是「K线形态触发」，
        # 策略在 0.5s 循环里现算现用、不落库，所以图上不画灯、只留规则图例。
        # ⚠ 原来这里给「镜像出场三闸 + 快速失败」喂 exit_*/qf_* 实时字段，
        #   那两套规则 B 档已整条删除 → 字段一起删掉，否则前端会画一条
        #   已经不存在的规则（比不画更糟）。
        _bp = scfg.get("params") or {}
        _half_r = float(_bp.get("half_at_r") or 0.0)
        _full_r = float(_bp.get("full_at_r") or 0.0)
        _half_done = bool(p.get("half_closed"))

        out.append({**p, "mark_price": price, "unrealized_pnl": upnl,
                    # stale=True 表示这笔仓位当前**没有真实盘口价**，
                    # mark_price / unrealized_pnl / liq_dist_pct 都是按开仓价
                    # 兜底算出来的占位值，前端必须显示 --，不能当真实数字用。
                    "stale": stale,
                    "notional": abs(p["qty"] * price),
                    "leverage": lev, "liq_price": liq, "liq_dist_pct": liq_dist,
                    "stop_dist_pct": stop_dist,
                    "peak_r_mem": float(peak_r_mem) if peak_r_mem is not None else None,
                    "drawdown_r": drawdown_r,
                    # 出场刻度：1.2R 平半 / 2.5R 兜底全平（值从配置读，前端不写死）
                    "half_at_r": _half_r, "full_at_r": _full_r,
                    "half_done": _half_done,
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
    # ⚠ 2026-11-09：原来是 float(x or 1.0) —— fraction=0 会走 `0 or 1.0` 变成 1.0，
    # 然后被当成「手动全平」。0 和 "0" 还得到不同结果（数字 0 → 全平，字符串 "0" → 1%）。
    # 现在是显式区间校验：不在 (0,1] 直接 400，绝不做兜底转换。
    raw = (body or {}).get("fraction", 1.0)
    try:
        fraction = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, "fraction must be a number")
    if not (0 < fraction <= 1.0):
        raise HTTPException(400, "fraction must be in (0, 1]（要全平请传 1.0，不能传 0）")
    label = "手动全平" if fraction >= 0.999 else f"手动平仓 {fraction*100:.0f}%"
    ok = await executor.close_position(pos, price, label, fraction)
    if not ok:
        # 200 + ok:false 也是失败。前端会按失败弹窗，不会谎报「已平」。
        return {"ok": False, "fraction": fraction, "symbol": pos["symbol"],
                "error": "平仓未成交（交易所拒单/未成交），或该仓位已被其他路径平掉"}
    return {"ok": True, "fraction": fraction, "symbol": pos["symbol"]}


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
    # ── 2026-11-11 改：手动全平不再把日目标置为 done ─────────────────────
    # 原实现是「平完就 mark_done → paused_today=True」，后果是 risk.py::
    # allow_entry 的第二道闸 _dtm.blocked() 从这一刻起拒掉当天所有信号，
    # 而界面上没有任何说明，用户以为「平完继续做」，等一整天没单还
    # 以为策略坏了（2026-11-11 16:08 实案：ERAUSDT 平仓后 2 小时零开仓，
    # /api/status 却是 hit=true —— 因为 hit 原本只看 state 不看盈亏）。
    #
    # 两者的语义本来就不同：
    #   手动一键全平 = 清掉仓位重新来（风险动作）
    #   日目标 done  = 当日盈利 ≥ target，落袋收工（收益动作）
    # 日目标该不该停开新仓，交给 daily_target.tick() 按 pnl >= target 判断。
    out = {"ok": failed == 0, "closed": closed, "failed": failed,
           "paused_today": False}
    if failed:
        out["error"] = (f"{failed} 个仓位没平掉（交易所拒单/未成交，或已被其他路径平仓），"
                        f"请刷新持仓确认后再试")
    return out


@app.post("/api/ops/pause")
async def api_ops_pause(payload: dict):
    """手动暂停 / 恢复运营（VS15-1.1 新增）。

    用途：系统更新、改参数、换凭据之前，先把「开新仓」这道闸关上，
    避免更新过程中间冒出一笔新单。

    ⚠ 只停开新仓，**不动已开的仓位**。要不要平仓是另一个决定，走
      `POST /api/positions/close-all`。想让仓位在更新前干净退出，顺序是：
      先 pause → 再 close-all（或等它自己按离场规则走完）。

    与日内熔断完全分开存（manual_pause vs halt_until）：
      恢复运营不能顺手把风控熔断也解掉。
    """
    action = str((payload or {}).get("action") or "").strip().lower()
    if action not in ("pause", "resume"):
        raise HTTPException(400, "action 必须是 'pause' 或 'resume'")
    before = risk.manual_paused()
    open_n = len(db.open_positions())
    if action == "pause":
        db.set_meta("manual_pause", "1")
        db.set_meta("manual_pause_ts", int(time.time()))
        db.log("warn", f"手动暂停运营：停止开新仓（已开 {open_n} 个仓位不受影响）")
    else:
        db.set_meta("manual_pause", "0")
        db.set_meta("manual_pause_ts", "0")
        db.log("info", "手动恢复运营：允许开新仓")
    after = risk.manual_paused()
    try:
        config._audit("ops_" + action, "global",
                      {"manual_paused": after, "open_positions": open_n})
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "manual_paused": after, "halted": risk.halted(),
            "changed": before != after, "open_positions": open_n}


@app.post("/api/positions/{pid}/leverage")
async def api_set_position_leverage(pid: int, payload: dict):
    """手动调整单个仓位的杠杆：只改保证金占用与爆仓价，不改仓位数量。"""
    pos = db.get_position(pid)
    if not pos or pos["status"] != "open":
        raise HTTPException(404, "position not open")
    # ⚠ 2026-11-09：实盘下这个接口只改本地库，交易所那边一点没动。
    # 界面却按新杠杆重算并显示爆仓价 → 给操作员一个假的安全边际。
    # 全仓改杠杆在交易所侧还要校验保证金是否够，不是改个数就完事，所以直接拒绝。
    if executor.mode() == "live":
        raise HTTPException(400, {
            "error": "实盘仓位的杠杆不能在系统里改",
            "fix": "请在交易所 App 里调整该币的杠杆，系统只做展示，不代改",
        })
    try:
        lev = int(payload.get("leverage") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "leverage must be an integer")
    if lev < 1 or lev > 125:
        raise HTTPException(400, "leverage must be 1-125")
    old = pos.get("leverage")
    ok = db.set_position_field(pid, "leverage", float(lev))
    if not ok:
        raise HTTPException(409, "仓位已平，无法修改")
    db.log("info", f"手动调整杠杆 {pos['symbol']} {old or '-'}x → {lev}x")
    return {"ok": True, "leverage": lev, "previous": old}


@app.post("/api/positions/{pid}/margin")
async def api_add_position_margin(pid: int, payload: dict):
    """追加保证金：从可用保证金划入该仓位，推远爆仓价。"""
    pos = db.get_position(pid)
    if not pos or pos["status"] != "open":
        raise HTTPException(404, "position not open")
    # ⚠ 2026-11-09：实盘下没有任何代码调交易所的追加保证金接口
    # （binance.py / exchanges.py 都没实现），这里只写本地 extra_margin。
    # 而前端拿这个值算「爆仓价拉远了多少」—— 纯属幻觉。
    if executor.mode() == "live":
        raise HTTPException(400, {
            "error": "实盘不能追加保证金",
            "fix": "请在交易所 App 里手动加保证金；系统这边不代操作，避免账面与实际不符",
        })
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
    if new_extra is None:
        raise HTTPException(409, "仓位已平，无法追加保证金")
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


# ── 进场 EFF 门槛（带 mtime 缓存的小读取）─────────────────────────────
# ⚠ 绝对不要在这里调 config.load_strategies()：那个函数顺手会把
#   config.UNIVERSE 重置成「候选池」，而 /api/gates 每 2 秒被前端轮询一次 ——
#   等于每 2 秒把内存里的大盘清单搅一遍（2026-11-10 踩到：
#   tests/test_gates_verdict.py 打桩的 UNIVERSE 被清成真实候选池，断言拿到 0 行）。
_EFF_SIG_CACHE = {"mtime": 0.0, "val": 0.0}


def _eff_signal_min() -> float:
    """进场 EFF 门槛（strategies.yaml → S15 params → eff_min_signal）。"""
    try:
        mt = config.STRATEGIES_PATH.stat().st_mtime
        if mt != _EFF_SIG_CACHE["mtime"]:
            cfg = yaml.safe_load(config.STRATEGIES_PATH.read_text(encoding="utf-8")) or {}
            p = ((cfg.get("strategies") or {}).get("S15_CE_ZLSMA") or {}).get("params") or {}
            _EFF_SIG_CACHE["val"] = float(p.get("eff_min_signal") or 0.0)
            _EFF_SIG_CACHE["mtime"] = mt
    except Exception as exc:  # noqa: BLE001
        log.debug("read eff_min_signal failed: %s", exc)
    return _EFF_SIG_CACHE["val"]


@app.get("/api/gates")
async def api_gates():
    """进场闸门诊断：监控池每个币最新一根 15m 的七道闸状态。

    数据来自策略扫描时留在内存里的中间结果（strategies._GATE_STATE），
    不额外拉 K 线，零流量成本。回答的问题是：今天没开仓，是
    「真没机会」还是「门槛卡死了」。
    """
    gs = strategies._GATE_STATE
    mon = set(quality.monitor_list() or [])
    eff_sig = _eff_signal_min()
    # 监控池快照：拿每个币的「趋势效率 EFF」和池子判定原因，用来解释「为什么在池外」
    try:
        qdata = (quality.snapshot() or {}).get("data") or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("gates: quality snapshot failed: %s", exc)
        qdata = {}
    eff_min = float(getattr(quality, "EFF_MIN", 0.30) or 0.30)
    # ★ 2026-11-09 修：策略说「全部闸门通过」≠ 真开仓。
    #   信号后面还有 risk.allow_entry 九道闸，任何一道拒绝都没有仓位
    #   （实例：RSR 06:45 全部闸门通过，但被「已达日目标」拦下，页面却写「已开仓」）。
    #   所以结论列必须用两样真实事实覆盖：
    #     （持仓币已在上面的循环里整行跳过，不在本表出现）
    #     ② signals 表本根 K 线的最新信号被拒 → 直接显示它的 skip_reason
    # ★ 2026-11-09 再修：池外的币永远不会被扫描，_GATE_STATE 是历史残留。
    #   照旧显示「全过·待成交」会让人以为在排队等开仓（实际等到天亮也不开）。
    #   池外一律明说「池外」，并尽可能带上被踢原因（EFF 不达标 / 池满）。
    held = {p["symbol"] for p in db.open_positions()}
    sig_map = {}
    try:
        for s in db.recent_signals(400):
            sig_map.setdefault(s.get("symbol"), s)      # 已按 id DESC，首条即最新
    except Exception as exc:  # noqa: BLE001
        log.debug("gates: read signals failed: %s", exc)

    rows = []
    bar = None
    for sym in config.UNIVERSE:
        v = gs.get(sym)
        if not v:
            continue
        # ★ 2026-11-09 浩哥定：已持仓的币不进光闸面板。
        #   原因：策略对持仓币直接 continue（不用再判断能不能开），它的 ce/ha
        #   是开仓前留下的旧值，挂在表上会让人以为「又反转了」（LIT 实例）。
        #   能不能开仓看这张表，持仓状态看持仓卡，两边各管各的。
        if sym in held:
            continue
        if v.get("ts") and (bar is None or v["ts"] > bar):
            bar = v["ts"]
        risk = v.get("risk")
        risk_thr = v.get("risk_thr") or 0
        # 2026-11-11：第⑤道闸从「只有上限」变成「有下限也有上限」。
        # 下限（min_stop_dist_pct 0.60）由 strategies 通过 risk_lo 传过来，
        # 不在这里读 yaml —— 保持「门槛值只有一个真源」。
        risk_lo = v.get("risk_lo") or 0
        # ── 结论列：以真实事实为准 ──────────────────────────────
        sig = sig_map.get(sym) or {}
        sig_ts = int(sig.get("ts") or 0)
        # 信号必须①不早于本轮判定 ②足够新鲜（5 分钟内）。
        # 否则会出现：浩哥刚点了「今日重置」，页面却还在显示重置前那条
        # 「已达日目标」——旧拒绝原因已经失效，却一直挂在表上（2026-11-09 踩到）。
        sig_fresh = sig_ts >= int(v.get("ts") or 0) and (time.time() - sig_ts) <= 300
        if sym in held:
            verdict = "已开仓"
        elif sym not in mon:
            # 池外：策略扫不到它，上面的 ce/ha/bbw 是它上一次还在池里时的残留值。
            # 必须明说「池外」，否则看起来像在排队等成交。
            q = qdata.get(sym) or {}
            eff = q.get("eff")
            try:
                eff = float(eff) if eff is not None else None
            except (TypeError, ValueError):
                eff = None
            if eff is None:
                verdict = "池外·不参与扫描"
            elif eff < eff_min:
                verdict = "池外·EFF %.2f 未达 %.2f" % (eff, eff_min)
            else:
                verdict = "池外·池满未入选"
        elif v.get("ok") and sig_fresh and not sig.get("acted") and sig.get("skip_reason"):
            # 只有「这一轮全部闸门真的过了、信号刚刚真被风控拦下」才报拦截原因，
            # 否则会出现「CE 都没翻」却写着「已达日目标」的自相矛盾（2026-11-09 踩到）。
            verdict = sig["skip_reason"]          # 例：已达日目标，今日停止开新仓
        elif v.get("ok"):
            verdict = "全闸通过·待成交"
        else:
            verdict = v.get("why") or ""
        rows.append({"symbol": sym, "in_monitor": sym in mon,
                     # 第五道闸：下限 ≤ 止损距离 ≤ 上限（两个都为 0 表示这道闸关闭）
                     "risk_ok": (risk is not None
                                 and (risk <= risk_thr if risk_thr > 0 else True)
                                 and (risk >= risk_lo if risk_lo > 0 else True)),
                     "risk_lo": risk_lo,
                     "opened": sym in held, "verdict": verdict,
                     **v,                                  # 含 key（关键K线）/ eff（进场 EFF）
                     "eff_thr": eff_sig,                   # 逐行给门槛，前端才能画 0.73/0.30
                     "pool_eff": (qdata.get(sym) or {}).get("eff"),
                     "why": verdict})
    # 有戏的排前面：在池 > 全闸通过 > 时段方向 > HA 成立 > 止损合适 > BBW 高
    rows.sort(key=lambda r: (not r.get("in_monitor"), not r.get("ok"), not r.get("ce"),
                             not r.get("ha"), not r.get("risk_ok"),
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
        "risk_n": sum(1 for r in rows if r.get("risk_ok")),
        # ②b 关键K线、⑥ 进场 EFF（门槛从配置读，不在前端写死）
        "key_n": sum(1 for r in rows if r.get("key")),
        "eff_n": sum(1 for r in rows
                     if r.get("eff") is not None and r["eff"] >= eff_sig),
        "eff_thr": eff_sig,
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
    except ValueError as exc:
        # 2026-11-11：把具体原因带出去。原来统一回 "invalid config value"，
        # 用户/脚本只能知道「填错了」，不知道「错在哪、允许范围多少」。
        # 风控边界校验的报错本身就写清了 "leverage=999 超出允许范围 1 ~ 125"。
        # 同时落一条失败审计 —— 被拒的尝试也是证据。
        try:
            config.audit_rejected("set_strategy_params", sid,
                                  {"payload": payload}, exc)
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(400, str(exc) or "invalid config value")
    except TypeError:
        try:
            config.audit_rejected("set_strategy_params", sid,
                                  {"payload": payload}, "TypeError")
        except Exception:  # noqa: BLE001
            pass
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
        try:
            config.audit_rejected("set_global_risk", "global", {"payload": payload}, exc)
        except Exception:  # noqa: BLE001
            pass
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
            "qv": q.get("qv"), "spike": q.get("spike"),
            # 2026-11-08 新闸门：止损距离（平滑后）+ 趋势效率（平滑后）
            "stop_pct": q.get("stop_pct"), "eff": q.get("eff"),
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
            # 2026-11-08 新闸门参数
            "eff_min": snap.get("eff_min"),
            "eff_exit": snap.get("eff_exit"),
            "eff_win": snap.get("eff_win"),
            "smooth_sec": snap.get("smooth_sec"),
            "kl_interval": snap.get("kl_interval"),
            "min_dwell_sec": snap.get("min_dwell_sec"),
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
            "position_max": config.load_strategies().get("global", {}).get("max_concurrent_coins")}


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
    # ★ 2026-11-11：加币种校验。原来任意字符串都能写进 yaml（NOTACOIN、
    #   甚至超长串），与 set_strategy_symbols 的标准不一致 —— 同一条规则
    #   在另一个入口漏了，用户填错只能靠候选池里的「已跳过」提示才发现。
    _allowed = set(config.UNIVERSE) | set(config.KNOWN_SYMBOLS)
    _bad = [s for s in wl if s not in _allowed]
    if _bad:
        raise HTTPException(
            400, f"未知交易币种: {','.join(_bad[:5])}（需在全局监控或已知币列表内）")
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
    except RuntimeError as e:
        # 实盘有持仓时不许换交易所（见 executor.set_exchange 注释）
        raise HTTPException(400, {"error": str(e),
                                  "fix": "先把实盘仓位全部平掉，再切换执行交易所"})
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
    else:
        # ⚠ 2026-11-09 加：切回纸面前必须确认实盘仓位已清空。
        # 原因：db.open_positions() 默认只返回**当前模式**的仓位，
        # engine.manage_positions 正是用它遍历。切到 paper 的那一刻，实盘仓位
        # 立刻失去硬止损 / 保本 1.5R / 平半 1.2R / 全平 2.5R / 右侧止盈 的全部管理 —— 而交易所侧没有
        # 止损单，等于永久裸奔；同时页面按当前模式查询会显示「0 持仓」，骗人。
        live_open = db.open_positions(mode="live")
        if live_open and executor.mode() == "live":
            syms = [p["symbol"] for p in live_open]
            raise HTTPException(400, {
                "error": f"实盘还有 {len(live_open)} 个持仓，切回纸面后它们将失去全部止损管理",
                "positions": syms,
                "fix": "请先在实盘模式下全部平仓，再切换",
            })
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


# ── 审计查询（2026-11-11）────────────────────────────────────────────
# 只给操作 token 看：观战者不该知道「谁在什么时候把参数改成了什么」，
# 审计行里还带 IP。
#   GET /api/audit?limit=100
#   GET /api/audit?action=set_*        按动作前缀筛
#   GET /api/audit?only_fail=1         只看被拒/失败的
@app.get("/api/audit")
async def api_audit(request: Request, limit: int = 100, action: str = "",
                    only_fail: int = 0):
    if not _token_ok(_take_token(request)):
        raise HTTPException(status_code=403, detail="需要操作 token 才能查看审计")
    return {
        "total": db.audit_count(),
        "rows": db.audit_recent(
            limit=limit, action=(action or None), only_fail=bool(only_fail)),
    }


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
