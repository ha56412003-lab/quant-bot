# -*- coding: utf-8 -*-
"""加闸实验：横截面共振闸 × 成交量缩量闸（2026-11-11）

★★★ 2026-11-11 警告：本脚本的【量比】索引口径是错的，缩量闸结论已作废 ★★★
    sim() 记的 entry_ts 是 ks[i+1]（信号在 i 收盘生成、下一根开盘进场），
    线上 evaluate 判量比用的是 ks[i]（信号那根）。本脚本用 entry_ts 反查 →
    量的是【进场根】，335 笔里 69 笔（20.6%）判定与线上相反。
    正确口径下缩量闸每一档都比不加更差：
      不加 +45.93R / <1.0 +43.22R / <1.2 +38.88R / <1.3 +35.23R
    （见 strategies.yaml 的 min_vol_ratio 注释与 backend/strategies.py 第⑧道闸说明。）
    ★ 缩量闸已定档关闭（min_vol_ratio: 0.0）。要重跑量能维度，先改成 ts2i[entry_ts] - 1。
    ★ 共振维度不受此 bug 影响（共振数按 entry_ts 所在 bar 统计，同 bar 内一致性成立）。

分桶已经证明两件事：
  · 同 bar 同向信号数 ≥6 时胜率 36.4%、总R −13.79（全样本唯一灾难区）
  · 进场 bar 量比 <1.0（缩量）时胜率 34~40%、总R −11.20

本脚本把这两条做成进场闸，**真实跑一遍「闸 → 限2笔」的顺序**，
按浩哥口径出六项：单/天、胜率、盈亏比、净值R、回撤R、均持h。

★ 顺序口径：先过闸（淘汰整笔信号），再做同 bar 同向限 2 笔择优（strength 升序）。
★ 回撤必须用 backtest.s15_live_parity.stats（按时间顺序），不能用别的。
★ 自证：无闸那一行必须精确复现当前定档 281 笔 / 54.1% / 1.33 / +45.93R / −9.70R / 4.35h，
        否则本实验作废。
★ 只读，不写库、不改生产文件。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/exp_gates_1111.py
"""
import os
import pickle
import statistics as st
import sys
from collections import defaultdict

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from backend.strategies import _ce_zlsma        # noqa: E402
from backtest.exit_speed_test import sim        # noqa: E402
from backtest.s15_live_parity import stats as canon   # noqa: E402

SC = r"E:\Hermes\cache\scratch"
VOL_WIN = 96
CAP = 2

# 自证基准（当前定档，数据截止 2026-11-11 12:00）
BASE = dict(n=281, win=54.1, pl=1.33, net=45.93, mdd=-9.70, hold=4.35)


def load():
    with open(os.path.join(SC, "pool_cache.pkl"), "rb") as f:
        D = pickle.load(f)
    with open(os.path.join(SC, "effcache.pkl"), "rb") as f:
        E = pickle.load(f)
    with open(os.path.join(SC, "rank_cache.pkl"), "rb") as f:
        rank = pickle.load(f)
    ALL = sorted([s for s in D if s in rank], key=lambda v: rank[v])[:40]
    return D, E, rank, ALL


def build(cfg):
    D, E, rank, ALL = load()
    node = cfg["strategies"]["S15_CE_ZLSMA"]
    P = node["params"]
    mh = float(node.get("max_hold_hours") or 0)
    B = dict(
        key_bar=True, key_mult=0.7, key_closepos=0.66, key_bodyfrac=0.5,
        stop_key=True, stop_mode="both",
        min_stop_pct=float(P.get("min_stop_dist_pct") or 0.0),
        swing=int(P["swing_lookback"]),
        dual_stop_min_pct=0.5, dual_mode="ce",
        half_at_r=float(P["half_at_r"]), half_tp_r=float(P.get("half_tp_r") or 0.0),
        full_at_r=2.0, right_exit=True, right_exit_strict=True,
        maxh=(int(mh * 4) if mh > 0 else 999999),
        max_risk_pct=5.0, min_dev_pct=float(P["min_dev_pct"]), dev_atr_mult=1.0,
        qf_bars=0, qf_min_r=0.0, trail_arm=0.0, trail_dd=0.0,
        mirror_exit=False, mirror_bbw_min=0.0,
        be_at_r=float(P.get("be_at_r") or 0.0),
    )
    ce_len = int(P.get("ce_len") or 1)
    z_len = int(P.get("zlsma_len") or 50)
    thr = float(P["eff_min_signal"])

    out = []
    for s in ALL:
        ks = D[s][0]
        ind = _ce_zlsma(ks, ce_len=ce_len, ce_mult=float(P["ce_mult"]), z_len=z_len)
        got = sim(ks, ind, eff_mask=[x >= thr for x in E[s]], **B)
        vols = [float(k["volume"]) for k in ks]
        ts2i = {int(k["ts"]): i for i, k in enumerate(ks)}
        for t in got:
            i = ts2i.get(int(t["entry_ts"]))
            t["symbol"] = s
            t["_vol_ratio"] = None
            if i is not None and i >= VOL_WIN:
                base = st.median(vols[i - VOL_WIN:i])
                if base > 0:
                    t["_vol_ratio"] = vols[i] / base
            out.append(t)
    cnt = defaultdict(int)
    for t in out:
        cnt[(int(t["entry_ts"]), t["side"])] += 1
    for t in out:
        t["_n_same"] = cnt[(int(t["entry_ts"]), t["side"])]
    return out


def apply_gates(trades, res_gate, vol_gate, cap=CAP):
    o = list(trades)
    n0 = len(o)
    if res_gate:
        o = [t for t in o if t["_n_same"] < res_gate]
    n1 = len(o)
    if vol_gate:
        o = [t for t in o if (t["_vol_ratio"] is None) or (t["_vol_ratio"] >= vol_gate)]
    n2 = len(o)
    if cap:
        g = defaultdict(list)
        for t in o:
            g[(int(t["entry_ts"]), t["side"])].append(t)
        kept = []
        for lst in g.values():
            lst.sort(key=lambda x: x.get("strength", 0.0))     # 升序 = 线上口径
            kept.extend(lst[:cap])
        o = kept
    o = sorted(o, key=lambda x: x["exit_ts"])
    return o, n0, n1, n2


def six(o):
    s = canon(o)
    n = len(o)
    win = [t for t in o if t["r"] > 0]
    gp = sum(t["r"] for t in win)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    return dict(n=n, per_day=n / 60.0, win=s["win"], pl=(gp / gl if gl else 0.0),
                net=s["r"], mdd=s["mdd"], hold=st.mean(hs) if hs else 0.0)


def main():
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    tr = build(cfg)
    print("原始信号 %d 笔（未限笔）" % len(tr))
    print()

    plans = [
        ("基线：只限2笔（当前定档）", 0, 0.0),
        ("+ 共振闸 ≥6 不开", 6, 0.0),
        ("+ 共振闸 ≥5 不开", 5, 0.0),
        ("+ 共振闸 ≥7 不开", 7, 0.0),
        ("+ 缩量闸 <1.0 不开", 0, 1.0),
        ("+ 缩量闸 <0.8 不开", 0, 0.8),
        ("+ 缩量闸 <1.2 不开", 0, 1.2),
        ("+ 两闸（≥6 & <1.0）", 6, 1.0),
        ("+ 两闸（≥5 & <1.0）", 5, 1.0),
        ("+ 两闸（≥6 & <1.2）", 6, 1.2),
    ]

    hdr = "%-26s %7s %7s %8s %8s %9s %9s %8s" % (
        "方案", "笔数", "单/天", "胜率", "盈亏比", "净值R", "回撤R", "均持h")
    print("=" * len(hdr))
    print(hdr)
    print("-" * len(hdr))

    base_six = None
    for name, rg, vg in plans:
        o, n0, n1, n2 = apply_gates(tr, rg, vg)
        s = six(o)
        if base_six is None:
            base_six = s
        print("%-26s %7d %7.2f %7.1f%% %8.2f %+9.2f %9.2f %8.2f"
              % (name, s["n"], s["per_day"], s["win"], s["pl"], s["net"], s["mdd"], s["hold"]))
        print("%-26s   过共振 %d → 过缩量 %d → 限2笔 %d"
              % ("", n1, n2, s["n"]))
    print("-" * len(hdr))

    # ── 自证
    print()
    print("=" * 72)
    print("自证：基线必须精确复现当前定档")
    print("=" * 72)
    ok = True
    for k, want in (("n", BASE["n"]), ("net", BASE["net"]), ("mdd", BASE["mdd"]),
                    ("win", BASE["win"]), ("pl", BASE["pl"]), ("hold", BASE["hold"])):
        got = base_six[k]
        hit = abs(got - want) <= max(0.02, abs(want) * 0.005)
        ok = ok and hit
        print("  %-5s 期望 %10.2f  实际 %10.2f  %s"
              % (k, want, got, "PASS" if hit else "★FAIL"))
    print()
    if ok:
        print("  ✓ 自证通过，本次实验数字有效")
    else:
        print("  ★ 自证失败，本实验作废，不要用这些数字 ★")


if __name__ == "__main__":
    main()
