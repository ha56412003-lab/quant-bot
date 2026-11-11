# -*- coding: utf-8 -*-
"""监控池容量对比实验：40 / 50 / 60 / 80 —— 2026-11-11

浩哥的问题：「监控调到 60 会有哪些数据变化」

做法：
  与定档脚本 exp_ce_mult_1111.py 完全同一套口径（同一个 sim、同一个 stats），
  唯一变化的变量 = 池子容量 N（rank_cache 前 N 个币）。
  ce_mult 固定取 strategies.yaml 当前值（线上 1.5），其余参数全读 yaml。

★ 自证：N=40 那一行的六项必须等于定档基准
       （281 笔 / 4.68 单天 / 54.1% / 1.33 / +45.93R / -9.70R / 4.35h），否则本实验作废。

只读：不改 yaml、不写库、不动生产文件。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/exp_pool_size_1111.py
"""
import os
import pickle
import statistics as st
import sys
import time
from collections import defaultdict

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from backend.strategies import _ce_zlsma                    # noqa: E402
from backtest.exit_speed_test import sim                    # noqa: E402
from backtest.s15_live_parity import stats as canon         # noqa: E402

SC = r"E:\Hermes\cache\scratch"
SIZES = [40, 50, 60, 80]
BASE = dict(n=281, win=54.1, net=45.93, mdd=-9.70)


def build():
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    node = cfg["strategies"]["S15_CE_ZLSMA"]
    P = node["params"]
    mh = float(node.get("max_hold_hours") or P.get("max_hold_hours") or 0)
    B = dict(
        key_bar=bool(P["key_bar"]), key_mult=float(P["key_mult"]),
        key_closepos=float(P["key_closepos"]), key_bodyfrac=float(P["key_bodyfrac"]),
        stop_key=bool(P["stop_key"]), stop_mode="both",
        min_stop_pct=float(P.get("min_stop_dist_pct") or 0.0),
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
        be_at_r=float(P.get("be_at_r") or 0.0),
    )
    return P, B


def six_of(o):
    n = len(o)
    s_ = canon(o)
    win = [t for t in o if t["r"] > 0]
    gp = sum(t["r"] for t in win)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    sd = sorted(t["risk_pct"] for t in o if t.get("risk_pct"))
    return dict(n=n, per_day=n / 60.0, win=s_["win"], pl=(gp / gl if gl else 0.0),
                net=s_["r"], mdd=s_["mdd"], hold=st.mean(hs), med=st.median(hs),
                med_stop=(st.median(sd) if sd else 0.0))


def main():
    P, B = build()
    CE_LEN = int(P.get("ce_len") or 1)
    Z_LEN = int(P.get("zlsma_len") or 50)
    CM = float(P.get("ce_mult") or 1.5)
    thr = float(P["eff_min_signal"])
    MSB = int(P.get("max_same_bar_side") or 0)

    with open(os.path.join(SC, "pool_cache.pkl"), "rb") as f:
        D = pickle.load(f)
    with open(os.path.join(SC, "effcache.pkl"), "rb") as f:
        EFFV = pickle.load(f)
    with open(os.path.join(SC, "rank_cache.pkl"), "rb") as f:
        rank = pickle.load(f)

    pool = sorted([s for s in D if s in rank], key=lambda v: rank[v])
    print("=" * 96)
    print("监控池容量对比 · 可排序币 %d 个 · ce_mult=%.1f ce_len=%d z_len=%d" % (len(pool), CM, CE_LEN, Z_LEN))
    print("  其余参数全取 strategies.yaml：half_at_r=%.1f full_at_r=%.1f be_at_r=%.1f "
          "min_stop=%.2f%% 同K线限笔=%d"
          % (B["half_at_r"], B["full_at_r"], B["be_at_r"], B["min_stop_pct"], MSB))
    print("=" * 96)
    sys.stdout.flush()

    results = {}
    for N in SIZES:
        if N > len(pool):
            print("[跳过] N=%d 超过可用币数 %d" % (N, len(pool)))
            continue
        ALL = pool[:N]
        t0 = time.time()
        o = []
        for i, s in enumerate(ALL, 1):
            ks, _c, _ = D[s]
            ind = _ce_zlsma(ks, ce_len=CE_LEN, ce_mult=CM, z_len=Z_LEN)
            got = sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B)
            for t in got:
                t["symbol"] = s
                t["_rank"] = rank[s]
            o.extend(got)
            if i % 10 == 0:
                print("  N=%d  %d/%d 币  已 %d 笔原始信号  %.0fs" % (N, i, N, len(o), time.time() - t0))
                sys.stdout.flush()
        # ★ 同 K 线限笔（与 exp_ce_mult_1111.py:118-124 完全一致）
        #   按 (进场时间, 方向) 分组，每组只保留 strength 最低的 MSB 个
        raw_n = len(o)
        g = defaultdict(list)
        for t in o:
            g[(t["entry_ts"], t["side"])].append(t)
        kept = []
        for lst in g.values():
            lst.sort(key=lambda x: x.get("strength", 0.0))
            kept.extend(lst[:MSB])
        o = sorted(kept, key=lambda x: (x["entry_ts"], x.get("strength", 0.0)))

        results[N] = six_of(o)
        results[N]["raw_n"] = raw_n
        r = results[N]
        print("  N=%d 完成：原始 %d 笔 → 限笔后 %d 笔（拦掉 %d）/ %.2f 单天 / %.1f%% / %.2f / %+.2fR / %.2fR / %.2fh  (%.0fs)"
              % (N, raw_n, r["n"], raw_n - r["n"], r["per_day"], r["win"], r["pl"], r["net"], r["mdd"], r["hold"], time.time() - t0))
        sys.stdout.flush()

    print()
    print("=" * 96)
    print("六项对比")
    print("=" * 96)
    hdr = "%-8s %6s %8s %8s %8s %10s %10s %9s" % ("容量", "笔数", "单/天", "胜率", "盈亏比", "净值(R)", "回撤(R)", "均持(h)")
    print(hdr)
    print("-" * 96)
    for N in SIZES:
        if N not in results:
            continue
        r = results[N]
        mark = ""
        if N == 40:
            ok = (abs(r["n"] - BASE["n"]) < 2 and abs(r["win"] - BASE["win"]) < 0.3
                  and abs(r["net"] - BASE["net"]) < 0.5 and abs(r["mdd"] - BASE["mdd"]) < 0.5)
            mark = "  <-- 定档基准 " + ("一致 OK" if ok else "★不一致，本实验作废★")
        print("%-8d %6d %8.2f %7.1f%% %8.2f %+10.2f %10.2f %9.2f%s"
              % (N, r["n"], r["per_day"], r["win"], r["pl"], r["net"], r["mdd"], r["hold"], mark))

    print()
    print("=== 相对 40 的变化 ===")
    if 40 in results:
        b = results[40]
        for N in SIZES:
            if N not in results or N == 40:
                continue
            r = results[N]
            print("  容量 %d  vs 40 ：笔数 %+d (%.0f%%)  胜率 %+.1fpt  盈亏比 %+.2f  "
                  "净值 %+.2fR  回撤 %+.2fR  均持 %+.2fh"
                  % (N, r["n"] - b["n"], (r["n"] / b["n"] - 1) * 100, r["win"] - b["win"],
                     r["pl"] - b["pl"], r["net"] - b["net"], r["mdd"] - b["mdd"], r["hold"] - b["hold"]))

    print()
    print("=== 必查：扩容后多出来的币是哪些 ===")
    for N in SIZES:
        if N <= 40:
            continue
        extra = pool[40:N]
        print("  容量 %d 比 40 多出 %d 个：%s" % (N, len(extra), ", ".join(extra)))


if __name__ == "__main__":
    main()
