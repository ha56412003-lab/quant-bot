"""峰值 R 分布统计 —— 回答「一笔单子到底能走到多少 R」

浩哥问题（2026-11-09）：
    「一只票单周期内达到 0.8R / 1R / 1.2R / 1.5R / 1.8R / 2R 的概率有多少？
     目前我们设置的 2R 好像一次都没触发」

统计两种口径：
  A 自然峰值：只留硬止损 + 16h 超时 + 镜像，不平半、不移动止盈
              → 回答「市场到底给不给你走到 2R」
  B 现行规则：2R 平半 / 3R 全平（线上现状）→ 回答「实际记录到的峰值」

用法：python backtest/peak_dist.py
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma
from backtest.exit_speed_test import sim

THRESHOLDS = [0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0]


def load():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    cache = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        cache.append((sym, ks))
    return cache


def table(name, trades):
    n = len(trades)
    print("\n%s（%d 笔）" % (name, n))
    print("%-8s%8s%9s%11s%11s%10s" % ("阈值", "笔数", "占比", "该档均峰值R", "该档均最终R", "回吐R"))
    print("-" * 60)
    for th in THRESHOLDS:
        sel = [t for t in trades if t["peak"] >= th]
        if not sel:
            print("%-8s%8d%8.1f%%%11s%11s%10s" % ("≥%.1fR" % th, 0, 0.0, "-", "-", "-"))
            continue
        avg_peak = sum(t["peak"] for t in sel) / len(sel)
        avg_r = sum(t["r"] for t in sel) / len(sel)
        print("%-8s%8d%8.1f%%%11.2f%+11.2f%10.2f" % (
            "≥%.1fR" % th, len(sel), len(sel) / n * 100, avg_peak, avg_r, avg_peak - avg_r))


def main():
    cache = load()
    print("载入 %d 币" % len(cache))
    inds = {s: _ce_zlsma(k, 1, 2.0, 50, True, True) for s, k in cache}

    # A 自然峰值：不平半、不移动止盈
    nat = []
    for sym, ks in cache:
        nat.extend(sim(ks, inds[sym], half_at_r=99.0, half_tp_r=0.0))
    table("A · 自然峰值（只留硬止损/16h超时/镜像，不平半）", nat)

    # B 现行规则
    cur = []
    for sym, ks in cache:
        cur.extend(sim(ks, inds[sym]))
    table("B · 现行规则（2R 平半 / 3R 全平）", cur)

    # ── 峰值分布直方图 ──────────────────────────────────────
    print("\n【自然峰值分布直方图】（共 %d 笔）" % len(nat))
    bins = [(-99, 0), (0, 0.5), (0.5, 0.8), (0.8, 1.0), (1.0, 1.2), (1.2, 1.5),
            (1.5, 1.8), (1.8, 2.0), (2.0, 2.5), (2.5, 3.0), (3.0, 99)]
    for lo, hi in bins:
        sel = [t for t in nat if lo <= t["peak"] < hi]
        pct = len(sel) / len(nat) * 100
        bar = "█" * max(0, int(pct / 1.2))
        lbl = ("<0R" if lo == -99 else "≥3R" if hi == 99 else "%.1f~%.1f" % (lo, hi))
        print("  %-9s %5d  %5.1f%%  %s" % (lbl, len(sel), pct, bar))

    # ── 结论摘要 ────────────────────────────────────────────
    print("\n【一句话结论】")
    for th in [1.0, 2.0, 3.0]:
        sel = [t for t in nat if t["peak"] >= th]
        print("  自然峰值到 %.1fR 的：%d / %d = %.1f%%" % (th, len(sel), len(nat), len(sel) / len(nat) * 100))
    zero = [t for t in nat if t["peak"] <= 0]
    print("  从头到尾没赚过（峰值 ≤0R）的：%d / %d = %.1f%%" % (len(zero), len(nat), len(zero) / len(nat) * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())
