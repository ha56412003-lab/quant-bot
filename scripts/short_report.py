"""空头策略验证日报 —— 每天统计一次多/空两个方向的实际表现，推飞书。

背景（2026-11-05）：影子系统 258 笔已结算里，空头 60 笔胜率 0.0%，
多头 198 笔胜率 83.8%。经逐笔 K 线回放确认不是结算 bug，是 11-04 市场暴涨所致。
样本只有 1.7 天，需要持续观察才能判断是"行情问题"还是"策略逻辑问题"。

因此每天固定统计三组数据：
  ① 影子系统（样本大，覆盖所有信号，含未开仓的）—— 看策略本身有没有 alpha
  ② 模拟盘实际平仓（样本小，但含滑点/风控拦截的真实结果）—— 看落地效果
  ③ 当前在持仓位（多空分布 + 浮动盈亏）

用法：
    python scripts/short_report.py           # 打印到屏幕
    python scripts/short_report.py --push    # 打印并推飞书
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "quantbot.db")
STATE = os.path.join(ROOT, "data", "short_watch_state.json")


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _pct(x: float, digits: int = 1) -> str:
    return f"{x:+.{digits}f}%" if x else "—"


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _stats(rows, side):
    sub = [float(r["outcome_r"]) for r in rows if r["side"] == side]
    if not sub:
        return None
    win = [x for x in sub if x > 0]
    return len(sub), len(win) / len(sub) * 100, statistics.mean(sub), sum(sub)


def build_report() -> str:
    c = _conn()
    now = datetime.datetime.now()
    st = _load_state()
    baseline_ts = int(st.get("baseline_ts") or 0)

    L: list[str] = []
    L.append(f"📊 空头验证日报 · {now:%m-%d %H:%M}")
    L.append("")

    # ── ① 影子系统（按方向）：观察期优先，无样本则回退到累计 ──
    q = ("SELECT side, outcome_r FROM shadow_signals "
         "WHERE outcome_r IS NOT NULL AND ts >= ?")
    recent = c.execute(q, (baseline_ts,)).fetchall()
    period = "本观察期" if recent else "累计（观察期暂无新样本）"
    rows = recent or c.execute(
        "SELECT side, outcome_r FROM shadow_signals WHERE outcome_r IS NOT NULL"
    ).fetchall()
    L.append(f"【影子系统 · {period}】")
    if not rows:
        L.append("  无已结算样本")
    else:
        for side, name in (("long", "多头"), ("short", "空头")):
            s = _stats(rows, side)
            if not s:
                L.append(f"  {name}：无样本")
                continue
            n, wr, exp, tot = s
            L.append(f"  {name}：{n} 笔  胜率 {wr:.1f}%  期望 {exp:+.3f}R  合计 {tot:+.1f}R")
        sh = _stats(rows, "short")
        if sh:
            n, wr, _exp, _tot = sh
            if wr == 0 and n >= 10:
                L.append(f"  ⚠ 空头 {n} 笔全亏，建议认真评估是否关闭该方向")
            elif wr < 30 and n >= 15:
                L.append(f"  ⚠ 空头胜率仅 {wr:.1f}%，低于盈亏平衡点（约 40%）")
    L.append("")

    # ── ② 模拟盘实际平仓 ───────────────────────────────────
    q2 = ("SELECT symbol, side, pnl, close_reason FROM positions "
          "WHERE status='closed' AND pnl IS NOT NULL AND closed_at >= ?")
    rec2 = c.execute(q2, (baseline_ts,)).fetchall()
    period2 = "本观察期" if rec2 else "累计"
    rows2 = rec2 or c.execute(
        "SELECT symbol, side, pnl, close_reason FROM positions "
        "WHERE status='closed' AND pnl IS NOT NULL"
    ).fetchall()
    L.append(f"【模拟盘 · 实际平仓 · {period2}】")
    if not rows2:
        L.append("  无平仓记录")
    else:
        for side, name in (("long", "多头"), ("short", "空头")):
            sub = [r for r in rows2 if r["side"] == side]
            if not sub:
                L.append(f"  {name}：无平仓")
                continue
            p = [float(r["pnl"]) for r in sub]
            win = [x for x in p if x > 0]
            L.append(f"  {name}：{len(sub)} 笔  胜率 {len(win)/len(sub)*100:.1f}%  合计 {sum(p):+.2f}U")
        sh2 = [r for r in rows2 if r["side"] == "short"]
        if sh2:
            L.append("  空头最近平仓：")
            for r in sh2[-3:]:
                L.append(f"    {r['symbol']:12} {float(r['pnl']):+8.2f}U  {r['close_reason'] or ''}")
    L.append("")

    # ── ③ 当前持仓 ─────────────────────────────────────────
    try:
        from backend import db as qdb, factors
        qdb.init()
        pos = qdb.open_positions()

        def _upnl(p) -> float:
            """db 层不存浮动盈亏，按 (现价-开仓价)×数量 现算。"""
            try:
                px = float(factors.mark_price_of(p["symbol"]) or 0)
                entry = float(p["entry_price"] or 0)
                qty = float(p["qty"] or 0)
                if not px or not entry:
                    return 0.0
                return (px - entry) * qty if p["side"] == "long" else (entry - px) * qty
            except Exception:  # noqa: BLE001
                return 0.0

        n_long = sum(1 for p in pos if p["side"] == "long")
        n_short = sum(1 for p in pos if p["side"] == "short")
        upnl_l = sum(_upnl(p) for p in pos if p["side"] == "long")
        upnl_s = sum(_upnl(p) for p in pos if p["side"] == "short")
        L.append(f"【当前持仓】{len(pos)} 个（多 {n_long} / 空 {n_short}）")
        if n_long:
            L.append(f"  多头浮盈亏 {upnl_l:+.2f}U")
        if n_short:
            L.append(f"  空头浮盈亏 {upnl_s:+.2f}U")
            for p in [x for x in pos if x["side"] == "short"]:
                L.append(f"    {p['symbol']:12} 浮盈亏 {_upnl(p):+8.2f}U")
    except Exception as exc:  # noqa: BLE001
        L.append(f"【当前持仓】读取失败: {exc}")
    L.append("")

    # ── ④ 观察进度 ─────────────────────────────────────────
    if not baseline_ts:
        L.append("【观察】今天是第一天，基线已建立")
    else:
        days = (now.timestamp() - baseline_ts) / 86400
        L.append(f"【观察】已第 {days:.1f} 天（基线 {datetime.datetime.fromtimestamp(baseline_ts):%m-%d}）")
        if days < 3:
            L.append("  样本还太短，不下结论")
        elif days < 7:
            L.append("  继续观察，7 天后给出是否关闭空头的建议")
        else:
            L.append("  ⏰ 已满 7 天，建议做一次正式评估（看空头胜率是否稳定低于 40%）")

    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="推送到飞书")
    args = ap.parse_args()

    # 先出报告（首次运行 baseline_ts=0 → 统计全部历史），再落基线
    text = build_report()

    st = _load_state()
    if not st.get("baseline_ts"):
        st["baseline_ts"] = int(datetime.datetime.now().timestamp())
        st["baseline_note"] = "空头验证观察起点（2026-11-05 发现 60 笔空头 0% 胜率）"
        _save_state(st)
        text += f"\n\n（首次运行，已建立观察基线 {datetime.datetime.now():%m-%d %H:%M}）"

    print(text)

    if args.push:
        try:
            from backend.daily_target import _push
            ok = _push(text)
            print("\n→ 飞书推送:", "成功" if ok else "失败")
        except Exception as exc:  # noqa: BLE001
            print(f"\n→ 飞书推送异常: {exc}")


if __name__ == "__main__":
    main()
