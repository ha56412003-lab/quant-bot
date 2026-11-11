# -*- coding: utf-8 -*-
"""B 方案：同一根 K 线（同一信号时点）同向最多开 N 笔 —— 2026-11-11

背景：11-10 23:45:22 那一秒，40 币同时算出 6 个空信号，系统全开了 →
      行情 V 型反转，6 个空 + 后续 2 个多全灭，已平 7 笔 −1,089.71U。
      根因不是止损参数，是「同一根 K 线的信号不是 N 个独立机会，
      是 1 个机会 × N 个标的」，而 max_same_side=6 等于不设防。

本脚本测三件事（全部只读，不改母本、不改 yaml）：
  A. 同 bar 上限 K：同一 entry_ts + 同一方向，最多保留 K 笔
  B. 同向并发上限 K：任意时刻同向未平仓 ≤ K（事件驱动，更严）
  C. 用 DB 里真实那 8 笔回放，看新规则下会剩几笔

★ 回撤用 backtest.s15_live_parity.stats()（按时间顺序），与线上口径一致。
★ 不修改 backtest/exit_speed_test.py（母本），筛选在脚本层做。

用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/exp_sameside_1111.py
"""
import os
import pickle
import sqlite3
import statistics as st
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import yaml

from backtest.exit_speed_test import sim
from backtest.s15_live_parity import stats as canon

SC = r"E:\Hermes\cache\scratch"


# ────────────────────────── 筛选器 ──────────────────────────
def cap_same_bar(trades, k):
    """同一 entry_ts（同一根 15m K 线收盘）+ 同一方向，最多保留 k 笔。

    保留顺序按币的 rank（成交额/排名），即「池子里更靠前的优先」。
    """
    g = defaultdict(list)
    for t in trades:
        g[(t["entry_ts"], t["side"])].append(t)
    out = []
    for lst in g.values():
        # ★ 按 strength 升序，与线上 engine 的择优入场同序（2026-11-11 改）
        #   engine: signals.sort((strength, fresh)) 升序 → 离均线最近的先开
        #   历史教训：先后用过「成交额排名」「strength 降序」，两者都和线上不同序，
        #   得出的「限 2 笔」结论一正一反。口径不同序 = 结论不可用。
        lst.sort(key=lambda x: x.get("strength", 0.0))
        out.extend(lst[:k])
    return sorted(out, key=lambda x: (x["entry_ts"], x.get("strength", 0.0)))


def cap_concurrent(trades, k):
    """任意时刻同向未平仓 ≤ k（事件驱动贪心）。

    比 cap_same_bar 严：挡的是「连续几根 K 线陆续开仓叠加」的情况。
    """
    out = []
    pools = {"long": [], "short": []}   # 放 exit_ts
    for t in sorted(trades, key=lambda x: (x["entry_ts"], x["_rank"])):
        p = pools[t["side"]]
        p[:] = [e for e in p if e > t["entry_ts"]]   # 已平仓的挪走
        if len(p) >= k:
            continue
        p.append(t["exit_ts"])
        out.append(t)
    return out


def six(trades, label):
    """六项 + 盈亏比，回撤用 canonical stats()。"""
    if not trades:
        return dict(label=label, n=0)
    tr = sorted(trades, key=lambda x: x["entry_ts"])
    s = canon(tr)
    win_r = [t["r"] for t in tr if t["r"] > 0]
    loss_r = [-t["r"] for t in tr if t["r"] <= 0]
    pl = (sum(win_r) / sum(loss_r)) if loss_r else 0.0
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in tr]
    sd = sorted(t["risk_pct"] for t in tr if t.get("risk_pct"))
    return dict(label=label, n=len(tr), per_day=len(tr) / 60.0, win=s["win"],
                pl=pl, net=s["r"], mdd=s["mdd"],
                hold=st.mean(hs), med=st.median(hs),
                med_stop=(st.median(sd) if sd else 0.0),
                min_stop=(min(sd) if sd else 0.0))


def row(d):
    if not d.get("n"):
        return "  %-30s   无交易" % d["label"]
    return ("  %-30s %6.2f %8.1f%% %7.2f %9.2f %9.2f %7.2f" %
            (d["label"], d["per_day"], d["win"], d["pl"], d["net"], d["mdd"], d["hold"]))


def main():
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    node = cfg["strategies"]["S15_CE_ZLSMA"]
    P = node["params"]
    mh = float(node.get("max_hold_hours") or 0)
    B = dict(
        key_bar=bool(P["key_bar"]), key_mult=float(P["key_mult"]),
        key_closepos=float(P["key_closepos"]), key_bodyfrac=float(P["key_bodyfrac"]),
        stop_key=bool(P["stop_key"]), stop_mode="both",
        min_stop_pct=float(P.get("min_stop_dist_pct") or 0.0),
        swing=int(P["swing_lookback"]),
        dual_stop_min_pct=float(P.get("dual_stop_min_pct") or 0.0), dual_mode="ce",
        half_at_r=float(P["half_at_r"]), half_tp_r=0.0,
        full_at_r=float(P["full_at_r"]), right_exit=bool(P["right_exit"]),
        right_exit_strict=bool(P["right_exit_strict"]),
        maxh=(int(mh * 4) if mh > 0 else 999999),
        max_risk_pct=float(P["max_risk_pct"]), min_dev_pct=float(P["min_dev_pct"]),
        dev_atr_mult=float(P["dev_atr_mult"]),
        qf_bars=0, qf_min_r=0.0, trail_arm=0.0, trail_dd=0.0,
        mirror_exit=False, mirror_bbw_min=0.0,
        be_at_r=float(P.get("be_at_r") or 0.0),
    )
    thr = float(P["eff_min_signal"])

    with open(os.path.join(SC, "pool_cache.pkl"), "rb") as f:
        D = pickle.load(f)
    with open(os.path.join(SC, "effcache.pkl"), "rb") as f:
        EFFV = pickle.load(f)
    with open(os.path.join(SC, "rank_cache.pkl"), "rb") as f:
        rank = pickle.load(f)
    ALL = sorted([s for s in D if s in rank], key=lambda v: rank[v])

    print("=" * 92)
    print("B 方案实验 · 同一根 K 线同向限笔（40 币 / 60 天 / 参数取 strategies.yaml 现值）")
    print("=" * 92)
    print("  %-30s %6s %9s %7s %9s %9s %7s" %
          ("方案", "单/天", "胜率", "盈亏比", "净值R", "回撤R", "均持h"))
    print("  " + "-" * 88)

    base = []
    for s in ALL[:40]:
        ks, ind, _ = D[s]
        got = sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B)
        for t in got:
            t["symbol"] = s
            t["_rank"] = rank[s]
        base.extend(got)

    d0 = six(base, "基线（现在线上口径）")
    print(row(d0))

    variants = []
    for k in (1, 2, 3, 4):
        d = six(cap_same_bar(base, k), "A 同 bar 同向 ≤ %d" % k)
        variants.append(("same", k, d))
        print(row(d))
    print("  " + "-" * 88)
    for k in (1, 2, 3, 4):
        d = six(cap_concurrent(base, k), "B 同向并发 ≤ %d" % k)
        variants.append(("conc", k, d))
        print(row(d))
    print("  " + "-" * 88)
    # 组合：先同 bar 限 2，再同向并发限 3 / 4
    for k2 in (3, 4, 5):
        d = six(cap_concurrent(cap_same_bar(base, 2), k2), "A2 + B%d（组合）" % k2)
        print(row(d))

    # ── 与基线比，谁在「胜率 + 盈亏比 + 时效」三项都保住的前提下净值最好
    print()
    print("=" * 92)
    print("判定（浩哥口径：先看胜率，再看盈亏比，净值不能崩）")
    print("=" * 92)
    best = None
    for kind, k, d in variants:
        if not d.get("n"):
            continue
        keep_win = d["win"] >= d0["win"] - 0.3          # 胜率允许掉 0.3 个点
        keep_pl = d["pl"] >= d0["pl"] - 0.03            # 盈亏比允许掉 0.03
        better = (d["win"] >= d0["win"] and d["pl"] >= d0["pl"])
        gain = d["net"] - d0["net"]
        mdd_gain = d["mdd"] - d0["mdd"]                  # 负数变小 = 回撤改善
        mark = "★" if (keep_win and keep_pl and gain > 0) else " "
        print("  %s %-26s 净值 %+6.2fR（%+6.2f） 回撤 %+6.2fR（%+5.2f） 单量 %+5.2f/天" %
              (mark, d["label"], d["net"], gain, d["mdd"], mdd_gain, d["per_day"] - d0["per_day"]))
        if keep_win and keep_pl and gain > 0:
            if best is None or d["net"] > best[2]["net"]:
                best = (kind, k, d)

    if best:
        print()
        print("  → 推荐：%s（净值 %.2fR / 胜率 %.1f%% / 盈亏比 %.2f / 回撤 %.2fR）" %
              (best[2]["label"], best[2]["net"], best[2]["win"], best[2]["pl"], best[2]["mdd"]))
    else:
        print()
        print("  → 没有一个方案能在保住胜率和盈亏比的前提下提升净值。")

    # ── C. 用 DB 真实那 8 笔回放
    print()
    print("=" * 92)
    print("C · 用本地库里真实的 8 笔回放（11-10 23:45 ~ 11-11 02:45）")
    print("=" * 92)
    c = sqlite3.connect("file:quantbot.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""SELECT id,symbol,side,opened_at,closed_at,pnl,peak_r,stop_price,entry_price
                        FROM positions WHERE id BETWEEN 97 AND 104 ORDER BY opened_at""").fetchall()
    tr = []
    for r in rows:
        tr.append(dict(symbol=r["symbol"], side=r["side"], entry_ts=r["opened_at"],
                       exit_ts=(r["closed_at"] or r["opened_at"] + 3600),
                       pnl=r["pnl"] or 0.0, _rank=rank.get(r["symbol"], 999)))
    print("  原始 %d 笔（已平合计 %.2fU）" % (len(tr), sum(t["pnl"] for t in tr)))
    for k in (1, 2, 3):
        kept = cap_same_bar(tr, k)
        names = "+".join("%s%s" % (t["symbol"].replace("USDT", ""), "空" if t["side"] == "short" else "多")
                         for t in kept)
        print("  同 bar 同向 ≤ %d → 留 %d 笔，合计 %+8.2fU   [%s]" %
              (k, len(kept), sum(t["pnl"] for t in kept), names))
    for k in (1, 2, 3):
        kept = cap_concurrent(tr, k)
        print("  同向并发 ≤ %d    → 留 %d 笔，合计 %+8.2fU" %
              (k, len(kept), sum(t["pnl"] for t in kept)))

    print()
    print("=" * 92)
    print("注：本脚本只读，未改 strategies.yaml / 未改回测母本。要上线才改配置。")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
