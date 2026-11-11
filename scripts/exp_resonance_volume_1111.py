# -*- coding: utf-8 -*-
"""横截面共振 × 成交量参与度 双维扫描（2026-11-11）

★★★ 2026-11-11 警告：本脚本的【量比】索引口径是错的，量能相关结论已作废 ★★★
    sim() 记的 entry_ts 是 ks[i+1]（信号在 i 收盘生成、下一根开盘进场），
    而线上 evaluate 判量比用的是 ks[i]（信号那根）。
    本脚本用 entry_ts 反查 → 量的是【进场根】，与线上差一根 K 线，
    335 笔里 69 笔（20.6%）判定相反 —— 「缩量闸很好」因此完全反过来。
    正确口径下（信号根）缩量闸 {1.0/1.2/1.3} 全部比不加更差（见 strategies.yaml
    的 min_vol_ratio 注释：+45.93R → +35.23R）。共振维度不受影响（共振数按
    entry_ts 的 bar 统计，同一根 bar 内一致性成立）。
    要重跑量能维度，必须把取索引改成 ts2i[entry_ts] - 1，并重跑 verify_exit_final 复核。

浩哥批准的方向：两条一起扫，看有没有做成第 8 道进场闸的空间。

【维度一】横截面共振 = 同一根 15m K 线、同一方向、40 个币里有几个同时出信号。
    假设：n_same 越大 = 越是市场级同步行情 = 越可能是假突破/集体反转（11-10 那波是 6 个）。

【维度二】成交量参与度 = 进场那根 K 线成交量 ÷ 前 96 根（24h）成交量中位数。
    假设：缩量突破（ratio 低）多是假突破；放量突破更可靠。

★ 口径：用当前 strategies.yaml 真实参数跑，**不限笔**（拿 335 笔原始信号），
   否则 n_same 的分桶会被「限 2 笔」提前切掉，看不全。
★ 只读，不写库、不改任何生产文件。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/exp_resonance_volume_1111.py
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

SC = r"E:\Hermes\cache\scratch"
VOL_WIN = 96          # 24 小时 = 96 根 15m


def load():
    with open(os.path.join(SC, "pool_cache.pkl"), "rb") as f:
        D = pickle.load(f)
    with open(os.path.join(SC, "effcache.pkl"), "rb") as f:
        E = pickle.load(f)
    with open(os.path.join(SC, "rank_cache.pkl"), "rb") as f:
        rank = pickle.load(f)
    ALL = sorted([s for s in D if s in rank], key=lambda v: rank[v])[:40]
    return D, E, rank, ALL


def run_all(D, E, rank, ALL, cfg):
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
    volmap = {}
    for s in ALL:
        ks = D[s][0]
        ind = _ce_zlsma(ks, ce_len=ce_len, ce_mult=float(P["ce_mult"]), z_len=z_len)
        got = sim(ks, ind, eff_mask=[x >= thr for x in E[s]], **B)
        # 预计算该币每根 bar 的量比
        vols = [float(k["volume"]) for k in ks]
        ts2i = {int(k["ts"]): i for i, k in enumerate(ks)}
        for t in got:
            i = ts2i.get(int(t["entry_ts"]))
            t["symbol"] = s
            t["_rank"] = rank[s]
            t["_vol_ratio"] = None
            if i is not None and i >= VOL_WIN:
                base = st.median(vols[i - VOL_WIN:i])
                if base > 0:
                    t["_vol_ratio"] = vols[i] / base
            out.append(t)
        volmap[s] = (ks, ts2i)
    return out


def agg(rows):
    n = len(rows)
    if not n:
        return None
    win = sum(1 for t in rows if t["r"] > 0)
    gp = sum(t["r"] for t in rows if t["r"] > 0)
    gl = -sum(t["r"] for t in rows if t["r"] <= 0)
    return dict(n=n, win=win / n * 100, pl=(gp / gl if gl else 0.0),
                avg=sum(t["r"] for t in rows) / n, net=sum(t["r"] for t in rows))


def table(title, buckets, total):
    print()
    print("=" * 84)
    print(title)
    print("=" * 84)
    hdr = "%-14s %7s %8s %9s %9s %9s %10s" % ("分档", "笔数", "占比", "胜率", "盈亏比", "均R", "总R")
    print(hdr)
    print("-" * 84)
    for name, rows in buckets:
        a = agg(rows)
        if not a:
            print("%-14s %7d %8s %9s %9s %9s %10s" % (name, 0, "-", "-", "-", "-", "-"))
            continue
        print("%-14s %7d %7.1f%% %8.1f%% %9.2f %+9.4f %+10.2f"
              % (name, a["n"], a["n"] / total * 100, a["win"], a["pl"], a["avg"], a["net"]))
    print("-" * 84)
    a = agg([t for _, rs in buckets for t in rs])
    if a:
        print("%-14s %7d %7.1f%% %8.1f%% %9.2f %+9.4f %+10.2f"
              % ("合计", a["n"], 100.0, a["win"], a["pl"], a["avg"], a["net"]))


def main():
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    D, E, rank, ALL = load()
    trades = run_all(D, E, rank, ALL, cfg)
    total = len(trades)
    print("原始信号 %d 笔（未限笔），币数 %d" % (total, len(ALL)))

    # ── 实验一：横截面共振
    cnt = defaultdict(int)
    for t in trades:
        cnt[(int(t["entry_ts"]), t["side"])] += 1
    for t in trades:
        t["_n_same"] = cnt[(int(t["entry_ts"]), t["side"])]

    b1 = []
    for k in (1, 2, 3, 4, 5):
        b1.append(("%d 个" % k, [t for t in trades if t["_n_same"] == k]))
    b1.append(("6+ 个", [t for t in trades if t["_n_same"] >= 6]))
    table("【实验一】横截面共振 = 同一根 K 线同向信号数", b1, total)

    # ── 实验二：成交量参与度
    vt = [t for t in trades if t["_vol_ratio"] is not None]
    b2 = [
        ("< 0.5x（极度缩量）", [t for t in vt if t["_vol_ratio"] < 0.5]),
        ("0.5~1.0x（缩量）", [t for t in vt if 0.5 <= t["_vol_ratio"] < 1.0]),
        ("1.0~2.0x（正常）", [t for t in vt if 1.0 <= t["_vol_ratio"] < 2.0]),
        ("2.0~3.0x（放量）", [t for t in vt if 2.0 <= t["_vol_ratio"] < 3.0]),
        ("3.0x+（爆量）", [t for t in vt if t["_vol_ratio"] >= 3.0]),
    ]
    table("【实验二】成交量参与度 = 进场 bar 量 ÷ 前 96 根中位数", b2, len(vt))
    print("（参与度可算 %d / %d 笔）" % (len(vt), total))

    # ── 交叉：共振数 × 成交量
    print()
    print("=" * 84)
    print("【交叉】共振数 × 成交量参与度（看有没有叠加效果）")
    print("=" * 84)
    print("%-12s %8s %8s %8s %8s" % ("共振\\量能", "缩量<1x", "正常1~2x", "放量2x+", "合计"))
    print("-" * 84)
    for lab, lo, hi in (("1~2 个", 1, 2), ("3~4 个", 3, 4), ("5+ 个", 5, 99)):
        g = [t for t in vt if lo <= t["_n_same"] <= hi]
        c1 = [t for t in g if t["_vol_ratio"] < 1.0]
        c2 = [t for t in g if 1.0 <= t["_vol_ratio"] < 2.0]
        c3 = [t for t in g if t["_vol_ratio"] >= 2.0]
        def w(rs):
            a = agg(rs)
            return ("%d/%0.1f%%" % (a["n"], a["win"])) if a else "0/-"
        print("%-12s %8s %8s %8s %8s" % (lab, w(c1), w(c2), w(c3), w(g)))
    print("（单元格格式：笔数/胜率）")

    # ── 11-10 那波落在哪一档
    print()
    print("=" * 84)
    print("【诊断】11-10 那波共线空单，在本次分桶里的位置")
    print("=" * 84)
    t0 = 1794240000      # 2026-11-10 23:20 左右（本地）
    wave = [t for t in trades if t0 <= int(t["entry_ts"]) <= t0 + 3600]
    if not wave:
        print("  这笔行情不在样本里，或已按当前参数不产生信号")
    for t in sorted(wave, key=lambda x: x["symbol"]):
        print("  %-12s %-4s  r=%+.2f  共振=%d 个  量比=%s"
              % (t["symbol"], t["side"], t["r"], t["_n_same"],
                 ("%.2fx" % t["_vol_ratio"]) if t["_vol_ratio"] is not None else "n/a"))
    if wave:
        a = agg(wave)
        print("  小计：%d 笔，胜率 %.1f%%，总R %+.2f" % (a["n"], a["win"], a["net"]))


if __name__ == "__main__":
    main()
