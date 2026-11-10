# -*- coding: utf-8 -*-
"""BBW 分位门槛对照组：65（朋友版） vs 70（我们的定档）

其余参数全部按 strategies.yaml 当前定档，只动 bbw_pct_min，
用来回答「朋友把 BBW 从 70 放宽到 65 到底值不值」。

用法：
  .venv/Scripts/python.exe scripts/exp_bbw_friend.py
"""
import os
import pickle
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import yaml  # noqa: E402
from backtest.exit_speed_test import sim  # noqa: E402
from backtest.s15_live_parity import stats as canon  # noqa: E402

cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
node = cfg["strategies"]["S15_CE_ZLSMA"]
P = node["params"]
mh = float(node.get("max_hold_hours") or 0)

BASE = dict(
    key_bar=bool(P["key_bar"]), key_mult=float(P["key_mult"]),
    key_closepos=float(P["key_closepos"]), key_bodyfrac=float(P["key_bodyfrac"]),
    stop_key=bool(P["stop_key"]), stop_mode="both", min_stop_pct=0.0,
    swing=int(P["swing_lookback"]),
    dual_stop_min_pct=float(P.get("dual_stop_min_pct") or 0.0), dual_mode="ce",
    half_at_r=float(P["half_at_r"]), half_tp_r=float(P.get("half_tp_r") or 0.0),
    full_at_r=float(P["full_at_r"]), right_exit=bool(P["right_exit"]),
    right_exit_strict=bool(P["right_exit_strict"]),
    maxh=(int(mh * 4) if mh > 0 else 999999),
    max_risk_pct=float(P["max_risk_pct"]), min_dev_pct=float(P["min_dev_pct"]),
    dev_atr_mult=float(P["dev_atr_mult"]),
    qf_bars=0, qf_min_r=0.0, trail_arm=0.0, trail_dd=0.0,
    mirror_exit=False, mirror_bbw_min=0.0,
)
thr = float(P["eff_min_signal"])

SC = r"E:\Hermes\cache\scratch"
D = pickle.load(open(os.path.join(SC, "pool_cache.pkl"), "rb"))
EFFV = pickle.load(open(os.path.join(SC, "effcache.pkl"), "rb"))
rank = pickle.load(open(os.path.join(SC, "rank_cache.pkl"), "rb"))
ALL = sorted([s for s in D if s in rank], key=lambda v: rank[v])[:40]

print("=" * 84)
print("BBW 分位门槛对照 · 40 币 × 60 天 · 其余参数按 11-10 定档")
print("=" * 84)
print(f"  {'BBW门槛':<10}{'单量/天':>9}{'胜率':>9}{'盈亏比':>9}{'净值R':>10}{'回撤R':>10}{'持单h':>9}{'每笔R':>9}")

rows = []
for bbw in (60, 65, 70, 75):
    B = dict(BASE, bbw_min=float(bbw))
    o = []
    for s in ALL:
        ks, ind, _ = D[s]
        o.extend(sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B))
    n = len(o)
    s_ = canon(o)
    gp = sum(t["r"] for t in o if t["r"] > 0)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    rows.append(dict(bbw=bbw, n=n, per_day=n / 60.0, win=s_["win"],
                     pl=(gp / gl if gl else 0), net=gp - gl, dd=s_["mdd"],
                     hold=st.mean(hs) if hs else 0, per=(gp - gl) / n if n else 0))
    r = rows[-1]
    print(f"  {bbw:<10}{r['per_day']:>9.2f}{r['win']:>8.1f}%{r['pl']:>9.2f}"
          f"{r['net']:>+10.2f}{r['dd']:>+10.2f}{r['hold']:>9.2f}{r['per']:>+9.4f}")

base = next(r for r in rows if r["bbw"] == 70)
print("\n" + "-" * 84)
print("与定档（BBW 70）对比")
print("-" * 84)
for r in rows:
    if r["bbw"] == 70:
        continue
    print(f"  BBW {r['bbw']:<3} 单量 {r['per_day']-base['per_day']:+.2f}/天  "
          f"胜率 {r['win']-base['win']:+.1f}pt  "
          f"盈亏比 {r['pl']-base['pl']:+.2f}  "
          f"净值 {r['net']-base['net']:+.2f}R  "
          f"回撤 {r['dd']-base['dd']:+.2f}R  "
          f"持单 {r['hold']-base['hold']:+.2f}h")
print("=" * 84)
