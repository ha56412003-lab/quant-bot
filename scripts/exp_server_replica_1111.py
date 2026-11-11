# -*- coding: utf-8 -*-
"""服务器复刻对比 —— 本地新版 vs 服务器在跑的那套（2026-11-11）

浩哥的问题：「本地和服务器盈亏差距到底为啥？是我们改坏了么」

前一步已证明：只把 ce_mult 从 1.5 改回 2.0，净值从 +53.75R 掉到 +18.78R。
但服务器不止 ce_mult 不同，一整套参数都不一样，所以那个数字不代表服务器真实水平。

本脚本用**服务器 strategies.yaml 的真实值**，在本地同一份母本（backend.strategies._ce_zlsma
+ backtest.exit_speed_test.sim）上复刻一遍，和本地当前版本对比。

服务器实际参数（2026-11-11 11:26 从 /opt/quantbot/strategies.yaml 读出）：
    ce_len 1 / ce_mult 2.0 / zlsma_len 50 / use_ha true
    swing_lookback 20        ← 本地 8
    min_dev_pct 0.5          ← 本地 0.0
    bbw_pct_min 70.0
    max_risk_pct 5.0
    quickfail_hours 4.0 / quickfail_min_r 0.5   ← 本地已物理删除
    half_at_r 1.8 / half_tp_r 1.0              ← 本地 1.2 / 0（关）
    trail_arm 1.5 / trail_dd 1.2               ← 本地已物理删除
    mirror_bbw_min 70.0（镜像出场）             ← 本地已物理删除
    max_hold_hours 16                           ← 本地无限制
    pools.monitor.stop_min_pct 1.0（池子级止损筛选，止损<1%的币不进池）
    无 保本移损 / 无 进场止损下限 / 无 同K线限笔

★ 说明：本脚本只做「同一批 40 币 60 天」的口径对比，不代表实盘同日表现。
   复刻是近似（池子级 stop_min_pct 用进场闸 min_stop_pct 近似），差异会写在输出里。

只读，不改生产文件、不写库。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/exp_server_replica_1111.py
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

from backend.strategies import _ce_zlsma          # noqa: E402
from backtest.exit_speed_test import sim          # noqa: E402
from backtest.s15_live_parity import stats as canon   # noqa: E402

SC = r"E:\Hermes\cache\scratch"


def load_cache():
    with open(os.path.join(SC, "pool_cache.pkl"), "rb") as f:
        D = pickle.load(f)
    with open(os.path.join(SC, "effcache.pkl"), "rb") as f:
        EFFV = pickle.load(f)
    with open(os.path.join(SC, "rank_cache.pkl"), "rb") as f:
        rank = pickle.load(f)
    ALL = sorted([s for s in D if s in rank], key=lambda v: rank[v])[:40]
    return D, EFFV, rank, ALL


def six_of(o):
    n = len(o)
    s_ = canon(o)
    win = [t for t in o if t["r"] > 0]
    gp = sum(t["r"] for t in win)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    return dict(n=n, per_day=n / 60.0, win=s_["win"], pl=(gp / gl if gl else 0.0),
                net=s_["r"], mdd=s_["mdd"], hold=st.mean(hs), med=st.median(hs))


def run_one(D, EFFV, rank, ALL, *, ce_mult, swing, min_dev, qf_bars, qf_min_r,
            trail_arm, trail_dd, mirror_exit, mirror_bbw, half_at_r, half_tp_r,
            maxh_bars, min_stop_pct, be_at_r, msb, thr, ce_len, z_len):
    B = dict(
        key_bar=True, key_mult=0.7, key_closepos=0.66, key_bodyfrac=0.5,
        stop_key=True, stop_mode="both",
        min_stop_pct=min_stop_pct, swing=swing,
        dual_stop_min_pct=0.5, dual_mode="ce",
        half_at_r=half_at_r, half_tp_r=half_tp_r,
        full_at_r=2.0, right_exit=True, right_exit_strict=True,
        maxh=maxh_bars,
        max_risk_pct=5.0, min_dev_pct=min_dev, dev_atr_mult=1.0,
        qf_bars=qf_bars, qf_min_r=qf_min_r,
        trail_arm=trail_arm, trail_dd=trail_dd,
        mirror_exit=mirror_exit, mirror_bbw_min=mirror_bbw,
        be_at_r=be_at_r,
    )
    o = []
    for s in ALL:
        ks, _c, _ = D[s]
        ind = _ce_zlsma(ks, ce_len=ce_len, ce_mult=ce_mult, z_len=z_len)
        got = sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B)
        for t in got:
            t["symbol"] = s
            t["_rank"] = rank[s]
        o.extend(got)
    raw_n = len(o)
    if msb > 0:
        g = defaultdict(list)
        for t in o:
            g[(t["entry_ts"], t["side"])].append(t)
        kept = []
        for lst in g.values():
            lst.sort(key=lambda x: x.get("strength", 0.0))
            kept.extend(lst[:msb])
        o = sorted(kept, key=lambda x: (x["entry_ts"], x.get("strength", 0.0)))
    six = six_of(o)
    six["raw_n"] = raw_n
    return six, o


def main():
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    P = cfg["strategies"]["S15_CE_ZLSMA"]["params"]
    ce_len = int(P.get("ce_len") or 1)
    z_len = int(P.get("zlsma_len") or 50)
    thr = float(P["eff_min_signal"])
    D, EFFV, rank, ALL = load_cache()

    # ── A. 本地当前版（读 yaml，与 verify_exit_final 同口径）
    mh = float(cfg["strategies"]["S15_CE_ZLSMA"].get("max_hold_hours") or 0)
    a_six, _ = run_one(
        D, EFFV, rank, ALL,
        ce_mult=float(P["ce_mult"]), swing=int(P["swing_lookback"]),
        min_dev=float(P["min_dev_pct"]),
        qf_bars=0, qf_min_r=0.0, trail_arm=0.0, trail_dd=0.0,
        mirror_exit=False, mirror_bbw=0.0,
        half_at_r=float(P["half_at_r"]), half_tp_r=float(P.get("half_tp_r") or 0.0),
        maxh_bars=(int(mh * 4) if mh > 0 else 999999),
        min_stop_pct=float(P.get("min_stop_dist_pct") or 0.0),
        be_at_r=float(P.get("be_at_r") or 0.0),
        msb=int(P.get("max_same_bar_side") or 0),
        thr=thr, ce_len=ce_len, z_len=z_len)

    # ── B. 服务器复刻（服务器真实参数）
    b_six, _ = run_one(
        D, EFFV, rank, ALL,
        ce_mult=2.0, swing=20, min_dev=0.5,
        qf_bars=16, qf_min_r=0.5,          # 4 小时
        trail_arm=1.5, trail_dd=1.2,
        mirror_exit=True, mirror_bbw=70.0,
        half_at_r=1.8, half_tp_r=1.0,
        maxh_bars=64,                       # 16 小时
        min_stop_pct=1.0,                   # 近似服务器的池子级 stop_min_pct 1.0
        be_at_r=0.0,
        msb=0,                              # 服务器没有同K线限笔
        thr=thr, ce_len=ce_len, z_len=z_len)

    print("=" * 88)
    print("本地新版  vs  服务器复刻（40 币 × 60 天，同一份 K 线缓存、同一份母本代码）")
    print("=" * 88)
    print("%-30s %10s %9s %9s %11s %10s %9s" %
          ("", "单/天", "胜率", "盈亏比", "净值R", "回撤R", "均持h"))
    print("-" * 88)

    def show(tag, s_):
        print("%-30s %10.2f %8.1f%% %9.2f %+11.2f %10.2f %9.2f"
              % (tag, s_["per_day"], s_["win"], s_["pl"], s_["net"], s_["mdd"], s_["hold"]))

    show("本地（1.5 / 限2笔 / 保本1.5R）", a_six)
    show("服务器复刻（2.0 / 16h强平 / 无保本）", b_six)
    print("-" * 88)
    print("  本地原始信号 %d 笔 → 限笔后 %d 笔" % (a_six["raw_n"], a_six["n"]))
    print("  服务器原始信号 %d 笔 → 无限笔，%d 笔" % (b_six["raw_n"], b_six["n"]))
    print()
    print("差异：单量 %+.2f 单/天 | 胜率 %+.1f 个点 | 盈亏比 %+.2f | "
          "净值 %+.2fR | 回撤 %+.2fR"
          % (a_six["per_day"] - b_six["per_day"], a_six["win"] - b_six["win"],
             a_six["pl"] - b_six["pl"], a_six["net"] - b_six["net"],
             a_six["mdd"] - b_six["mdd"]))
    print()
    print("⚠ 复刻是近似：服务器的 stop_min_pct 是「池子级筛币」，这里用进场闸 min_stop_pct 近似；")
    print("  池子 eff_min/eff_exit 两边都是 0.3/0，max_spike 8.0 相同，K 线缓存为同一份。")
    print("  因此这行数字代表「服务器那套参数在同一批行情下的水平」，不等于它的实盘逐笔。")


if __name__ == "__main__":
    main()
