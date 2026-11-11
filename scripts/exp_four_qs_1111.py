# -*- coding: utf-8 -*-
"""11-11 浩哥四问的验证实验

① 1.5R 保本移损 —— 需要 sim 支持（另开脚本 exp_breakeven.py）
② 费后 RR 门槛 —— 分桶统计：窄止损单子的费后收益（本脚本 C 段）
③ 3×ATR 止损下限 —— 能不能减少 11-11 那 6 笔损失（本脚本 A/B 段）
④ MACD(5,10,5) 0 轴方向过滤（本脚本 A/B 段）

用法：.venv/Scripts/python.exe scripts/exp_four_qs_1111.py
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
        print(f"  {label:<26}     0 笔（全被挡）")
        return None
    s_ = canon(o)
    gp = sum(t["r"] for t in o if t["r"] > 0)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    r = dict(per_day=n / 60.0, n=n, win=s_["win"], pl=(gp / gl if gl else 0),
             net=gp - gl, dd=s_["mdd"], hold=st.mean(hs), per=(gp - gl) / n)
    print(f"  {label:<26}{r['per_day']:>8.2f}{r['win']:>8.1f}%{r['pl']:>8.2f}"
          f"{r['net']:>+10.2f}{r['dd']:>+10.2f}{r['hold']:>8.2f}{r['per']:>+9.4f}")
    return r

HDR = (f"  {'方案':<26}{'单量/天':>8}{'胜率':>9}{'盈亏比':>8}"
       f"{'净值R':>10}{'回撤R':>10}{'持单h':>8}{'每笔R':>9}")
LINE = "  " + "-" * 88

print("=" * 92)
print("A/B · ③ 3×ATR 止损下限  ｜ ④ MACD(5,10,5) 0 轴方向过滤")
print("=" * 92)
print(HDR)
print(LINE)
base = show("基线（11-10 定档）", run())
print(LINE)
show("+3×ATR 下限", run(atr_min_mult=3.0))
show("+2×ATR 下限", run(atr_min_mult=2.0))
show("+1.5×ATR 下限", run(atr_min_mult=1.5))
show("+1×ATR 下限", run(atr_min_mult=1.0))
print(LINE)
show("+MACD(5,10,5) 0轴过滤", run(macd_filter=True))
show("+MACD(12,26,9) 0轴过滤", run(macd_filter=True, macd_fast=12, macd_slow=26, macd_sig=9))
show("+MACD(5,10,5) +3×ATR", run(macd_filter=True, atr_min_mult=3.0))

print("\n" + "=" * 92)
print("C · ② 费后 RR：按「入场止损距离」分桶，看手续费吃掉多少")
print("=" * 92)
print("  费率 0.05% × 2（开+平）+ 滑点我按 0 算（纸面盘实测 slip=0），")
print("  费后 2R 净收益 = 2 × 止损距离 − 0.10%（百分比），再除以止损距离 = 费后 R 倍数\n")
bt = run()
BUCKETS = [(0, 0.5), (0.5, 0.75), (0.75, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, 99)]
print(f"  {'止损距离区间':<14}{'笔数':>6}{'占比':>8}{'胜率':>8}{'期望R':>9}"
      f"{'合计R':>9}{'费后2R净R':>11}{'费率占比':>10}")
print("  " + "-" * 82)
for lo, hi in BUCKETS:
    g = [t for t in bt if lo <= t["risk_pct"] < hi]
    if not g:
        continue
    w = sum(1 for t in g if t["r"] > 0) / len(g) * 100
    e = st.mean(t["r"] for t in g)
    tot = sum(t["r"] for t in g)
    sp = st.mean(t["risk_pct"] for t in g)
    # 到 2R 全平的理想单：毛 2R，扣掉往返手续费（按 R 计）
    fee_r = 0.10 / sp          # 手续费占 1R 的比例
    net2r = 2 - fee_r
    print(f"  {lo}~{hi if hi < 99 else '∞':<12}{len(g):>6}{len(g)/len(bt)*100:>7.1f}%"
          f"{w:>7.1f}%{e:>+9.3f}{tot:>+9.1f}{net2r:>11.2f}{fee_r/2*100:>9.1f}%")

print("\n" + "=" * 92)
print("D · ③ 直接回答：11-11 那 6 笔，加 3×ATR 下限会被挡掉吗")
print("=" * 92)
SIX = [
    # (币, 方向, 实际止损距离%, 入场时 ATR(15m) %)
    ("INJ", "short", 2.81, 5.24),
    ("ENA", "short", 4.11, 5.78),
    ("PUMP", "short", 3.89, 5.77),
    ("TRUMP", "short", 2.67, 3.94),
    ("XRP", "long", 1.01, 1.47),
    ("TAO", "long", 1.01, 1.68),
]
print(f"  {'币':<8}{'方向':<7}{'实际止损%':>10}{'ATR%':>9}{'3×ATR%':>10}{'会开吗':>9}{'亏损U':>9}")
print("  " + "-" * 62)
LOSS = {"INJ": 159.3, "ENA": 154.3, "PUMP": 153.7, "TRUMP": 154.6, "XRP": 153.6, "TAO": 149.6}
blocked = 0
saved = 0.0
for sym, side, sp, atr in SIX:
    need = 3 * atr
    ok = sp >= need
    # 即使 3×ATR 过了，还要过 max_risk_pct 5%
    if ok and need > 5.0:
        ok = False
    if not ok:
        blocked += 1
        saved += LOSS[sym]
    print(f"  {sym:<8}{side:<7}{sp:>9.2f}%{atr:>8.2f}%{need:>9.2f}%"
          f"{'不会开' if not ok else '照开':>9}{-LOSS[sym]:>+9.1f}")
print("  " + "-" * 62)
print(f"  6 笔中被挡 {blocked} 笔，可避免亏损 {saved:.1f}U")
print("\n  ★ 但代价看 A 段：3×ATR 把单量砍成多少 —— 那才是真问题。")
print("=" * 92)
