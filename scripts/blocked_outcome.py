"""被拦信号的事后验证：拦下来的信号，后来涨了还是跌了？

回答 Q3「监控池 30 该不该扩」——光看拦截次数不够，必须知道拦得对不对。

做法：
  1. 从 signals 表按 (日期, 币种, 方向) 聚合被拦信号
  2. 拉该币当天的 1h K 线（24 根）
  3. 算 24h 净涨跌（按信号方向调整）、期间最大有利/不利偏移
  4. 写入 blocked_daily 表

流量：每天约 70~90 个币 × 1 次请求 × ~2KB ≈ 150-200 KB/天。
远小于 mark_all 的 69MB/天。

用法：
    python scripts/blocked_outcome.py            # 回填所有可回填的历史
    python scripts/blocked_outcome.py --days 3   # 只回填最近 3 天
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "quantbot.db")
FUT = "https://fapi.binance.com"
DAY = 86400

DDL = """
CREATE TABLE IF NOT EXISTS blocked_daily (
    day        TEXT    NOT NULL,   -- '2026-11-05'
    symbol     TEXT    NOT NULL,
    side       TEXT    NOT NULL,   -- long / short
    n          INTEGER NOT NULL,   -- 当天该币该方向被拦次数
    top_reason TEXT,               -- 最多的拦截原因
    open_price REAL,               -- 当天首根 1h 开盘价
    close_24h  REAL,               -- 24h 后收盘价
    ret_pct    REAL,               -- 按方向调整后的净收益 %
    mfe_pct    REAL,               -- 期间最大有利偏移 %
    mae_pct    REAL,               -- 期间最大不利偏移 %
    filled_ts  INTEGER,
    PRIMARY KEY (day, symbol, side)
);
"""


def day_bounds(day: str) -> tuple[int, int]:
    d = dt.datetime.strptime(day, "%Y-%m-%d")
    a = int(d.timestamp())
    return a, a + DAY


def fetch_1h(symbol: str, start: int, end: int) -> list[list]:
    """拉 [start, end) 的 1h K 线，返回 binance 原始数组。"""
    out: list[list] = []
    cur = start * 1000
    stop = end * 1000
    while cur < stop:
        try:
            r = httpx.get(f"{FUT}/fapi/v1/klines", params={
                "symbol": symbol, "interval": "1h",
                "startTime": cur, "endTime": stop, "limit": 1500,
            }, timeout=15)
            r.raise_for_status()
            batch = r.json()
        except Exception:  # noqa: BLE001
            return out
        if not batch:
            break
        out.extend(batch)
        nxt = int(batch[-1][0]) + 3600_000
        if nxt <= cur:
            break
        cur = nxt
        if len(batch) < 1500:
            break
    return out


def backfill(days: int | None = None) -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(DDL)

    # 找出所有「有被拦信号」的日期（以本地时区切天）
    rows = c.execute(
        "SELECT DISTINCT date(ts,'unixepoch','localtime') AS d FROM signals "
        "WHERE acted=0 AND ts > 0 ORDER BY d"
    ).fetchall()
    all_days = [r["d"] for r in rows if r["d"]]
    if days:
        all_days = all_days[-days:]

    done = {r[0] for r in c.execute("SELECT DISTINCT day FROM blocked_daily")}
    todo = [d for d in all_days if d not in done]
    if not todo:
        print("没有待回填的日期")
        return 0

    total = 0
    for day in todo:
        a, b = day_bounds(day)
        # 当天结束了吗？没结束就没法算 24h 收益
        if b > time.time():
            print(f"  {day}: 当天未结束，跳过")
            continue
        grp = c.execute(
            """SELECT symbol, side, count(*) n,
                      (SELECT skip_reason FROM signals s2
                        WHERE s2.symbol=s.symbol AND s2.side=s.side AND s2.acted=0
                          AND s2.ts>=? AND s2.ts<?
                        GROUP BY skip_reason ORDER BY count(*) DESC LIMIT 1) top_reason
               FROM signals s
               WHERE acted=0 AND ts>=? AND ts<?
               GROUP BY symbol, side""", (a, b, a, b)).fetchall()

        n_ok = 0
        for g in grp:
            ks = fetch_1h(g["symbol"], a, b)
            if len(ks) < 2:
                continue
            o = float(ks[0][1])
            cl = float(ks[-1][4])
            hi = max(float(k[2]) for k in ks)
            lo = min(float(k[3]) for k in ks)
            if o <= 0:
                continue
            if g["side"] == "long":
                ret = (cl / o - 1) * 100
                mfe = (hi / o - 1) * 100
                mae = (lo / o - 1) * 100
            else:  # short：价格跌才赚钱
                ret = (1 - cl / o) * 100
                mfe = (1 - lo / o) * 100
                mae = (1 - hi / o) * 100
            c.execute(
                "INSERT OR REPLACE INTO blocked_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (day, g["symbol"], g["side"], g["n"], g["top_reason"],
                 o, cl, round(ret, 4), round(mfe, 4), round(mae, 4), int(time.time())))
            n_ok += 1
            time.sleep(0.05)  # 别把接口打急

        c.commit()
        print(f"  {day}: {len(grp)} 组被拦 → 回填 {n_ok} 组")
        total += n_ok

    return total


def summarize(days: int = 7) -> str:
    """给日报用的汇总：被拦信号的事后表现。"""
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(DDL)
    except Exception:  # noqa: BLE001
        pass
    L = ["## 被拦信号事后验证（Q3：监控池准入拦得对不对）"]
    rows = c.execute(
        "SELECT day, sum(n) n, count(*) grp, "
        "       sum(case when ret_pct>0 then n else 0 end) win_n, "
        "       sum(case when ret_pct>0 then 1 else 0 end) win_grp, "
        "       avg(ret_pct) avg_ret "
        "FROM blocked_daily GROUP BY day ORDER BY day DESC LIMIT ?", (days,)).fetchall()
    if not rows:
        L.append("- **还没有回填数据**（blocked_outcome.py 尚未运行或历史不足 24h）")
        return "\n".join(L)

    L.append("| 日期 | 被拦次数 | 币种组 | 若开了会赚的比例 | 平均收益 |")
    L.append("|---|---|---|---|---|")
    tot_n = tot_win = 0
    for r in rows:
        pct = (r["win_n"] / r["n"] * 100) if r["n"] else 0
        L.append(f"| {r['day']} | {r['n']} | {r['grp']} | {pct:.0f}% | {r['avg_ret']:+.2f}% |")
        tot_n += r["n"] or 0
        tot_win += r["win_n"] or 0

    if tot_n:
        pct = tot_win / tot_n * 100
        L.append(f"- **累计**：{tot_n} 条被拦信号，若全部开仓 {pct:.0f}% 会赚钱")
        if pct >= 60:
            L.append("- → **拦错的比例高**：监控池可能太小，在漏机会（支持扩池）")
        elif pct >= 45:
            L.append("- → **对半开**：拦得没错也没漏，扩池收益不明确")
        else:
            L.append("- → **拦得对**：被拦的大多亏钱，维持现状甚至该收紧")

    # 按拦截原因拆
    by_reason = c.execute(
        "SELECT top_reason, sum(n) n, sum(case when ret_pct>0 then n else 0 end) w, "
        "       avg(ret_pct) ar FROM blocked_daily GROUP BY top_reason "
        "ORDER BY n DESC LIMIT 5").fetchall()
    if by_reason:
        L.append("")
        L.append("按拦截原因拆（看哪类拦得最冤）：")
        for r in by_reason:
            if not r["n"]:
                continue
            L.append(f"- {r['top_reason'] or '未知'}：{r['n']} 条，"
                     f"若开了 {r['w']/r['n']*100:.0f}% 会赚，平均 {r['ar']:+.2f}%")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    if args.summary:
        print(summarize())
        return 0
    n = backfill(args.days)
    print(f"回填完成：{n} 组")
    print()
    print(summarize())
    return 0


if __name__ == "__main__":
    sys.exit(main())
