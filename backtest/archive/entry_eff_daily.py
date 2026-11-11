# -*- coding: utf-8 -*-
"""补测：24h EFF>=0.25 这组到底值不值

主测试里它是唯一「均笔R↑ + 回撤↓ + 币种稳」的，只差「净R 不如基准」。
浩哥的痛点是「某一天全亏」，不是「长期少赚」——所以要换个角度看：
  按日聚合，看「最差的一天」能改善多少。

对比三组：
  基准（线上现状，无 EFF 过滤）
  24h/>=0.25   （均笔最优候选）
  4h/>=0.45    （门槛最猛，回撤最小）
"""
import glob
import json
import os
import sys
import collections
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma                      # noqa: E402
from backtest.s15_live_parity import simulate, stats           # noqa: E402
from backtest.entry_eff_test import simulate_eff, eff_series    # noqa: E402


def load():
    cache = []
    for fp in sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json"))):
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) >= 800:
            cache.append((sym, ks))
    return cache


def daily(trades):
    """按出场日聚合 R"""
    d = collections.defaultdict(float)
    for t in trades:
        day = datetime.datetime.fromtimestamp(t["exit_ts"]).strftime("%m-%d")
        d[day] += t["r"]
    return d


def main():
    cache = load()
    print(f"载入 {len(cache)} 币\n")
    inds = {sym: _ce_zlsma(ks, 1, 2.0, 50, True, True) for sym, ks in cache}

    groups = {
        "基准(线上现状)": None,
        "24h EFF>=0.25": (96, 0.25),
        "4h EFF>=0.45": (16, 0.45),
    }

    res = {}
    for name, cfg in groups.items():
        tr = []
        for sym, ks in cache:
            if cfg is None:
                got = simulate(ks, inds[sym], want_mirror=True, use_bbw=True)
            else:
                win, thr = cfg
                got = simulate_eff(ks, inds[sym], eff_series(ks, win), thr, win)
            for t in got:
                t["sym"] = sym
            tr.extend(got)
        res[name] = tr

    # ── 总体 ─────────────────────────────────────────
    print("=" * 92)
    print("总体")
    print("=" * 92)
    print(f"{'方案':<18}{'笔数':>7}{'净R':>9}{'均笔R':>9}{'胜率':>8}{'回撤':>9}")
    print("-" * 92)
    for name, tr in res.items():
        s = stats(tr)
        print(f"{name:<18}{s['n']:>7}{s['r']:>+9.1f}{s['avg']:>+9.3f}{s['win']:>7.1f}%{s['mdd']:>+9.1f}")

    # ── 按日看：最差的一天 ─────────────────────────────
    print()
    print("=" * 92)
    print("按日看（这才是浩哥的痛点：某一天集中亏）")
    print("=" * 92)
    print(f"{'方案':<18}{'天数':>6}{'日均R':>9}{'最差日':>9}{'负R天数':>9}{'日胜率':>8}{'标准差':>9}")
    print("-" * 92)
    for name, tr in res.items():
        d = daily(tr)
        vals = sorted(d.values())
        n = len(vals)
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        neg = sum(1 for v in vals if v < 0)
        print(f"{name:<18}{n:>6}{mean:>+9.2f}{vals[0]:>+9.2f}{neg:>9}"
              f"{(n - neg) / n * 100:>7.1f}%{var ** 0.5:>9.2f}")

    print()
    print("  最差 5 天对比（同一批日期才可比）")
    print("-" * 92)
    alld = set()
    for tr in res.values():
        alld |= set(daily(tr).keys())
    days = sorted(alld)[-0:] or sorted(alld)
    print(f"  {'日期':<8}" + "".join(f"{n:>20}" for n in res))
    worst = sorted(alld, key=lambda x: min(daily(tr).get(x, 0) for tr in res.values()))[:8]
    for day in worst:
        line = f"  {day:<8}"
        for tr in res.values():
            v = daily(tr).get(day, 0.0)
            line += f"{v:>+20.2f}"
        print(line)

    # ── 防过拟合：前后半段 ──────────────────────────────
    print()
    print("=" * 92)
    print("防过拟合：60 天切前后两半，看优势是不是只在一段（上次出场 ATR 就栽在这）")
    print("=" * 92)
    base_tr = res["基准(线上现状)"]
    ts = sorted(t["entry_ts"] for t in base_tr)
    mid = ts[len(ts) // 2]
    print(f"{'方案':<18}{'前半净R':>11}{'后半净R':>11}   两段都更好？")
    print("-" * 92)
    b1 = sum(t["r"] for t in base_tr if t["entry_ts"] < mid)
    b2 = sum(t["r"] for t in base_tr if t["entry_ts"] >= mid)
    for name, tr in res.items():
        a = sum(t["r"] for t in tr if t["entry_ts"] < mid)
        b = sum(t["r"] for t in tr if t["entry_ts"] >= mid)
        if name.startswith("基准"):
            note = "—"
        else:
            note = "✅ 是" if (a > b1 and b > b2) else ("❌ 否" if a < b1 else "❌ 否")
        print(f"{name:<18}{a:>+11.1f}{b:>+11.1f}   {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
