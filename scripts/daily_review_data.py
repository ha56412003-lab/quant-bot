"""交易日报 · 数据采集器

职责边界：**只出事实，不做判断**。判断交给 AI（cron 任务里做复盘分析）。

这样拆分的好处：
  - 数据口径固定在一处，复盘结论天天可比
  - 采集逻辑改起来不怕影响分析质量
  - 可以直接人工跑，看原始数据

口径：一天 = 本地 00:00 起（与 `db.day_start_equity()`、日内熔断一致）。
     系统虽然按 UTC 记账，但熔断/日目标都用本地 00:00，报告必须跟它们对齐，
     否则会出现「报告说今天亏 2%、风控说亏 3.1%」的错位。

用法：
    python scripts/daily_review_data.py            # 打今天
    python scripts/daily_review_data.py --days 3   # 最近 3 天汇总
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "quantbot.db")


def _conn():
    import sqlite3
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _f(x, d=2, sign=False):
    if x is None:
        return "—"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def _hm(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "—"


def _dur(a, b):
    if not a or not b:
        return "—"
    m = int((b - a) / 60)
    return f"{m//60}h{m%60:02d}m" if m >= 60 else f"{m}m"


def collect(start_ts: int, label: str) -> str:
    c = _conn()
    L: list[str] = []

    # ── 净值 ────────────────────────────────────────────────
    eq0 = c.execute(
        "SELECT equity, balance, ts FROM equity_curve WHERE ts>=? ORDER BY ts LIMIT 1",
        (start_ts,)).fetchone()
    eq1 = c.execute(
        "SELECT equity, balance, ts FROM equity_curve ORDER BY ts DESC LIMIT 1").fetchone()
    if eq0 and eq1:
        e0, e1 = float(eq0["equity"]), float(eq1["equity"])
        change = e1 - e0
        pct = change / e0 * 100 if e0 else 0
        L.append(f"## 净值")
        L.append(f"- 期初 {e0:.2f}U  →  最新 {e1:.2f}U   变化 {change:+.2f}U ({pct:+.2f}%)")
        L.append(f"- 余额 {float(eq1['balance']):.2f}U   浮动盈亏 {float(eq1['equity'])-float(eq1['balance']):+.2f}U")
    else:
        L.append("## 净值\n- 数据不足")

    # ── 平仓交易 ────────────────────────────────────────────
    closes = c.execute(
        """SELECT t.ts, t.symbol, t.kind, t.pnl, t.price, t.qty, t.strategy, t.position_id,
                  p.side AS pside, p.opened_at, p.close_reason, p.status AS pstatus,
                  p.qty AS p_qty
           FROM trades t LEFT JOIN positions p ON p.id = t.position_id
           WHERE t.ts >= ? AND t.kind IN ('close','partial_close')
           ORDER BY t.ts""", (start_ts,)).fetchall()
    L.append("")
    L.append(f"## 平仓交易（{len(closes)} 笔成交）")
    if not closes:
        L.append("- 无平仓")
    else:
        wins = [r for r in closes if (r["pnl"] or 0) > 0]
        losses = [r for r in closes if (r["pnl"] or 0) <= 0]
        tot = sum(float(r["pnl"] or 0) for r in closes)
        # 注意：一个仓位可以被多次部分平仓，成交笔数 ≠ 独立仓位笔数。
        # 2026-11-05 事故：POL 部分平仓被 AI 当成"第二笔独立亏损"，
        # 进而推出"同一币种同方向连吃两次亏，判断系统性偏了"的错误结论。
        n_pos = len({r["position_id"] for r in closes if r["position_id"] is not None})
        L.append(f"- 合计 {tot:+.2f}U   胜 {len(wins)} / 负 {len(losses)}   "
                 f"胜率 {len(wins)/len(closes)*100:.0f}%")
        if n_pos and n_pos != len(closes):
            L.append(f"- ⚠ 涉及 **{n_pos} 个独立仓位**，但成交 {len(closes)} 笔 —— "
                     f"差额是**部分平仓**：同一仓位减仓，不是新开的一笔。")
        L.append("")
        L.append("| 时间 | 币种 | 方向 | 盈亏 | 类型 | 平仓原因 | 持仓时长 | 策略 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in closes:
            kind = "部分平仓(减仓)" if r["kind"] == "partial_close" else "全部平掉"
            L.append(f"| {_hm(r['ts'])} | {r['symbol']} | {r['pside'] or '?'} | "
                     f"{_f(r['pnl'],2,True)}U | {kind} | {r['close_reason'] or '—'} | "
                     f"{_dur(r['opened_at'], r['ts'])} | {r['strategy'] or '—'} |")
        # 部分平仓的剩余仓位说明
        for r in closes:
            if r["kind"] == "partial_close":
                # positions.qty 在部分平仓后**已被更新为剩余量**，不要再减
                remain = float(r["p_qty"] or 0)
                status = "仍在持仓" if r["pstatus"] == "open" else r["pstatus"]
                L.append(f"- 说明：{r['symbol']} 是**部分平仓**，砍掉 {float(r['qty'] or 0):.2f}，"
                         f"剩余 {remain:.2f}，仓位{status}（同一仓位，非新增亏损笔数）")

    # ── 开仓交易 ────────────────────────────────────────────
    opens = c.execute(
        """SELECT ts, symbol, side, price, qty, strategy
           FROM trades WHERE ts >= ? AND kind='open' ORDER BY ts""",
        (start_ts,)).fetchall()
    L.append("")
    L.append(f"## 开仓（{len(opens)} 笔）")
    if not opens:
        L.append("- 无开仓")
    else:
        for r in opens:
            notional = float(r["price"] or 0) * float(r["qty"] or 0)
            L.append(f"- {_hm(r['ts'])}  {r['symbol']:12} {r['side']:5} "
                     f"@{_f(r['price'],6)}  名义 {notional:.0f}U  {r['strategy'] or ''}")

    # ── 信号漏斗 ────────────────────────────────────────────
    sig = c.execute(
        "SELECT side, acted, skip_reason FROM signals WHERE ts >= ?", (start_ts,)
    ).fetchall()
    L.append("")
    L.append(f"## 信号漏斗（{len(sig)} 条）")
    if sig:
        acted = [r for r in sig if r["acted"]]
        skipped = [r for r in sig if not r["acted"]]
        L.append(f"- 产生 {len(sig)} → 真开仓 {len(acted)} → 拦截 {len(skipped)}"
                 f"（拦截率 {len(skipped)/len(sig)*100:.0f}%）")
        reasons: dict[str, int] = {}
        for r in skipped:
            k = (r["skip_reason"] or "未注明").split(":")[0][:20]
            reasons[k] = reasons.get(k, 0) + 1
        L.append("- 拦截原因 Top5：")
        for k, n in sorted(reasons.items(), key=lambda x: -x[1])[:5]:
            L.append(f"  - {k}：{n} 次（{n/len(skipped)*100:.0f}%）")
        # 按方向
        s_long = sum(1 for r in sig if r["side"] == "long")
        s_short = sum(1 for r in sig if r["side"] == "short")
        L.append(f"- 方向：多头 {s_long} / 空头 {s_short}")

    # ── 当前持仓 ────────────────────────────────────────────
    try:
        from backend import db as qdb, factors
        qdb.init()
        pos = qdb.open_positions()

        # 余额：名义杠杆的分母，必须脚本算好给 AI，否则它会自己估（曾算出 6.5x，实际 0.72x）
        _bal = 0.0
        try:
            _r = c.execute("SELECT balance FROM equity_curve ORDER BY ts DESC LIMIT 1").fetchone()
            _bal = float(_r["balance"]) if _r and _r["balance"] else 0.0
        except Exception:  # noqa: BLE001
            pass

        def _px(sym):
            try:
                return float(factors.mark_price_of(sym) or 0)
            except Exception:  # noqa: BLE001
                return 0.0

        def _dist(a, b):
            """a 到 b 的百分比距离（分母 a）。"""
            return abs(a - b) / a * 100 if a and b else 0.0

        L.append("")
        L.append(f"## 当前持仓（{len(pos)} 个）")
        if not pos:
            L.append("- 空仓")
        else:
            L.append("| 币种 | 方向 | 开仓价 | 现价 | 名义 | 浮盈亏 | 持仓时长 | "
                     "**开仓止损距离** | 现止损距离 |")
            L.append("|---|---|---|---|---|---|---|---|---|")
            tot_notional = 0.0
            tot_upnl = 0.0
            for p in sorted(pos, key=lambda x: x["opened_at"]):
                px = _px(p["symbol"])
                e = float(p["entry_price"] or 0)
                q = float(p["qty"] or 0)
                stop = float(p["stop_price"] or 0)
                up = (px - e) * q if p["side"] == "long" else (e - px) * q
                notional = q * px
                tot_notional += notional
                tot_upnl += up
                # 开仓止损距离 = 开仓那一刻到止损的百分比。
                # 必须单独给：AI 看到"现止损距离 1.31%"会误当成"开仓时就这么近"，
                # 进而建议"止损距离<2%的不开"（POL 开仓时其实是 4.45%）。
                L.append(
                    f"| {p['symbol']} | {p['side']} | {_f(e,6)} | {_f(px,6)} | "
                    f"{notional:.0f}U | {_f(up,2,True)}U | "
                    f"{_dur(p['opened_at'], dt.datetime.now().timestamp())} | "
                    f"{_dist(e, stop):.2f}% | {_dist(px, stop):.2f}% |"
                )
            L.append(f"\n- 合计浮动 **{tot_upnl:+.2f}U**")
            if _bal:
                L.append(f"- 合计名义 **{tot_notional:.0f}U**   余额 {_bal:.2f}U   "
                         f"**名义杠杆 {tot_notional/_bal:.2f}x**")
            n_long = sum(1 for p in pos if p["side"] == "long")
            L.append(f"- 方向分布：多头 {n_long} / 空头 {len(pos)-n_long}")
            L.append("- 注：**开仓止损距离**是入场那一刻的风险宽度；"
                     "**现止损距离**是价格走动后的剩余空间。两者含义不同，不要混用。")
    except Exception as exc:  # noqa: BLE001
        L.append(f"\n## 当前持仓\n- 读取失败: {exc}")

    # ── 影子信号（策略本身有没有 alpha）────────────────────
    sh = c.execute(
        "SELECT side, outcome, outcome_r FROM shadow_signals "
        "WHERE settled_ts >= ? AND outcome_r IS NOT NULL", (start_ts,)).fetchall()
    L.append("")
    L.append(f"## 影子信号结算（{len(sh)} 笔）")
    if sh:
        for side, name in (("long", "多头"), ("short", "空头")):
            sub = [float(r["outcome_r"]) for r in sh if r["side"] == side]
            if not sub:
                continue
            w = [x for x in sub if x > 0]
            L.append(f"- {name}：{len(sub)} 笔  胜率 {len(w)/len(sub)*100:.0f}%  "
                     f"期望 {statistics.mean(sub):+.3f}R  合计 {sum(sub):+.1f}R")
        stops = sum(1 for r in sh if r["outcome"] == "stop")
        tps = sum(1 for r in sh if r["outcome"] == "tp")
        L.append(f"- 止损 {stops} / 止盈 {tps}")
    else:
        L.append("- 无新结算")

    # ── 异常与健康 ──────────────────────────────────────────
    L.append("")
    L.append("## 异常检查")
    issues = []
    big = [r for r in closes if abs(float(r["pnl"] or 0)) > 60]
    for r in big:
        issues.append(f"单笔大额：{r['symbol']} {float(r['pnl']):+.2f}U（{r['close_reason']}）")
    errs = c.execute(
        "SELECT count(*) FROM logs WHERE ts>=? AND level IN ('ERROR','warning')",
        (start_ts,)).fetchone()[0]
    if errs:
        issues.append(f"日志 warning/ERROR {errs} 条")
    if eq0 and eq1:
        chg = (float(eq1["equity"]) - float(eq0["equity"])) / float(eq0["equity"]) * 100
        if chg < -2:
            issues.append(f"净值单日跌幅 {chg:.2f}%，接近熔断线 3%")
    L.extend(f"- {i}" for i in issues) if issues else L.append("- 无异常")

    # ── 今日配置变更（关键！否则 AI 会把改参数前后的数据混算）──
    L.append("")
    L.append("## 今日配置变更")
    tries = [
        ("SELECT ts, msg FROM logs WHERE ts>=? AND msg LIKE '%风控参数更新%' ORDER BY ts", (start_ts,)),
        ("SELECT ts, msg FROM logs WHERE ts>=? AND msg LIKE '%全局风控%' ORDER BY ts", (start_ts,)),
    ]
    changes = []
    for q, p in tries:
        try:
            changes = c.execute(q, p).fetchall()
        except Exception:  # noqa: BLE001
            changes = []
        if changes:
            break
    if changes:
        L.append("⚠ **今天中途改过风控参数，改之前和改之后的数据不能混在一起算！**")
        L.append(f"- 今日共有 {len(changes)} 条参数变更记录，时间点：")
        for r in changes:
            ts = r["ts"] if hasattr(r, "keys") else r[0]
            msg = r["msg"] if hasattr(r, "keys") else r[1]
            # 整条消息可能很长（全量写入），只截关键字段
            brief = msg if len(msg) <= 60 else msg[:57] + "..."
            L.append(f"  - {_hm(ts)}  {brief}")
        first = changes[0]
        L.append(f"- **最早变更时间：{_hm(first['ts'] if hasattr(first,'keys') else first[0])}**，"
                 f"在此之前的数据全部按旧参数产生")
        L.append("- 分析拦截统计时，请把时间切成「变更前 / 变更后」两段分别看")
        L.append("- **不要数变更条数当分析结论** —— 其中可能包含测试写入。"
                 "真正要关注的是「变更时间点」和「最终生效值」")
    else:
        L.append("- 无变更")

    # ── 当前生效的风控参数（给 AI 一个确定的事实基线）──
    L.append("")
    L.append("## 当前生效风控参数")
    try:
        import yaml as _yaml
        _p = os.path.join(ROOT, "strategies.yaml")
        with open(_p, encoding="utf-8") as fh:
            _g = (_yaml.safe_load(fh) or {}).get("global", {}) or {}
        _cc = _g.get("max_concurrent_coins")
        _ms = _g.get("max_same_side")
        _rp = _g.get("risk_per_trade_pct")
        _dh = _g.get("daily_loss_halt_pct")
        L.append(f"- 并发上限 max_concurrent_coins = **{_cc}**")
        L.append(f"- 单边上限 max_same_side = **{_ms}**")
        L.append(f"- 每单风险 risk_per_trade_pct = **{_rp}%**")
        L.append(f"- 日内熔断 daily_loss_halt_pct = **{_dh}%**")
        if _ms and _rp:
            worst = float(_ms) * float(_rp)
            flag = "（**正好等于熔断线，同向全止损当天即熔断**）" if _dh and abs(worst - float(_dh)) < 1e-9 else ""
            L.append(f"- 由此推出：单边 {_ms} 单全止损 = {_ms} × {_rp}% = **{worst:.2f}%**{flag}")
    except Exception as exc:  # noqa: BLE001
        L.append(f"- 读取失败: {exc}")

    return f"# 交易日报数据 · {label}\n\n" + "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    args = ap.parse_args()

    now = dt.datetime.now()
    if args.days == 1:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = now.strftime("%Y-%m-%d（本地 00:00 起）")
    else:
        start = (now - dt.timedelta(days=args.days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        label = f"最近 {args.days} 天（{start:%m-%d} 起）"

    print(collect(int(start.timestamp()), label))


if __name__ == "__main__":
    main()
