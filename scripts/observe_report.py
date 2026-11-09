"""观察期成绩单 —— 策略自动 vs 人工干预，分开算。

为什么要有这个脚本：
    浩哥会手动平仓（这是他的钱、他的权利）。但如果把人工平仓的盈亏
    混进策略成绩里，一周后复盘会得出完全错误的结论：
      · 策略明明不行，靠人工捞回来了 → 看起来「策略还行」
      · 策略本来能赚，被人工提前平了 → 看起来「策略不行」
    所以观察期必须两条线分开记。

分类依据（按代码里的实际调用链，不是猜的）：
    策略自动：
        止损触发        engine 扫到 stop_price 被破
        止盈触发        engine 扫到 take_profit
        一键全平        daily_target.tick 传入 executor.close_all 的默认 reason
                        （日目标达成超时自动全平 —— 是系统行为，不是人点的）
    人工干预：
        手动全平        界面上点单仓平仓
        手动一键全平    /api/positions/close-all（main.py:375 明确传此 reason）
        手动市价平仓    其他手动入口

用法：
    python scripts/observe_report.py                    # 用默认观察期起点
    python scripts/observe_report.py 2026-11-06 18:20 10072.91
"""
import sqlite3
import sys
import os
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import config  # noqa: E402

# ── 观察期参数 ────────────────────────────────────────────────────
# ★ 2026-11-09：起点统一从 backend/config.py 的 SAMPLE_START 读，
#   和影子模式统计同一个下界，不再两处各写一个日期（以前就是这样漂的）。
#   命令行仍可覆盖：python scripts/observe_report.py 2026-11-09 12:00 10219.67
START = config.SAMPLE_START or dt.datetime(2026, 11, 9, 12, 0)
BASE_EQUITY = None          # None = 自动取起点后第一条 equity_curve
if len(sys.argv) >= 4:
    d, hm = sys.argv[1], sys.argv[2]
    START = dt.datetime.strptime(f"{d} {hm}", "%Y-%m-%d %H:%M")
    BASE_EQUITY = float(sys.argv[3])

# ── 分类表 ────────────────────────────────────────────────────────
AUTO = {"止损触发", "止盈触发", "一键全平", "时间止损", "信号反转", "平一半"}
MANUAL = {"手动全平", "手动一键全平", "手动市价平仓"}


def classify(reason):
    r = (reason or "").strip()
    if r in AUTO:
        return "auto"
    if r in MANUAL:
        return "manual"
    return "unknown"


def r_multiple(row):
    """1R = |开仓价 - 止损价| × 数量。返回这笔赚了几个 R。"""
    e, s, q = row["entry_price"], row["stop_price"], row["qty"]
    pnl = row["pnl"] or 0.0
    if not e or not s or not q:
        return None
    risk = abs(e - s) * abs(q)
    return pnl / risk if risk > 0 else None


def stats(rows):
    n = len(rows)
    if not n:
        return {"n": 0, "pnl": 0.0, "win": 0, "wr": 0.0, "avg": 0.0, "r": 0.0, "r_n": 0}
    pnl = sum(r["pnl"] or 0 for r in rows)
    win = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    rs = [x for x in (r_multiple(r) for r in rows) if x is not None]
    return {
        "n": n, "pnl": pnl, "win": win, "wr": win / n * 100,
        "avg": pnl / n,
        "r": sum(rs) if rs else 0.0, "r_n": len(rs),
    }


def main():
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    lo = START.timestamp()

    closed = c.execute(
        "SELECT * FROM positions WHERE status='closed' AND closed_at >= ? ORDER BY closed_at",
        (lo,)).fetchall()
    opens = c.execute("SELECT * FROM positions WHERE status='open'").fetchall()

    auto = [r for r in closed if classify(r["close_reason"]) == "auto"]
    manual = [r for r in closed if classify(r["close_reason"]) == "manual"]
    unknown = [r for r in closed if classify(r["close_reason"]) == "unknown"]

    # 浮盈（未平仓按实时价估）—— 实时价在服务进程的 ws 内存里，
    # 独立脚本拿不到，必须走 API；服务没起就跳过（净资产显示为已实现）
    upnl = 0.0
    upnl_ok = 0
    api_note = ""
    try:
        import json as _json
        import urllib.request as _ur
        with _ur.urlopen("http://127.0.0.1:8899/api/positions", timeout=5) as _r:
            _ps = _json.load(_r).get("positions", [])
        for p in _ps:
            upnl += float(p.get("unrealized_pnl") or 0)
            upnl_ok += 1
    except Exception as exc:
        api_note = f"（服务未响应，浮盈未计入：{type(exc).__name__}）"

    # ★ 2026-11-09：BASE_EQUITY 默认 None = 自动取「起点之后第一条净值」。
    #   手工填数字容易填错、也会忘了改；直接从 equity_curve 取最准。
    base = BASE_EQUITY
    base_src = "手工指定"
    if base is None:
        row0 = c.execute(
            "SELECT equity FROM equity_curve WHERE ts >= ? ORDER BY ts LIMIT 1",
            (lo,)).fetchone()
        base = float(row0["equity"]) if row0 else 0.0
        base_src = "起点首条净值" if row0 else "无数据"

    equity = base + sum(r["pnl"] or 0 for r in closed) + upnl
    equity_src = "基线+已实现+浮盈"
    # 净值以 API 为准：paper_balance 与流水有历史误差（已知 +9.87U 量级），
    # 自算会和界面数字打架，复盘时数字不一致最容易吵起来
    try:
        import json as _json2
        import urllib.request as _ur2
        with _ur2.urlopen("http://127.0.0.1:8899/api/status", timeout=5) as _r2:
            equity = float(_json2.load(_r2)["equity"])
        equity_src = "API"
    except Exception:
        pass

    W = 78
    print("═" * W)
    print(" 观察期成绩单  ——  策略自动 / 人工干预 分开算")
    print("═" * W)
    print(f"  起点     {START:%Y-%m-%d %H:%M}")
    print(f"  基线     {base:,.2f} U   （{base_src}）")
    print(f"  已过     {(dt.datetime.now() - START).total_seconds() / 3600:.1f} 小时")
    print(f"  净值     {equity:,.2f} U   （基线 {'%+.2f' % (equity - base)} U，来源 {equity_src}）")
    print(f"  浮盈     {upnl:+,.2f} U  ({upnl_ok}/{len(opens)} 笔取到实时价){api_note}")

    print()
    print("─" * W)
    print(f"  {'':<10}{'笔数':>6}{'净盈亏':>13}{'每笔':>11}{'胜率':>9}{'净R':>10}")
    print("─" * W)
    for label, rows in (("策略自动", auto), ("人工干预", manual)):
        s = stats(rows)
        if not s["n"]:
            print(f"  {label:<10}{0:>6}{'—':>13}{'—':>11}{'—':>9}{'—':>10}")
            continue
        rtxt = f"{s['r']:+.2f}" if s["r_n"] else "—"
        print(f"  {label:<10}{s['n']:>6}{s['pnl']:>+13,.2f}{s['avg']:>+11,.2f}"
              f"{s['wr']:>8.1f}%{rtxt:>10}")
    if unknown:
        print(f"  {'(未识别)':<10}{len(unknown):>6}   close_reason = "
              f"{sorted({r['close_reason'] for r in unknown})}")
    print("─" * W)

    # 干净策略成绩：只算策略自己平的
    s = stats(auto)
    print()
    print("  ★ 干净策略成绩（剔除人工干预）")
    print(f"      {s['n']} 笔 · {'%+.2f' % s['pnl']} U · 净 {s['r']:+.2f} R · 胜率 {s['wr']:.1f}%")
    print()
    print("  ⚠ 含人工干预合计")
    tot = s["pnl"] + stats(manual)["pnl"]
    print(f"      {len(closed)} 笔 · {'%+.2f' % tot} U")

    # ── 验收标准进度 ──
    print()
    print("═" * W)
    end = START + dt.timedelta(days=7)
    print(f" 验收标准（观察期至 {end:%Y-%m-%d %H:%M}，满 7 天）")
    print("═" * W)
    checks = [
        ("交易笔数 ≥ 15", s["n"], s["n"] >= 15, "笔"),
        ("净 R ≥ 0", round(s["r"], 2), s["r"] >= 0, "R"),
        ("胜率 ≥ 38%", round(s["wr"], 1), s["wr"] >= 38, "%"),
    ]
    for name, val, ok, unit in checks:
        mark = "✅" if ok else "⏳"
        print(f"  {mark} {name:<18} 现在 {val}{unit}")
    print()
    print("  硬熔断（碰到任一条立即停）：")
    print(f"    净值跌破 7,000 U      现在 {equity:,.0f} U  {'🔴 已触发' if equity < 7000 else '正常'}")
    print("    连续亏损 10 笔        需逐笔算")
    print("    单笔风险超 1.2%       需逐笔算")
    print("    池外币被开仓 1 次     需查日志")

    if manual:
        print()
        print("═" * W)
        print(" 人工干预明细（这些笔不算策略成绩，复盘时单独看）")
        print("═" * W)
        for r in manual:
            t = dt.datetime.fromtimestamp(r["closed_at"]).strftime("%m-%d %H:%M")
            rr = r_multiple(r)
            rtxt = f"{rr:+.2f}R" if rr is not None else "—"
            print(f"  {t}  {r['symbol']:<9}{r['side']:<6}{(r['pnl'] or 0):>+9.2f}U"
                  f"  {rtxt:>9}  {r['close_reason']}")

    print()
    print("═" * W)


main()
