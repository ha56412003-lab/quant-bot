"""网络健康监控 —— 断网 / 代理挂 / 封 IP 时自动告警。

背景（2026-11-08 浩哥定）：
    出场、下单、行情全部走 binance._get，那一层原本没有 try —— 断网、代理
    (127.0.0.1:7897) 挂掉、币安封 IP，会一路抛到上层；各模块各自 try 掉，
    结果是「系统看起来正常在跑，其实什么都拿不到，仓位也没人管」。
    浩哥要的是：自动重连 3 次；3 次都不成功 → 页面弹窗 + 飞书推送。

设计原则：
    1. 物理隔离 —— 只依赖 db / log，不 import strategies / engine / radar
    2. 库是保底，飞书是加分 —— 网络挂了飞书多半也发不出，但写本地库一定成功，
       所以前端弹窗靠库，飞书失败只记 warning 不抛
    3. 防刷屏 —— 同类告警 5 分钟冷却，否则每 0.5 秒一轮会把库写爆
    4. 状态机单调 —— ok → down 只推一次，down → ok 推一次恢复

判定口径（浩哥原话「三次不成功」）：
    单次请求重试 3 次全失败        → 记一次故障事件
    60 秒内累计 3 次故障事件       → 判定「网络瘫痪」→ 告警
    之后任意一次请求成功           → 判定「已恢复」→ 通知
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time

from . import config

log = logging.getLogger("netwatch")

# ── 阈值 ──────────────────────────────────────────────────────────
FAIL_THRESHOLD = 3        # 60 秒内累计几次「重试后仍失败」判定为瘫痪
FAIL_WINDOW_SEC = 60      # 上面那个计数的时间窗口
COOLDOWN_SEC = 300        # 同类告警冷却 5 分钟，防刷屏
RETRY_TIMES = 3           # 每个请求最多试几次（1 次原始 + 2 次重试）
RETRY_BACKOFF = (0.3, 0.9)  # 两次重试前的等待秒数

_state = {
    "status": "ok",          # ok / down
    "fails": [],             # 最近窗口内的失败时间戳
    "last_push": {},         # {kind: ts} 各类告警最后推送时间
    "last_detail": "",       # 最近一次失败原因，给弹窗显示
    "down_since": 0,         # 判定瘫痪的时刻
}

_conn = None

# 事件循环引用：binance._get 是从 uvicorn 的 loop 里调的，但告警推送要
# 在「网络已挂」的环境里跑，不能依赖调用方还活着 —— 记住主 loop 备用。
_loop = None


def set_loop(loop):
    """服务启动时登记 uvicorn 的事件循环，供告警推送用。"""
    global _loop
    _loop = loop


def conn():
    """自己的连接（与 radar.py 同模式：物理隔离，不碰 db 模块内部）。"""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=10.0)
        _conn.row_factory = sqlite3.Row
        for p in ("PRAGMA journal_mode=WAL", "PRAGMA busy_timeout=8000"):
            try:
                _conn.execute(p)
            except sqlite3.Error:
                pass
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS system_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            kind TEXT NOT NULL,          -- net_down / net_recovered
            severity TEXT,               -- critical / info
            message TEXT,
            detail TEXT,
            acked INTEGER DEFAULT 0      -- 前端点「知道了」置 1
        );
        CREATE INDEX IF NOT EXISTS ix_sysalert_ts ON system_alerts(ts DESC);
        """)
        _conn.commit()
    return _conn


def _write_alert(kind, severity, message, detail):
    """写本地库 —— 这是前端弹窗的唯一数据源，必须成功。"""
    try:
        c = conn()
        c.execute(
            "INSERT INTO system_alerts(ts, kind, severity, message, detail) "
            "VALUES(?,?,?,?,?)",
            (int(time.time()), kind, severity, message, detail[:500]))
        c.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("netwatch 写告警库失败: %s", exc)
        return False


# ── 飞书推送（复用 daily_target 那套凭证）──────────────────────────

def _feishu_targets():
    """从 Hermes .env 读 app 凭证与接收人（只读，失败返回 None）。"""
    try:
        env = {}
        with open(r"E:\Hermes\.env", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        aid, sec = env.get("FEISHU_APP_ID"), env.get("FEISHU_APP_SECRET")
        if not aid or not sec:
            return None
        users = [x.strip() for x in env.get("FEISHU_ALLOWED_USERS", "").split(",")
                 if x.strip()]
        ids = [u for u in users if u.startswith("ou_")]
        if not ids:
            return None
        return aid, sec, ids
    except Exception as exc:  # noqa: BLE001
        log.debug("netwatch 读飞书配置失败: %s", exc)
        return None


async def _feishu_send(text):
    """推一条飞书文本消息。失败只记 warning，绝不抛（网络可能本来就挂了）。"""
    t = _feishu_targets()
    if not t:
        log.debug("netwatch 飞书未配置，跳过推送")
        return False
    aid, sec, ids = t
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as cli:
            r = await cli.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": aid, "app_secret": sec})
            tok = (r.json() or {}).get("tenant_access_token")
            if not tok:
                log.warning("netwatch 拿飞书 token 失败")
                return False
            ok = 0
            for oid in ids:
                rr = await cli.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages"
                    "?receive_id_type=open_id",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"receive_id": oid, "msg_type": "text",
                          "content": json.dumps({"text": text},
                                                ensure_ascii=False)})
                if (rr.json() or {}).get("code") == 0:
                    ok += 1
            log.info("netwatch 飞书推送成功 %s/%s", ok, len(ids))
            return ok > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("netwatch 飞书推送失败（网络可能已断）: %s", exc)
        return False


def _push_async(kind, text):
    """后台推飞书，不阻塞调用方。

    优先用登记的主 loop（服务启动时 set_loop）；退化到当前 loop；
    都没有（纯脚本调用）就跳过 —— 库已经写了，前端弹窗不受影响。
    """
    try:
        loop = _loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        if loop is None or loop.is_closed():
            log.debug("netwatch 无可用事件循环，跳过飞书（库已写）")
            return
        loop.create_task(_feishu_send(text))
    except Exception as exc:  # noqa: BLE001
        log.debug("netwatch 飞书任务派发失败: %s", exc)


def _cooled(kind):
    """同类告警是否在冷却期内。"""
    last = _state["last_push"].get(kind, 0)
    return (time.time() - last) < COOLDOWN_SEC


# ── 对外接口 ──────────────────────────────────────────────────────

def report_failure(where, exc):
    """一次「重试 3 次后仍失败」的请求失败。

    where: 请求标识，如 'GET /fapi/v1/klines BTCUSDT'
    exc:   最后一次异常
    """
    now = time.time()
    detail = f"{where} → {type(exc).__name__}: {exc}"[:400]
    _state["last_detail"] = detail

    # 只保留窗口内的失败记录
    fails = [t for t in _state["fails"] if now - t <= FAIL_WINDOW_SEC]
    fails.append(now)
    _state["fails"] = fails

    # 还没到阈值，静默
    if len(fails) < FAIL_THRESHOLD:
        log.debug("netwatch 失败计数 %s/%s: %s", len(fails), FAIL_THRESHOLD, detail)
        return

    # 已达阈值，判定瘫痪（但同一状态不重复推）
    if _state["status"] == "down":
        return
    _state["status"] = "down"
    _state["down_since"] = now

    msg = (f"🔴 交易系统网络中断\n"
           f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
           f"{FAIL_WINDOW_SEC} 秒内连续 {FAIL_THRESHOLD} 次请求失败（每次已重试 "
           f"{RETRY_TIMES} 次）\n"
           f"原因：{detail}\n"
           f"⚠️ 持仓出场判断已失效，请检查网络 / 代理 7897 / 币安 IP 封禁")

    _write_alert("net_down", "critical", "交易系统网络中断", detail)
    if not _cooled("net_down"):
        _state["last_push"]["net_down"] = now
        _push_async("net_down", msg)
    log.error("netwatch 判定网络瘫痪: %s", detail)


def report_success():
    """任意一次请求成功 —— 如果之前在瘫痪态，判定恢复。"""
    _state["fails"] = []
    if _state["status"] != "down":
        return
    down_sec = int(time.time() - _state["down_since"])
    _state["status"] = "ok"

    msg = (f"🟢 交易系统网络已恢复\n"
           f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
           f"中断时长：约 {down_sec // 60} 分 {down_sec % 60} 秒")

    _write_alert("net_recovered", "info", "交易系统网络已恢复", f"中断 {down_sec} 秒")
    if not _cooled("net_recovered"):
        _state["last_push"]["net_recovered"] = time.time()
        _push_async("net_recovered", msg)
    log.info("netwatch 网络已恢复（中断 %s 秒）", down_sec)


def status():
    """给 /api/netstatus 用。"""
    return {
        "status": _state["status"],
        "recent_fails": len(_state["fails"]),
        "threshold": FAIL_THRESHOLD,
        "window_sec": FAIL_WINDOW_SEC,
        "detail": _state["last_detail"],
        "down_since": _state["down_since"],
    }


# ── 请求包装（给 binance._get / _signed 用）──────────────────────

async def with_retry(fn, where, *args, **kwargs):
    """执行 fn，失败自动重试 RETRY_TIMES 次。

    成功 → report_success()，失败（全试完）→ report_failure() 后原样抛出。
    ★ 必须原样抛出：调用方要靠异常决定跳过本轮，吞掉会造成「静默拿空数据」。
    """
    last: BaseException = RuntimeError(f"netwatch: {where} 未执行")
    for attempt in range(RETRY_TIMES):
        try:
            r = await fn(*args, **kwargs)
            report_success()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < RETRY_TIMES - 1:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                log.debug("netwatch %s 第 %s 次失败，%.1fs 后重试: %s",
                          where, attempt + 1, wait, exc)
                await asyncio.sleep(wait)
    report_failure(where, last)
    raise last


def env_flag(name, default=True):
    """小工具：从环境变量读开关。"""
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")
