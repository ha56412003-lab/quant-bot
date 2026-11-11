# -*- coding: utf-8 -*-
"""同K线同向限笔 × 排序规则 二维扫描（2026-11-11）

为什么要有这个脚本：
  第一版实验里 cap_same_bar 按「成交额排名」留前 N 笔，结论是 ≤2 → 55.6%/1.39/+49.78R。
  但线上 engine 的择优入场是按 strength/(1+0.3*fresh) 降序（fresh 恒 0）→ 等价 strength 降序。
  换成和线上同序（strength 降序）后，≤2 变成 54.8%/1.34/+44.33R —— 结论反转。

  根因：strength = |close − ZLSMA| / ATR，也就是「离均线多远」。
        按它降序排 = 优先留下**最追高**的信号 → 限笔时把好信号挤掉了。

  所以「限几笔」不是唯一变量，「留哪个」同样是变量。本脚本扫二维：
  cap ∈ {2,3,4,5} × order ∈ {strength降序(线上现状), strength升序, 成交额排名, ATR降序, ATR升序}

★ 只读，不改 yaml、不改母本逻辑（sim 的 strength 字段是只读新增）。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/exp_sameside_order_1111.py
"""
import os
import pickle
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

ORDERS = [
    ("strength降序（线上现状）", lambda x: -x.get("strength", 0.0)),
    ("strength升序（最不追高）", lambda x: x.get("strength", 0.0)),
    ("成交额排名（池内靠前）", lambda x: x.get("_rank", 999)),
    ("ATR降序（波动最大）", lambda x: -(x.get("atr_pct") or 0.0)),
    ("ATR升序（波动最小）", lambda x: (x.get("atr_pct") or 0.0)),
]


def cap_same_bar(trades, k, keyfn):
    g = defaultdict(list)
    for t in trades:
        g[(t["entry_ts"], t["side"])].append(t)
    out = []
    for lst in g.values():
        lst.sort(key=keyfn)
        out.extend(lst[:k])
    return sorted(out, key=lambda x: x["entry_ts"])


def six(trades):
    if not trades:
        return None
    tr = sorted(trades, key=lambda x: x["entry_ts"])
    s = canon(tr)
    gp = sum(t["r"] for t in tr if t["r"] > 0)
    gl = -sum(t["r"] for t in tr if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in tr]
    return dict(n=len(tr), per_day=len(tr) / 60.0, win=s["win"],
                pl=(gp / gl if gl else 0), net=s["r"], mdd=s["mdd"],
                hold=st.mean(hs))


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

    base = []
    for s in ALL[:40]:
        ks, ind, _ = D[s]
        got = sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B)
        for t in got:
            t["symbol"] = s
            t["_rank"] = rank[s]
        base.extend(got)

    d0 = six(base)
    print("=" * 100)
    print("同K线同向限笔 × 排序规则 二维扫描（40 币 / 60 天 / 现值参数）")
    print("=" * 100)
    print("  基线（不限笔）                     %5.2f 单/天  %5.1f%%  %5.2f  %+7.2fR  %7.2fR  %5.2fh" %
          (d0["per_day"], d0["win"], d0["pl"], d0["net"], d0["mdd"], d0["hold"]))
    print("  已上线配置 max_same_bar_side=2 走的就是第一行（strength降序）")
    print()

    hdr = "  %-26s %6s %8s %7s %9s %9s %7s"
    print(hdr % ("排序规则 / 限笔数", "单/天", "胜率", "盈亏比", "净值R", "回撤R", "均持h"))
    print("  " + "-" * 96)

    rows = []
    for name, keyfn in ORDERS:
        for k in (2, 3, 4, 5):
            d = six(cap_same_bar(base, k, keyfn))
            rows.append((name, k, d))
            win_d = d["win"] - d0["win"]
            pl_d = d["pl"] - d0["pl"]
            net_d = d["net"] - d0["net"]
            mdd_d = d["mdd"] - d0["mdd"]
            flag = "★" if (win_d >= 0 and pl_d >= 0 and net_d >= 0) else " "
            print("  %s %-24s ≤%d %6.2f %7.1f%% %7.2f %+9.2f %+9.2f %7.2f   (%+.1fpt %+.2f %+.2fR %+.2f)" %
                  (flag, name, k, d["per_day"], d["win"], d["pl"], d["net"], d["mdd"], d["hold"],
                   win_d, pl_d, net_d, mdd_d))
        print("  " + "-" * 96)

    print()
    print("=" * 100)
    print("按「胜率优先」排序（浩哥口径）")
    print("=" * 100)
    good = [(n, k, d) for n, k, d in rows
            if d["win"] >= d0["win"] and d["pl"] >= d0["pl"] and d["net"] >= d0["net"] - 3]
    good.sort(key=lambda x: (-x[2]["win"], -x[2]["pl"]))
    if not good:
        print("  ⚠ 没有任何组合能在胜率、盈亏比都不低于基线的前提下把净值控制在 −3R 以内。")
        print("  最接近的：")
        alt = sorted(rows, key=lambda x: (-x[2]["win"], -x[2]["pl"]))[:5]
        for n, k, d in alt:
            print("    %-24s ≤%d  胜率 %.1f%% / 盈亏比 %.2f / 净值 %+.2fR / 回撤 %.2fR" %
                  (n, k, d["win"], d["pl"], d["net"], d["mdd"]))
    else:
        for n, k, d in good[:6]:
            print("  ★ %-24s ≤%d  胜率 %.1f%% / 盈亏比 %.2f / 净值 %+.2fR / 回撤 %.2fR / %.2f 单/天" %
                  (n, k, d["win"], d["pl"], d["net"], d["mdd"], d["per_day"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
