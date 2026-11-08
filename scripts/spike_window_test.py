# -*- coding: utf-8 -*-
"""插针窗口 96h → 24h 回测验证（浩哥 2026-11-08 批准）

背景
  11-05 23:00 BTC/ETH 单根 1h 闪崩（3.61% / 4.76%），
  插针用「最近 96 根 1h 里最大单根振幅 ÷ ATR」，一根插针锁死 4 天。
  闪崩后 BTC 从 75525 涨到 80225（+6.22%），这波完全做不了。
  要验证：窗口缩到 24h 会多放进来多少笔、这些笔赚不赚钱。

口径（与线上完全一致，全部复用现成实现）
  - backend.strategies._ce_zlsma(ks, 1, 2.0, 50, True, True)
  - scripts.bbw_effect.simulate_all  （其 docstring 声明与 s15_live_parity 同源）
  - 2R 平半 / 半仓后 3R 全平 / 镜像出场 / 16h 超时 / 20 根摆动硬止损
  - fee_rate 0.0005
  - BBW 分位 >= 70（线上第③道闸）
  - 插针门槛 <= 8.0（现线上值）

只改一个变量：插针回看窗口（96 / 72 / 48 / 24 / 12 小时）

判据（事先声明，避免事后找理由）
  ✅ 采纳 24h 的条件：
     ① 净 R 不低于 96h（允许 -5% 以内）
     ② 最大回撤不超过 96h 的 1.3 倍
     ③ 新增的那些笔，均笔 R > 0
     ④ 新增笔在至少一半的币种上是正贡献（分币种 ≥10/20）
  ❌ 任何一条不过 → 维持 96h

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/spike_window_test.py
"""
import bisect
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma          # noqa: E402
from scripts.bbw_effect import simulate_all        # noqa: E402

SPIKE_MAX = 8.0
BBW_MIN = 70.0
WINDOWS = [96, 72, 48, 24, 12]
ATR_N = 14


def agg_1h(ks):
    """15m → 1h（4 根），返回 (bars, ts_list)"""
    out = []
    for i in range(0, len(ks) - 3, 4):
        g = ks[i:i + 4]
        out.append({"ts": int(g[0]["ts"]),
                    "h": max(x["high"] for x in g),
                    "l": min(x["low"] for x in g),
                    "c": g[-1]["close"]})
    return out


def atr_series(h1):
    """Wilder14 ATR 的滚动值，O(n)。atr[i] = 截至第 i 根的 ATR"""
    n = len(h1)
    out: list = [None] * n
    if n <= ATR_N:
        return out
    trs = [0.0]        # 索引 0 占位（第 0 根没有前一根，算不出 TR）
    for i in range(1, n):
        trs.append(max(h1[i]["h"] - h1[i]["l"],
                       abs(h1[i]["h"] - h1[i - 1]["c"]),
                       abs(h1[i]["l"] - h1[i - 1]["c"])))
    atr = sum(trs[1:ATR_N + 1]) / ATR_N
    out[ATR_N] = atr
    for i in range(ATR_N + 1, n):
        atr = (atr * (ATR_N - 1) + trs[i]) / ATR_N
        out[i] = atr
    return out


def spike_series(h1, atrs, win):
    """每个 1h 位置的插针值：最近 win 根最大振幅 ÷ ATR"""
    n = len(h1)
    amp = [b["h"] - b["l"] for b in h1]
    out: list = [None] * n
    for i in range(win - 1, n):
        a = atrs[i]
        if not a:
            continue
        out[i] = max(amp[i - win + 1:i + 1]) / a
    return out


def max_dd(trades):
    """按进场时间排序后累计 R 的最大回撤"""
    if not trades:
        return 0.0
    s = sorted(trades, key=lambda t: t["entry_ts"])
    cum = peak = dd = 0.0
    for t in s:
        cum += t["r"]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def stat(trades):
    if not trades:
        return dict(n=0, r=0.0, avg=0.0, win=0.0, dd=0.0)
    r = sum(t["r"] for t in trades)
    w = sum(1 for t in trades if t["r"] > 0) / len(trades) * 100
    return dict(n=len(trades), r=r, avg=r / len(trades), win=w, dd=max_dd(trades))


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    cache = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        cache.append((sym, ks))
    print(f"载入 {len(cache)} 币 × 60 天 15m K 线")
    print()

    # ── 1. 每个币跑一遍策略，拿到全部信号 ──
    all_tr = []
    for sym, ks in cache:
        ind = _ce_zlsma(ks, 1, 2.0, 50, True, True)
        trs = simulate_all(ks, ind)
        h1 = agg_1h(ks)
        atrs = atr_series(h1)
        h1_ts = [b["ts"] for b in h1]
        spikes = {w: spike_series(h1, atrs, w) for w in WINDOWS}
        for t in trs:
            t["sym"] = sym
            # 找进场时刻对应的 1h 位置
            j = bisect.bisect_right(h1_ts, t["entry_ts"]) - 1
            t["spike"] = {}
            for w in WINDOWS:
                t["spike"][w] = spikes[w][j] if 0 <= j < len(spikes[w]) else None
        all_tr.extend(trs)

    # ── 2. 只保留过了 BBW 门槛的（线上第③道） ──
    base = [t for t in all_tr if t["bbw"] is not None and t["bbw"] >= BBW_MIN]
    print(f"全部信号 {len(all_tr)} 笔，过 BBW>=70 后 {len(base)} 笔（这就是线上候选）")
    print()

    # ── 3. 各窗口对比 ──
    print("=" * 104)
    print("只改插针回看窗口，其余口径全部与线上一致（BBW>=70，插针<=8.0）")
    print("=" * 104)
    print(f"{'窗口':>8}{'笔数':>8}{'净R':>11}{'均笔R':>10}{'胜率':>9}"
          f"{'最大回撤':>12}{'相对96h笔数':>13}{'相对96h净R':>13}")
    print("-" * 104)

    groups = {}
    groups["无插针门槛"] = base
    for w in WINDOWS:
        groups[f"{w}h"] = [t for t in base
                           if t["spike"][w] is not None and t["spike"][w] <= SPIKE_MAX]

    ref = stat(groups["96h"])
    for label in ["无插针门槛"] + [f"{w}h" for w in WINDOWS]:
        s = stat(groups[label])
        if label == "无插针门槛":
            dn = f"{s['n'] - ref['n']:+d}"
            dr = f"{s['r'] - ref['r']:+.1f}R"
        else:
            dn = f"{s['n'] - ref['n']:+d}" if label != "96h" else "—"
            dr = f"{s['r'] - ref['r']:+.1f}R" if label != "96h" else "—"
        mark = "  <- 现在" if label == "96h" else ("  <- 提案" if label == "24h" else "")
        print(f"{label:>8}{s['n']:>8}{s['r']:>+11.1f}{s['avg']:>+10.3f}"
              f"{s['win']:>8.1f}%{s['dd']:>+12.1f}{dn:>13}{dr:>13}{mark}")

    # ── 4. 关键：24h 比 96h 多放进来的是哪些笔，赚不赚钱 ──
    print()
    print("=" * 104)
    print("★ 核心问题：24h 窗口比 96h 多放进来的那批笔，赚还是亏？")
    print("=" * 104)
    set96 = {(t["sym"], t["entry_ts"]) for t in groups["96h"]}
    for w in [72, 48, 24, 12]:
        extra = [t for t in groups[f"{w}h"] if (t["sym"], t["entry_ts"]) not in set96]
        s = stat(extra)
        if extra:
            print(f"  {w:>2}h 窗口新增 {s['n']:>4} 笔：净 {s['r']:>+7.1f}R  "
                  f"均笔 {s['avg']:>+6.3f}R  胜率 {s['win']:>5.1f}%")
        else:
            print(f"  {w:>2}h 窗口新增    0 笔")

    print()
    print("─" * 104)
    print("24h 新增笔的分币种贡献（判据④：至少 10/20 个币是正贡献）")
    print("─" * 104)
    extra24 = [t for t in groups["24h"] if (t["sym"], t["entry_ts"]) not in set96]
    by_sym = {}
    for t in extra24:
        by_sym.setdefault(t["sym"], []).append(t)
    pos = neg = 0
    for sym in sorted(by_sym, key=lambda s: -sum(x["r"] for x in by_sym[s])):
        g = by_sym[sym]
        s = stat(g)
        flag = "+" if s["r"] > 0 else "-"
        if s["r"] > 0:
            pos += 1
        else:
            neg += 1
        print(f"  {flag} {sym:<12}{s['n']:>4} 笔  净 {s['r']:>+7.1f}R  "
              f"均笔 {s['avg']:>+6.3f}R  胜率 {s['win']:>5.1f}%")
    print(f"\n  正贡献 {pos} 个币 / 负贡献 {neg} 个币")

    # ── 5. 出场原因对比：新增笔是不是更容易被硬止损 ──
    print()
    print("=" * 104)
    print("新增笔 vs 原有笔的出场原因分布（看是不是都死在硬止损上）")
    print("=" * 104)
    for label, grp in (("96h 原有笔", groups["96h"]), ("24h 新增笔", extra24)):
        if not grp:
            continue
        cnt = {}
        for t in grp:
            cnt[t["reason"]] = cnt.get(t["reason"], 0) + 1
        tot = len(grp)
        parts = [f"{k} {v}({v / tot * 100:.0f}%)"
                 for k, v in sorted(cnt.items(), key=lambda x: -x[1])]
        print(f"  {label:<12} {' / '.join(parts)}")

    # ── 6. BTC/ETH 单独看（这两个是事情起因） ──
    print()
    print("=" * 104)
    print("BTC / ETH 单独看（这两个是本次的问题源头）")
    print("=" * 104)
    for sym in ("BTCUSDT", "ETHUSDT"):
        print(f"\n  {sym}")
        for label in ["无插针门槛", "96h", "48h", "24h"]:
            g = [t for t in groups[label] if t["sym"] == sym]
            s = stat(g)
            print(f"    {label:<12}{s['n']:>4} 笔  净 {s['r']:>+7.1f}R  "
                  f"均笔 {s['avg']:>+6.3f}R  胜率 {s['win']:>5.1f}%")

    # ── 7. 判据核对 ──
    print()
    print("=" * 104)
    print("判据核对（事先声明，不是事后找理由）")
    print("=" * 104)
    s96, s24 = stat(groups["96h"]), stat(groups["24h"])
    e24 = stat(extra24)
    c1 = s24["r"] >= s96["r"] * 0.95
    c2 = abs(s24["dd"]) <= abs(s96["dd"]) * 1.3
    c3 = e24["avg"] > 0
    c4 = pos >= 10
    print(f"  ① 净R 不低于 96h 的 95%      : {s24['r']:+.1f}R vs {s96['r']:+.1f}R  "
          f"→ {'过' if c1 else '不过'}")
    print(f"  ② 回撤不超过 96h 的 1.3 倍    : {s24['dd']:+.1f}R vs {s96['dd']:+.1f}R  "
          f"→ {'过' if c2 else '不过'}")
    print(f"  ③ 新增笔均笔 R > 0            : {e24['avg']:+.3f}R  "
          f"→ {'过' if c3 else '不过'}")
    print(f"  ④ 新增笔正贡献币种 >= 10/20   : {pos}/20  "
          f"→ {'过' if c4 else '不过'}")
    print()
    if all([c1, c2, c3, c4]):
        print("  ✅ 四条全过 → 建议采纳 24h 窗口")
    else:
        print("  ❌ 有条件不过 → 建议维持 96h 窗口")


if __name__ == "__main__":
    main()
