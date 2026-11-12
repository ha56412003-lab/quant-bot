"""日目标（落袋为安）—— 当日盈利达到目标时提醒 + 超时自动全平 + 停止开新仓。

状态机：
    idle     → 未达标
    armed    → 已达标，观察中（要求持续 sustain_sec 才算真达标，防止插针误触）
    alerted  → 已提醒（发飞书 + 前端横幅），等待人工平仓
    done     → 已处理完（人工平了 / 自动平了），当天不再开新仓

全部行为由 global.daily_target 配置控制，enabled=false 即完全关闭。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
import urllib.request

from . import config, db, netwatch

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
        if k not in out or v is None:
            continue
        d = DEFAULTS[k]
        try:
            if isinstance(d, bool):
                # ⚠ bool("false") == True。前端/手拼 JSON 传字符串时，
                # 以前会把 daily_target.enabled 意外打开（连带自动全平），
                # 用户以为"关了"其实开着。
                out[k] = v if isinstance(v, bool) else \
                    str(v).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(d, float):
                out[k] = float(v)
            else:
                out[k] = int(float(v))
        except (TypeError, ValueError):
            # 以前非法值直接抛进 engine 的除错分支被 log.debug 吞掉，
            # 表现为"日目标整天不工作且界面无任何提示"。现在留痕 + 退回默认。
            log.warning("daily_target 配置项 %s=%r 非法，改用默认 %r", k, v, d)
            out[k] = d

    # 2026-11-11：本地/服务器身份分离。
    # strategies.yaml 是两边共用的配置文件（走 git 同步），所以不能靠改 yaml
    # 来让"本地关、服务器开"——一同步就互相覆盖。改用环境变量覆盖：
    #   本地 run_server.bat 设 QB_DAILY_TARGET_ENABLED=0 → 本地日目标关闭（测试盘）
    #   服务器不设 → 保持 yaml 里的 true（正式盘，日目标生效）
    _env = (os.environ.get("QB_DAILY_TARGET_ENABLED") or "").strip().lower()
    if _env in ("0", "false", "no", "off"):
        if out.get("enabled"):
            log.info("daily_target 已被环境变量 QB_DAILY_TARGET_ENABLED=%s 关闭（测试盘）", _env)
        out["enabled"] = False
    elif _env in ("1", "true", "yes", "on"):
        out["enabled"] = True

    return out


def _today() -> str:
    return datetime.date.today().isoformat()


def _g(k, default=None):
    v = db.get_meta(S + k, "")
    return v if v != "" else default


def _s(k, v):
    db.set_meta(S + k, str(v))


def reset(skip_today: bool = True):
    """手动重置日目标。

    skip_today=True（默认）：清掉状态、解除开仓封锁，并且**当天不再自动触发**。
        这是「今日重置」的正确语义 —— 用户平完想继续做，但不想再被全平一次。
        （2026-11-05 事故：只清状态不清 skip 标记时，若净值仍 ≥ 目标，
         下一轮 tick 会立刻重新 armed → 再全平一遍，用户以为按钮坏了。）
    skip_today=False：只清状态，达标会重新触发。
    """
    for k in ("state", "hit_ts", "alert_ts", "day", "pnl_at_alert"):
        _s(k, "")
    if skip_today:
        _s("skip_day", _today())
        log.info("日目标已重置：今天（%s）不再自动触发", _today())
    else:
        _s("skip_day", "")
        log.info("日目标已重置（未设置今日跳过）")


def skipped_today() -> bool:
    """今天是否已被手动重置（不再自动触发日目标）。"""
    return _g("skip_day", "") == _today()


def state() -> dict:
    return {
        "state": _g("state", "idle"),
        "hit_ts": int(_g("hit_ts", 0) or 0),
        "alert_ts": int(_g("alert_ts", 0) or 0),
        "day": _g("day", ""),
        "pnl_at_alert": float(_g("pnl_at_alert", 0) or 0),
    }


def blocked() -> bool:
    """是否应阻止开新仓（达到日目标后）。

    ⚠ 2026-11-06 修：`pause_new` 这个开关以前是死的 —— DEFAULTS 里有、
    前端能写、全仓库没人读，用户在前端关掉"达标后暂停开新仓"行为完全不变。
    现在真正读它：pause_new=False 时只提醒不拦单。
    """
    st = state()
    if st["day"] != _today():
        return False
    if st["state"] not in ("alerted", "done"):
        return False
    try:
        c = cfg(config.load_strategies().get("global", {}))
        return bool(c.get("pause_new", True))
    except Exception:  # noqa: BLE001
        return True          # 读配置失败时保持原行为（安全默认：拦）


# ── 飞书推送 ────────────────────────────────────────────────
# 2026-11-12 浩哥定：本地不再用，告警 / 日目标提醒**只从服务器端发**。
# 判定逻辑统一在 netwatch.feishu_targets()：Windows 不发 + 必须 QB_ALERT_PUSH=1
# + 凭据从环境变量（项目 .env 启动时已灌进 os.environ）读。
# 旧实现这里硬编码读 E:\Hermes\.env —— 服务器上读不到，导致线上日目标提醒一直是哑的。


def _push_sync(text: str) -> bool:
    """真正干活的同步实现（urllib 阻塞，最长 10s × 接收人数）。"""
    t = netwatch.feishu_targets()
    if not t:
        log.warning("日目标提醒未启用（本地不发 / 服务器需 QB_ALERT_PUSH=1 + 飞书凭据），跳过推送")
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


async def _push(text: str) -> bool:
    """异步包装。

    ⚠ 2026-11-06 修：原来 _push 直接在 tick()（async）里同步调用，
    urllib 的 timeout=10 会卡死整个 asyncio 事件循环 —— 偏偏是在"日目标达成、
    有大额浮盈、正需要盯止损"的时刻，position_loop 的止损/止盈整整停摆 10~N 秒。
    现在丢到线程池执行，事件循环照常跑。
    """
    return await asyncio.to_thread(_push_sync, text)


# ── 主循环 ──────────────────────────────────────────────────

async def tick(equity: float, day_start_equity: float, close_all_cb, c: dict):
    """每轮调用。close_all_cb() 用于自动全平。"""
    if not c["enabled"]:
        if state()["state"] not in ("", "idle"):
            reset(skip_today=False)   # 关掉开关不算「今日跳过」
        return

    today = _today()

    # 今天已被「今日重置」过 → 不再自动触发（但仍允许开仓）
    if skipped_today():
        st0 = state()
        if st0["state"] not in ("", "idle"):
            reset(skip_today=True)    # 保证状态干净
        return

    st = state()
    if st["day"] != today:
        reset(skip_today=False)       # 换天：清状态，但不要误设 skip 标记
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
                # 原来是 auto_close_sec // 60，配成 5 秒时算出 0 → 提示整句消失，
                # 用户看到"请确认一键全平"，5 秒后仓位已经被自动平掉了。
                _acs = int(c["auto_close_sec"] or 0)
                if _acs >= 60:
                    when = f"{_acs // 60} 分钟"
                elif _acs > 0:
                    when = f"{_acs} 秒"
                else:
                    when = ""
                tail = f"\n{when}内未平仓将自动全平。" if when else ""
                await _push("【量化系统 · 日目标达成】\n"
                      f"今日盈亏 +${pnl:.2f}（目标 ${target:.0f}）\n"
                      f"当前净值 ${equity:.2f}\n"
                      f"已提醒落袋：请打开控制台确认一键全平。{tail}")
        return

    if cur == "alerted":
        # 已无持仓 → 视为已落袋（手动平了 / 自动平完），立刻转 done
        # 这一步必须做，否则 auto_close_sec=0（只提醒不自动平）时状态永远停在
        # alerted，而 blocked() 认 alerted → 当天再也开不了仓。
        if not has_open_positions():
            _s("state", "done")
            log.info("日目标：已无持仓，视为落袋完成（%.2f）", pnl)
            if c["notify"]:
                await _push(f"【量化系统】日目标已落袋。\n今日盈亏 +${pnl:.2f}")
            return
        if c["auto_close_sec"] and now - st["alert_ts"] >= c["auto_close_sec"]:
            log.warning("日目标提醒超时 %.0f 分钟，自动全平", (now - st["alert_ts"]) / 60)
            try:
                closed, failed = await close_all_cb()
            except Exception as exc:  # noqa: BLE001
                log.warning("自动全平异常: %s", exc)
                return
            if failed:
                # ⚠ 2026-11-06 修：以前完全不看 close_all 的返回值。close_all 从不
                # 抛异常，失败只体现在返回值里 → 仓位还在，状态却已置 done，
                # blocked() 当天封死开仓，飞书还谎报"已全平落袋"，仓位实际裸奔。
                #
                # ⚠ 同轮补丁：第一版只是简单 _s("alert_ts", now) 重试，遇到
                # 「这个币永远拿不到价 → 永远平不掉」会每 auto_close_sec 秒
                # 全平一轮 + 推一条飞书，无限循环（前端最小能填 5 秒）。
                # 现在按次数指数退避，且只在第 1/3/10 次推送。
                n = int(st.get("autoclose_fail_n") or 0) + 1
                _s("autoclose_fail_n", n)
                backoff = min(60 * n, 1800)          # 1,2,3… 分钟，封顶 30 分钟
                _s("alert_ts", now + backoff)        # 推迟到 backoff 秒后再试
                log.error("日目标自动全平未全部成功（第 %d 次）：成功 %s 失败 %s，"
                          "%d 秒后重试", n, closed, failed, backoff)
                if c["notify"] and n in (1, 3, 10):
                    await _push(f"【量化系统】日目标自动全平部分失败："
                                f"成功 {closed} 笔 / 失败 {failed} 笔，"
                                f"系统将于 {backoff // 60} 分钟后重试（第 {n} 次）。"
                                f"若持续失败请手动处理。")
                return
            _s("state", "done")
            if c["notify"]:
                await _push(f"【量化系统】日目标提醒超时，已自动全平落袋。\n今日盈亏 +${pnl:.2f}")
        return


def has_open_positions() -> bool:
    """当前（活跃模式）是否还有持仓。查不到时保守当作「还有」。"""
    try:
        return bool(db.open_positions())
    except Exception as exc:  # noqa: BLE001
        log.warning("持仓查询失败，按「仍有持仓」处理: %s", exc)
        return True


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
        # hit = 「今天真的赚到目标了」。
        # 以前只看 state in (alerted, done) —— 手动全平误设 done 时，
        # 当日亏 76 U 也照样 hit=true，界面上的数字和标签自相矛盾。
        # state 是流程状态，hit 是对账事实，两者必须分开算。
        "hit": bool(st["state"] in ("alerted", "done")
                    and st["day"] == _today()
                    and pnl >= target),
        "blocked": blocked(),
        "auto_close_left": left,
        "sustain_sec": c["sustain_sec"],
        "skipped_today": skipped_today(),
    }
