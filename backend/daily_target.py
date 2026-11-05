"""日目标（落袋为安）—— 当日盈利达到目标时提醒 + 超时自动全平 + 停止开新仓。

状态机：
    idle     → 未达标
    armed    → 已达标，观察中（要求持续 sustain_sec 才算真达标，防止插针误触）
    alerted  → 已提醒（发飞书 + 前端横幅），等待人工平仓
    done     → 已处理完（人工平了 / 自动平了），当天不再开新仓

全部行为由 global.daily_target 配置控制，enabled=false 即完全关闭。
"""
from __future__ import annotations

import datetime
import json
import logging
import time
import urllib.request

from . import db

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "target_usd": 200.0,
    "sustain_sec": 300,
    "auto_close_sec": 1800,
    "pause_new": True,
    "notify": True,
}

S = "daily_target:"          # meta 前缀


def cfg(global_cfg) -> dict:
    raw = (global_cfg or {}).get("daily_target") or {}
    out = dict(DEFAULTS)
    for k, v in raw.items():
        if k in out and v is not None:
            out[k] = type(DEFAULTS[k])(v) if not isinstance(v, bool) else bool(v)
    return out


def _today() -> str:
    return datetime.date.today().isoformat()


def _g(k, default=None):
    v = db.get_meta(S + k, "")
    return v if v != "" else default


def _s(k, v):
    db.set_meta(S + k, str(v))


def reset():
    for k in ("state", "hit_ts", "alert_ts", "day", "pnl_at_alert"):
        _s(k, "")


def state() -> dict:
    return {
        "state": _g("state", "idle"),
        "hit_ts": int(_g("hit_ts", 0) or 0),
        "alert_ts": int(_g("alert_ts", 0) or 0),
        "day": _g("day", ""),
        "pnl_at_alert": float(_g("pnl_at_alert", 0) or 0),
    }


def blocked() -> bool:
    """是否应阻止开新仓（达到日目标后）。"""
    st = state()
    if st["day"] != _today():
        return False
    return st["state"] in ("alerted", "done")


# ── 飞书推送 ────────────────────────────────────────────────

def _feishu_targets():
    """从 Hermes .env 读 app 凭证与接收人（只读，失败则返回 None）。"""
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
        users = [x.strip() for x in env.get("FEISHU_ALLOWED_USERS", "").split(",") if x.strip()]
        ids = [u for u in users if u.startswith("ou_")]
        if not ids:
            return None
        return aid, sec, ids
    except Exception as exc:  # noqa: BLE001
        log.debug("feishu env read failed: %s", exc)
        return None


def _push(text: str) -> bool:
    t = _feishu_targets()
    if not t:
        log.warning("飞书未配置，跳过推送")
        return False
    aid, sec, ids = t
    try:
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": aid, "app_secret": sec}).encode(),
            headers={"Content-Type": "application/json"})
        tok = json.loads(urllib.request.urlopen(req, timeout=10).read())["tenant_access_token"]
        ok = 0
        for oid in ids:
            body = {"receive_id": oid, "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False)}
            r2 = urllib.request.Request(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + tok})
            try:
                if json.loads(urllib.request.urlopen(r2, timeout=10).read()).get("code") == 0:
                    ok += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("飞书推送失败 %s: %s", oid, exc)
        log.info("飞书推送成功 %d/%d", ok, len(ids))
        return ok > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("飞书推送异常: %s", exc)
        return False


# ── 主循环 ──────────────────────────────────────────────────

async def tick(equity: float, day_start_equity: float, close_all_cb, c: dict):
    """每轮调用。close_all_cb() 用于自动全平。"""
    if not c["enabled"]:
        if state()["state"] not in ("", "idle"):
            reset()
        return

    today = _today()
    st = state()
    if st["day"] != today:
        reset()
        _s("day", today)
        st = state()
        st["day"] = today

    pnl = float(equity) - float(day_start_equity)
    target = c["target_usd"]
    now = int(time.time())
    cur = st["state"] or "idle"

    if cur == "idle":
        if pnl >= target:
            _s("state", "armed")
            _s("hit_ts", now)
            _s("day", today)
            log.info("日目标达到 %.2f >= %.2f，进入观察期", pnl, target)
        return

    if cur == "armed":
        # 回吐超过 20% 目标，取消
        if pnl < target * 0.8:
            _s("state", "idle")
            _s("hit_ts", 0)
            log.info("日目标回吐至 %.2f，取消观察", pnl)
            return
        if now - st["hit_ts"] >= c["sustain_sec"]:
            _s("state", "alerted")
            _s("alert_ts", now)
            _s("pnl_at_alert", round(pnl, 2))
            log.info("日目标确认达标 %.2f，已提醒", pnl)
            if c["notify"]:
                mins = (c["auto_close_sec"] // 60) if c["auto_close_sec"] else 0
                tail = (f"\n{mins} 分钟内未平仓将自动全平。" if mins else "")
                _push("【量化系统 · 日目标达成】\n"
                      f"今日盈亏 +${pnl:.2f}（目标 ${target:.0f}）\n"
                      f"当前净值 ${equity:.2f}\n"
                      f"已提醒落袋：请打开控制台确认一键全平。{tail}")
        return

    if cur == "alerted":
        # 没有持仓了 → 视为已落袋
        if close_all_pending(c):
            pass
        if c["auto_close_sec"] and now - st["alert_ts"] >= c["auto_close_sec"]:
            log.warning("日目标提醒超时 %.0f 分钟，自动全平", (now - st["alert_ts"]) / 60)
            try:
                await close_all_cb()
                _s("state", "done")
                if c["notify"]:
                    _push(f"【量化系统】日目标提醒超时，已自动全平落袋。\n今日盈亏 +${pnl:.2f}")
            except Exception as exc:  # noqa: BLE001
                log.warning("自动全平失败: %s", exc)
        return


def close_all_pending(c):
    return False


def mark_done(reason="手动"):
    _s("state", "done")
    log.info("日目标置为 done（%s）", reason)


def summary(equity, day_start_equity, c):
    """给 /api/status 用的摘要。"""
    pnl = float(equity) - float(day_start_equity)
    st = state()
    target = c["target_usd"] or 1
    left = None
    if st["state"] == "alerted" and c["auto_close_sec"] and st["alert_ts"]:
        left = max(0, c["auto_close_sec"] - (int(time.time()) - st["alert_ts"]))
    return {
        "enabled": c["enabled"],
        "state": st["state"] if st["day"] == _today() else "idle",
        "pnl": round(pnl, 2),
        "target": c["target_usd"],
        "pct": round(min(999, pnl / target * 100), 1),
        "hit": bool(st["state"] in ("alerted", "done") and st["day"] == _today()),
        "blocked": blocked(),
        "auto_close_left": left,
        "sustain_sec": c["sustain_sec"],
    }
