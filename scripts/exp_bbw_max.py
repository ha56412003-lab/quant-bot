# -*- coding: utf-8 -*-
"""BBW 上限验证 + 分桶归因

回答 2026-11-11 六连亏提出的问题：
  六笔的入场 BBW 分位全在 84~96%，都是「暴跌/暴涨已经走完」的位置。
  我们的规则只有下限（≥70），没有上限 —— 是不是「BBW 越高越接近爆发末端」？

做两件事：
  A. 分桶归因：把定档的全量交易按入场 BBW 分位分桶，看每桶的胜率/期望R。
     如果 90-100 那档明显更差 → 假设成立，加上限有价值。
  B. 上限对照：BBW 下限固定 70，分别设上限 100/95/90/85/80，跑六项。

用法：.venv/Scripts/python.exe scripts/exp_bbw_max.py
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


def run(bbw_min, bbw_max=None):
    B = dict(BASE, bbw_min=float(bbw_min), bbw_max=bbw_max)
    o = []
    for s in ALL:
        ks, ind, _ = D[s]
        o.extend(sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B))
    return o


print("=" * 96)
print("A · 分桶归因：按「入场时的 BBW 分位」分组（定档全量，40 币 × 60 天）")
print("=" * 96)
base_trades = run(70.0, None)
print(f"  定档全量：{len(base_trades)} 笔  {len(base_trades)/60:.2f} 单/天\n")

BUCKETS = [(70, 80), (80, 85), (85, 90), (90, 95), (95, 101)]
print(f"  {'BBW 区间':<12}{'笔数':>7}{'占比':>8}{'胜率':>9}{'期望R':>10}{'合计R':>10}{'止损%':>9}{'均持h':>8}")
print("  " + "-" * 82)
for lo, hi in BUCKETS:
    g = [t for t in base_trades
         if t.get("bbw_pct") is not None and lo <= t["bbw_pct"] < hi]
    if not g:
        print(f"  {lo}~{hi if hi < 101 else 100:<10}     0      -        -         -         -        -       -")
        continue
    win = sum(1 for t in g if t["r"] > 0) / len(g) * 100
    exp = st.mean(t["r"] for t in g)
    tot = sum(t["r"] for t in g)
    sp = st.median([t["risk_pct"] for t in g]) if g else 0
    hh = st.mean([(t["exit_ts"] - t["entry_ts"]) / 3600 for t in g])
    print(f"  {lo}~{hi if hi < 101 else 100:<10}{len(g):>7}{len(g)/len(base_trades)*100:>7.1f}%"
          f"{win:>8.1f}%{exp:>+10.3f}{tot:>+10.1f}{sp:>8.2f}%{hh:>8.2f}")

print("\n" + "=" * 96)
print("B · 上限对照（下限固定 70）")
print("=" * 96)
print(f"  {'BBW 上限':<12}{'单量/天':>9}{'胜率':>9}{'盈亏比':>9}{'净值R':>10}{'回撤R':>10}{'持单h':>9}{'每笔R':>10}")
print("  " + "-" * 86)

rows = []
for hi in (None, 100, 95, 90, 85, 80):
    o = base_trades if hi is None else run(70.0, float(hi))
    n = len(o)
    if n == 0:
        continue
    s_ = canon(o)
    gp = sum(t["r"] for t in o if t["r"] > 0)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    r = dict(hi=hi, per_day=n / 60.0, n=n, win=s_["win"], pl=(gp / gl if gl else 0),
             net=gp - gl, dd=s_["mdd"], hold=st.mean(hs) if hs else 0, per=(gp - gl) / n)
    rows.append(r)
    label = "不设上限(定档)" if hi is None else f"≤{hi}"
    print(f"  {label:<12}{r['per_day']:>9.2f}{r['win']:>8.1f}%{r['pl']:>9.2f}"
          f"{r['net']:>+10.2f}{r['dd']:>+10.2f}{r['hold']:>9.2f}{r['per']:>+10.4f}")

base = rows[0]
print("\n" + "-" * 86)
print("与定档（不设上限）对比")
print("-" * 86)
for r in rows[1:]:
    print(f"  上限 {r['hi'] if r['hi'] else '∞':<4} 单量 {r['per_day']-base['per_day']:+.2f}/天  "
          f"胜率 {r['win']-base['win']:+.1f}pt  "
          f"盈亏比 {r['pl']-base['pl']:+.2f}  "
          f"净值 {r['net']-base['net']:+.2f}R  "
          f"回撤 {r['dd']-base['dd']:+.2f}R  "
          f"持单 {r['hold']-base['hold']:+.2f}h")
print("=" * 96)
