# -*- coding: utf-8 -*-
"""检验浩哥的质疑：用「BTC 是否同向」代替「数信号个数」做闸，是不是更好？

背景：浩哥问「币圈同向很普遍，一般不都是看 BTC 走向么」——
      这比"数 40 个币里有几个出信号"更本质。本脚本把两种判据放一起比。

判据 A（我原来的）：同 bar 同向信号数 n_same ≥ 6 → 不开
判据 B（浩哥提的）：同一根 bar，BTC 是否也出了同向信号 → 出了就不开
判据 C（更本质的）：同一根 bar，BTC 自己的 CE 方向是否与这笔同向（不管 BTC 有没有出信号）

只读，不写库。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/exp_btc_gate_1111.py
"""
import datetime
import os
import sys
from collections import defaultdict

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.exp_gates_1111 import apply_gates, build, six   # noqa: E402

TZ = datetime.timezone(datetime.timedelta(hours=8))


def tbl(title, buckets, total):
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)
    print("%-26s %7s %8s %9s %9s %10s" % ("分档", "笔数", "占比", "胜率", "盈亏比", "总R"))
    print("-" * 88)
    allrows = []
    for name, rows in buckets:
        allrows.extend(rows)
        n = len(rows)
        if not n:
            print("%-26s %7d %8s %9s %9s %10s" % (name, 0, "-", "-", "-", "-"))
            continue
        win = sum(1 for t in rows if t["r"] > 0) / n * 100
        gp = sum(t["r"] for t in rows if t["r"] > 0)
        gl = -sum(t["r"] for t in rows if t["r"] <= 0)
        net = sum(t["r"] for t in rows)
        print("%-26s %7d %7.1f%% %8.1f%% %9.2f %+10.2f"
              % (name, n, n / total * 100, win, (gp / gl if gl else 0.0), net))
    print("-" * 88)
    n = len(allrows)
    win = sum(1 for t in allrows if t["r"] > 0) / n * 100
    gp = sum(t["r"] for t in allrows if t["r"] > 0)
    gl = -sum(t["r"] for t in allrows if t["r"] <= 0)
    print("%-26s %7d %7.1f%% %8.1f%% %9.2f %+10.2f"
          % ("合计", n, 100.0, win, (gp / gl if gl else 0.0), sum(t["r"] for t in allrows)))


def main():
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    tr = build(cfg)
    print("原始信号 %d 笔（未限笔）" % len(tr))

    # ── 判据 B：BTC 是否在同一根 bar 出了同向信号
    btc_sig = {(int(t["entry_ts"]), t["side"]) for t in tr if t["symbol"] == "BTCUSDT"}
    for t in tr:
        t["_btc_signal"] = (int(t["entry_ts"]), t["side"]) in btc_sig

    # ── 只看非 BTC 的单（BTC 自己没必要跟自己比）
    other = [t for t in tr if t["symbol"] != "BTCUSDT"]
    btc = [t for t in tr if t["symbol"] == "BTCUSDT"]
    print("其中 BTC 自身 %d 笔，其他币 %d 笔" % (len(btc), len(other)))

    tbl("【判据 B】BTC 同期有没有出同向信号（只看非 BTC 的单）",
        [("BTC 没出信号", [t for t in other if not t["_btc_signal"]]),
         ("BTC 也出了同向信号", [t for t in other if t["_btc_signal"]])],
        len(other))

    # ── 判据 A 对照：共振数分档（只看非 BTC）
    tbl("【判据 A】同 bar 同向信号数（只看非 BTC）",
        [("1~2 个", [t for t in other if t["_n_same"] <= 2]),
         ("3~5 个", [t for t in other if 3 <= t["_n_same"] <= 5]),
         ("6+ 个", [t for t in other if t["_n_same"] >= 6])],
        len(other))

    # ── 交叉：共振数 × BTC 是否同向
    print()
    print("=" * 88)
    print("【交叉】共振数 × BTC 同向（单元格：笔数 / 胜率）")
    print("=" * 88)
    print("%-14s %20s %20s" % ("共振数", "BTC 没出信号", "BTC 也出了"))
    print("-" * 88)
    for lab, lo, hi in (("1~2 个", 1, 2), ("3~5 个", 3, 5), ("6+ 个", 6, 999)):
        g = [t for t in other if lo <= t["_n_same"] <= hi]
        a = [t for t in g if not t["_btc_signal"]]
        b = [t for t in g if t["_btc_signal"]]
        def w(rs):
            if not rs:
                return "0 / -"
            return "%d / %0.1f%%" % (len(rs), sum(1 for t in rs if t["r"] > 0) / len(rs) * 100)
        print("%-14s %20s %20s" % (lab, w(a), w(b)))

    # ── 三种闸的真实回测对照（含限 2 笔）
    print()
    print("=" * 88)
    print("【回测对照】三种闸各自落地后，全样本六项（都含限 2 笔）")
    print("=" * 88)
    print("%-34s %7s %7s %8s %8s %9s" % ("方案", "笔数", "单/天", "胜率", "盈亏比", "净值R"))
    print("-" * 88)

    def line(name, rows):
        s = six(rows)
        print("%-34s %7d %7.2f %7.1f%% %8.2f %+9.2f"
              % (name, s["n"], s["per_day"], s["win"], s["pl"], s["net"]))

    base, *_ = apply_gates(tr, 0, 0.0)
    line("基线（只限2笔）", base)

    # 判据 A：共振 ≥6 砍
    a_rows = [t for t in tr if t["_n_same"] < 6]
    a_rows = apply_gates(a_rows, 0, 0.0, cap=2)[0]
    line("A：共振≥6 砍", a_rows)

    # 判据 B：BTC 同向就砍（BTC 自己的单不受此限）
    b_rows = [t for t in tr if t["symbol"] == "BTCUSDT" or not t["_btc_signal"]]
    b_rows = apply_gates(b_rows, 0, 0.0, cap=2)[0]
    line("B：BTC 同向就砍", b_rows)

    # 判据 A+B
    ab = [t for t in tr if t["_n_same"] < 6 and (t["symbol"] == "BTCUSDT" or not t["_btc_signal"])]
    ab = apply_gates(ab, 0, 0.0, cap=2)[0]
    line("A+B：共振≥6 且 BTC 同向 都砍", ab)

    # 叠加缩量闸 <1.2
    print("-" * 88)
    for nm, rows in (("基线 + 缩量<1.2", base), ("A + 缩量<1.2", a_rows),
                     ("B + 缩量<1.2", b_rows), ("A+B + 缩量<1.2", ab)):
        if "缩量" in nm:
            src = tr if nm.startswith("基线") else (
                a_rows if nm.startswith("A +") else (b_rows if nm.startswith("B +") else ab))
            # 重新从原始集合出发加缩量
            if nm.startswith("基线"):
                pool = tr
            elif nm.startswith("A +"):
                pool = [t for t in tr if t["_n_same"] < 6]
            elif nm.startswith("B +"):
                pool = [t for t in tr if t["symbol"] == "BTCUSDT" or not t["_btc_signal"]]
            else:
                pool = [t for t in tr if t["_n_same"] < 6 and (t["symbol"] == "BTCUSDT" or not t["_btc_signal"])]
            o, *_ = apply_gates(pool, 0, 1.2, cap=2)
            line(nm, o)


if __name__ == "__main__":
    main()
