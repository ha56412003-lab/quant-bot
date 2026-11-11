# -*- coding: utf-8 -*-
"""① 保本移损：1.2R 到底好不好 —— 逐笔归因

浩哥 2026-11-11 在文档里拍板「改成保本移损 1.2R，有效挡掉笔数更多」。
本脚本回答两件事：
  A. 1.0 / 1.1 / 1.2 / 1.3 / 1.5 五档细扫（含「仅平半后」变体）
  B. 基线 vs 1.2R 逐笔对比：哪些单子被保本改了结局、各改了多少 R
     —— 分两类：从「亏 1R」救成「+0.6R」的（赚）／从「+1.6R」砍成「+0.6R」的（亏）

用法：.venv/Scripts/python.exe scripts/exp_be12_1111.py
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
    s_ = canon(o)
    gp = sum(t["r"] for t in o if t["r"] > 0)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    be = sum(1 for t in o if t["reason"] == "breakeven")
    print(f"  {label:<26}{n/60.0:>8.2f}{s_['win']:>8.1f}%{(gp/gl if gl else 0):>8.2f}"
          f"{gp-gl:>+10.2f}{s_['mdd']:>+10.2f}{st.mean(hs):>8.2f}{(gp-gl)/n:>+9.4f}{be:>7}")


print("=" * 96)
print("A · 保本移损细扫（峰顶到 N R 后把止损抬到入场价）")
print("=" * 96)
print(f"  {'方案':<26}{'单量/天':>8}{'胜率':>9}{'盈亏比':>8}{'净值R':>10}{'回撤R':>10}{'持单h':>8}{'每笔R':>9}{'保本平':>7}")
print("  " + "-" * 94)
base_t = run()
show("基线（无保本）", base_t)
print("  " + "-" * 94)
for r_ in (1.0, 1.1, 1.2, 1.3, 1.5):
    show(f"保本 {r_}R", run(be_at_r=r_))
print("  " + "-" * 94)
for r_ in (1.0, 1.2, 1.5):
    show(f"保本 {r_}R·仅平半后", run(be_at_r=r_, be_after_half=True))

print("\n" + "=" * 96)
print("B · 基线 vs 保本 1.2R —— 逐笔归因（同一笔单，结局变了多少）")
print("=" * 96)
b12 = run(be_at_r=1.2)
idx = {(t["sym"], t["entry_ts"]): t for t in base_t}
changed = []
for t in b12:
    k = (t["sym"], t["entry_ts"])
    o = idx.get(k)
    if o is None:
        continue
    d = t["r"] - o["r"]
    if abs(d) > 1e-9:
        changed.append((t["sym"], o, t, d))

saved = [c for c in changed if c[3] > 0]
hurt = [c for c in changed if c[3] < 0]
print(f"  结局被保本改掉的单子：{len(changed)} 笔")
print(f"    ├─ 变好（从亏损里救回来）：{len(saved)} 笔，合计 {sum(c[3] for c in saved):+.2f}R")
print(f"    └─ 变差（本来能赚更多）：{len(hurt)} 笔，合计 {sum(c[3] for c in hurt):+.2f}R")
print(f"  净影响：{sum(c[3] for c in changed):+.2f}R\n")

print(f"  {'币':<10}{'原R':>8}{'新R':>8}{'差':>8}   原出场 → 新出场")
print("  " + "-" * 66)
for sym, o, t, d in sorted(changed, key=lambda c: c[3])[:14]:
    print(f"  {sym.replace('USDT',''):<10}{o['r']:>+8.3f}{t['r']:>+8.3f}{d:>+8.3f}"
          f"   {o['reason']:<14} → {t['reason']}")
if len(changed) > 14:
    print(f"  ...（另有 {len(changed)-14} 笔，完整的变差/变好两组合计已在上方）")

print("\n" + "=" * 96)
print("C · 关键结论：1.2R 保本 + 平半 = 这笔单最差锁 +0.6R")
print("=" * 96)
print("  sim 的 R 记法：平半那部分按 half_at_r(1.2R) 记，剩半仓打保本价 → 0")
print("  所以 r = 0.5×1.2 + 0.5×0 = 0.600R")
print("  也就是说：保本 1.2R 不是「挡掉」（回到 0），是「锁定 +0.6R」。")
print("=" * 96)
