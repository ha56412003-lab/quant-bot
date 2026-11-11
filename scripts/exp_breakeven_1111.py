# -*- coding: utf-8 -*-
"""① 1.5R 保本移损 ｜ ③ 止损距离下限（绝对值 vs ATR 倍数）

用法：.venv/Scripts/python.exe scripts/exp_breakeven_1111.py
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
    half_at_r=float(P["half_at_r"]), half_tp_r=0.0, full_at_r=float(P["full_at_r"]),
    right_exit=bool(P["right_exit"]), right_exit_strict=bool(P["right_exit_strict"]),
    maxh=(int(mh * 4) if mh > 0 else 999999),
    max_risk_pct=float(P["max_risk_pct"]), min_dev_pct=float(P["min_dev_pct"]),
    dev_atr_mult=float(P["dev_atr_mult"]),
    qf_bars=0, qf_min_r=0.0, trail_arm=0.0, trail_dd=0.0,
    mirror_exit=False, mirror_bbw_min=0.0, bbw_min=float(P["bbw_pct_min"]),
)
thr = float(P["eff_min_signal"])
SC = r"E:\Hermes\cache\scratch"
D = pickle.load(open(os.path.join(SC, "pool_cache.pkl"), "rb"))
EFFV = pickle.load(open(os.path.join(SC, "effcache.pkl"), "rb"))
rank = pickle.load(open(os.path.join(SC, "rank_cache.pkl"), "rb"))
ALL = sorted([s for s in D if s in rank], key=lambda v: rank[v])[:40]


def run(**extra):
    B = dict(BASE, **extra)
    o = []
    for s in ALL:
        ks, ind, _ = D[s]
        for t in sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B):
            t["sym"] = s
            o.append(t)
    return o


def show(label, o):
    n = len(o)
    if n == 0:
        print(f"  {label:<24}     0 笔")
        return None
    s_ = canon(o)
    gp = sum(t["r"] for t in o if t["r"] > 0)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    be = sum(1 for t in o if t["reason"] == "breakeven")
    r = dict(n=n, per_day=n / 60.0, win=s_["win"], pl=(gp / gl if gl else 0),
             net=gp - gl, dd=s_["mdd"], hold=st.mean(hs), per=(gp - gl) / n, be=be)
    print(f"  {label:<24}{r['per_day']:>8.2f}{r['win']:>8.1f}%{r['pl']:>8.2f}"
          f"{r['net']:>+10.2f}{r['dd']:>+10.2f}{r['hold']:>8.2f}{r['per']:>+9.4f}{be:>7}")
    return r


HDR = (f"  {'方案':<24}{'单量/天':>8}{'胜率':>9}{'盈亏比':>8}"
       f"{'净值R':>10}{'回撤R':>10}{'持单h':>8}{'每笔R':>9}{'保本平':>7}")
LINE = "  " + "-" * 92

print("=" * 96)
print("① 保本移损（峰顶到 N R 后把止损抬到入场价）")
print("=" * 96)
print(HDR)
print(LINE)
base = show("基线（11-10 定档）", run())
print(LINE)
show("保本 1.2R（=平半线）", run(be_at_r=1.2))
show("保本 1.5R（朋友版）", run(be_at_r=1.5))
show("保本 1.8R", run(be_at_r=1.8))
show("保本 2.0R", run(be_at_r=2.0))
print(LINE)
show("保本 1.5R·仅平半后", run(be_at_r=1.5, be_after_half=True))

print("\n" + "=" * 96)
print("③ 止损距离下限：绝对值 vs ATR 倍数")
print("=" * 96)
print(HDR)
print(LINE)
print("  绝对值档（min_stop_pct）")
print(LINE)
show("止损 ≥ 0.60%", run(min_stop_pct=0.60))
show("止损 ≥ 0.75%", run(min_stop_pct=0.75))
show("止损 ≥ 1.00%", run(min_stop_pct=1.00))
show("止损 ≥ 1.20%", run(min_stop_pct=1.20))
show("止损 ≥ 1.50%", run(min_stop_pct=1.50))
print(LINE)
print("  ATR 倍数档（atr_min_mult）")
print(LINE)
show("止损 ≥ 0.5×ATR", run(atr_min_mult=0.5))
show("止损 ≥ 0.75×ATR", run(atr_min_mult=0.75))
show("止损 ≥ 1.0×ATR", run(atr_min_mult=1.0))

print("\n" + "=" * 96)
print("组合：止损 ≥0.75% + 保本 1.5R")
print("=" * 96)
print(HDR)
print(LINE)
show("0.75% + 保本1.5R", run(min_stop_pct=0.75, be_at_r=1.5))
show("0.75% + 保本1.2R", run(min_stop_pct=0.75, be_at_r=1.2))
show("1.0×ATR + 保本1.5R", run(atr_min_mult=1.0, be_at_r=1.5))
print("=" * 96)
