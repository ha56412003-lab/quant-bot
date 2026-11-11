# -*- coding: utf-8 -*-
"""ce_mult 对比实验 —— 本地 1.5 vs 服务器 2.0，到底差在哪（2026-11-11）

浩哥的问题：
    「本地和服务器两套系统的盈亏差距到底是为啥？是我们改坏了么」

背景事实（已从两边真实账本查出）：
    服务器：ce_mult 2.0 / half_at_r 1.8 / 还有镜像出场·快速失败·最大持仓 / 无保本 / 无止损下限
    本地  ：ce_mult 1.5 / half_at_r 1.2 / 这三条已物理删除 / 有保本 / 有止损下限 / 有同K线限笔
    → 两边不是同一套策略，不能直接比。本脚本只隔离出一个变量：ce_mult。

做法：
    同一个 pool_cache 的 K 线，用 _ce_zlsma 按不同 ce_mult 重算指标（不读缓存指标，
    缓存里的 ind 是按旧参数算的，直接用会串味），其余出场参数全部读当前 strategies.yaml。

★ 自证：ce_mult=1.5 那一行的六项必须与 verify_exit_final.py 的输出完全一致
       （281 笔 / 4.68 单天 / 54.1% / 1.33 / +45.93R / −9.70R / 4.35h），否则本实验作废。

只读，不改任何生产文件、不写库。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/exp_ce_mult_1111.py
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

from backend.strategies import _ce_zlsma          # noqa: E402  线上同一份 CE 实现
from backtest.exit_speed_test import sim          # noqa: E402
from backtest.s15_live_parity import stats as canon   # noqa: E402

SC = r"E:\Hermes\cache\scratch"


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
    thr = float(P["eff_min_signal"])
    MSB = int(P.get("max_same_bar_side") or 0)

    with open(os.path.join(SC, "pool_cache.pkl"), "rb") as f:
        D = pickle.load(f)
    with open(os.path.join(SC, "effcache.pkl"), "rb") as f:
        EFFV = pickle.load(f)
    with open(os.path.join(SC, "rank_cache.pkl"), "rb") as f:
        rank = pickle.load(f)
    ALL = sorted([s for s in D if s in rank], key=lambda v: rank[v])[:40]

    print("=" * 82)
    print("ce_mult 对比 · 40 币 × 60 天 · 其余参数全部取 strategies.yaml 当前值")
    print("  ce_len=%d  z_len=%d  half_at_r=%.1f  full_at_r=%.1f  be_at_r=%.1f  "
          "min_stop=%.2f%%  同K线限笔=%d"
          % (CE_LEN, Z_LEN, B["half_at_r"], B["full_at_r"], B["be_at_r"],
             B["min_stop_pct"], MSB))
    print("=" * 82)

    rows = {}
    for cm in (1.5, 2.0, 3.0):
        o = []
        for s in ALL:
            ks, _ind_cache, _ = D[s]
            # ★ 必须重算：pool_cache 里的 ind 是按旧 ce_mult 算的
            ind = _ce_zlsma(ks, ce_len=CE_LEN, ce_mult=cm, z_len=Z_LEN)
            got = sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B)
            for t in got:
                t["symbol"] = s
                t["_rank"] = rank[s]
            o.extend(got)
        raw_n = len(o)

        if MSB > 0:
            g = defaultdict(list)
            for t in o:
                g[(t["entry_ts"], t["side"])].append(t)
            kept = []
            for lst in g.values():
                lst.sort(key=lambda x: x.get("strength", 0.0))
                kept.extend(lst[:MSB])
            o = sorted(kept, key=lambda x: (x["entry_ts"], x.get("strength", 0.0)))

        six = six_of(o)
        six["raw_n"] = raw_n
        rows[cm] = (six, o)

        print()
        print("── ce_mult = %.1f %s" % (cm, "（本地现在跑的）" if cm == 1.5 else
                                       ("（服务器跑的）" if cm == 2.0 else "")))
        print("   原始信号 %d 笔 → 同K线限笔后 %d 笔（拦掉 %d）"
              % (raw_n, six["n"], raw_n - six["n"]))
        print("   每天单量  %.2f 单/天     胜率 %.1f%%     盈亏比 %.2f"
              % (six["per_day"], six["win"], six["pl"]))
        print("   净值      %+.2fR          回撤 %.2fR      均持 %.2fh（中位 %.2fh）"
              % (six["net"], six["mdd"], six["hold"], six["med"]))
        print("   中位止损  %.2f%%" % six["med_stop"])

    # ── 汇总对比表
    print()
    print("=" * 82)
    print("汇总")
    print("=" * 82)
    print("%-16s %8s %8s %8s %10s %9s %8s" %
          ("ce_mult", "单/天", "胜率", "盈亏比", "净值R", "回撤R", "均持h"))
    print("-" * 82)
    for cm in (1.5, 2.0, 3.0):
        s_ = rows[cm][0]
        tag = "%.1f" % cm
        if cm == 1.5:
            tag += " 本地◀"
        elif cm == 2.0:
            tag += " 服务器"
        print("%-16s %8.2f %7.1f%% %8.2f %+10.2f %9.2f %8.2f"
              % (tag, s_["per_day"], s_["win"], s_["pl"], s_["net"], s_["mdd"], s_["hold"]))

    # ── 自证：1.5 必须与 verify_exit_final 一致
    print()
    print("=" * 82)
    print("自证（ce_mult=1.5 必须等于 verify_exit_final 的六项）")
    print("=" * 82)
    s15 = rows[1.5][0]
    # ★ 2026-11-11 修：这里原来写的是旧基准（261 笔 / 56.3% / +53.75R / −9.07R），
    #   而打印出来的是新基准 —— 导致「比较用旧值、显示用新值」，永远报「不一致」。
    #   现在两边统一用定档六项。
    exp = dict(n=281, win=54.1, net=45.93, mdd=-9.70)
    ok = (s15["n"] == exp["n"] and abs(s15["win"] - exp["win"]) < 0.15
          and abs(s15["net"] - exp["net"]) < 0.5 and abs(s15["mdd"] - exp["mdd"]) < 0.5)
    print("  期望：%d 笔 / %.1f%% / %+.2fR / %.2fR" % (exp["n"], exp["win"], exp["net"], exp["mdd"]))
    print("  实际：%d 笔 / %.1f%% / %+.2fR / %.2fR   →  %s"
          % (s15["n"], s15["win"], s15["net"], s15["mdd"], "一致 ✓" if ok else "★不一致，本实验作废★"))
    if not ok:
        print("\n  说明：如果只是笔数或净值有小幅差异，可能是 K 线缓存更新；"
              "但差距大就说明筛选口径又漂了，不要用这份结果。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
