"""
S14 VWAP 偏离回归 —— 改造版回测
════════════════════════════════════════════════════════════════════════

原版（backend/strategies_legacy.py:264）：
  1m K线 · VWAP60 · 偏离阈值 1~2% · 持仓 45 分钟 · 依赖实时 taker 流
  → 普通人用不了（要盯盘、要实时流、利润薄到被手续费吃掉）

本文件做两件事：
  ① 把原版逻辑「周期拉长」到 15m / 1h，看能不能救活
  ② 用实际数据量化：这个策略的数学期望到底是正还是负

════════════════════════════════════════════════════════════════════════
"""
import sys, os, json, glob, math, statistics

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FEE = 0.0005      # 币安 taker 0.05%
SLIP = 0.0003     # 滑点 0.03%


def rolling_vwap(ks, win):
    """滚动 VWAP。与原版一致：典型价 (H+L+C)/3 加权成交量。"""
    n = len(ks)
    pv = [0.0] * (n + 1)
    vv = [0.0] * (n + 1)
    for i, k in enumerate(ks):
        tp = (float(k["high"]) + float(k["low"]) + float(k["close"])) / 3
        v = float(k["volume"])
        pv[i + 1] = pv[i] + tp * v
        vv[i + 1] = vv[i] + v
    out = [None] * n
    for i in range(win, n):
        v = vv[i + 1] - vv[i + 1 - win]
        if v > 0:
            out[i] = (pv[i + 1] - pv[i + 1 - win]) / v
    return out


def efficiency(ks, i, win):
    """趋势效率 = |净涨跌| / 区间总波动"""
    if i < win:
        return None
    hi = max(float(ks[j]["high"]) for j in range(i - win, i + 1))
    lo = min(float(ks[j]["low"]) for j in range(i - win, i + 1))
    if hi <= lo:
        return None
    return abs(float(ks[i]["close"]) - float(ks[i - win]["close"])) / (hi - lo)


def simulate_s14(sym, ks, vwap_win=96, dev_pct=3.0, exit_frac=0.5,
                 stop_mult=0.8, max_hold=32, eff_max=1.01,
                 vol_max=99.0, cooldown=8, fee=FEE, slip=SLIP):
    """S14 改造版。

    与 S06 等一样按「每笔固定风险金额」记账 —— 这里统一用 R 表达：
      1R = 入场时的偏离幅度（因为原版止损就设在 0.8×dev，用 dev 做单位最自然）

    返回 trades 列表。
    """
    n = len(ks)
    vw = rolling_vwap(ks, vwap_win)
    vols = [float(k["volume"]) for k in ks]
    trades = []
    i = vwap_win
    while i < n - 2:
        if vw[i] is None:
            i += 1
            continue

        # 震荡市过滤
        if eff_max <= 1.0:
            e = efficiency(ks, i, vwap_win)
            if e is None or e > eff_max:
                i += 1
                continue

        c = float(ks[i]["close"])
        dev = c / vw[i] - 1
        if abs(dev) * 100 < dev_pct:
            i += 1
            continue

        # 量能过滤（模拟 taker 衰竭：成交萎缩说明推动力不足）
        if vol_max < 90:
            vm = sum(vols[i - 19:i + 1]) / 20 if i >= 19 else 0
            if vm <= 0 or vols[i] / vm > vol_max:
                i += 1
                continue

        sign = 1 if dev > 0 else -1          # 偏离方向
        side = -sign                          # 反向开仓
        want = "long" if side > 0 else "short"

        entry = c * (1 + slip * side)
        # 原版止损：继续偏离 0.8×dev
        if want == "long":
            stop = entry * (1 - abs(dev) * stop_mult)
            tp = vw[i] * (1 + dev * exit_frac)
        else:
            stop = entry * (1 + abs(dev) * stop_mult)
            tp = vw[i] * (1 + dev * exit_frac)

        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue

        exit_px = exit_reason = None
        exit_i = None
        peak_r = 0.0

        for j in range(i + 1, min(n, i + max_hold + 1)):
            h_ = float(ks[j]["high"])
            l_ = float(ks[j]["low"])
            cj = float(ks[j]["close"])
            o_ = float(ks[j]["open"])

            # 止损
            if want == "long" and l_ <= stop:
                exit_px = min(o_, stop); exit_reason = "止损"; exit_i = j; break
            if want == "short" and h_ >= stop:
                exit_px = max(o_, stop); exit_reason = "止损"; exit_i = j; break

            # 止盈
            if want == "long" and h_ >= tp:
                exit_px = max(o_, tp); exit_reason = "止盈"; exit_i = j; break
            if want == "short" and l_ <= tp:
                exit_px = min(o_, tp); exit_reason = "止盈"; exit_i = j; break

            peak_r = max(peak_r, ((h_ - entry) if want == "long" else (entry - l_)) / risk)

        if exit_i is None:
            exit_i = min(n - 1, i + max_hold)
            exit_px = float(ks[exit_i]["close"])
            exit_reason = "时间止损"

        gross_r = ((exit_px - entry) if want == "long" else (entry - exit_px)) / risk
        fee_r = 2 * fee * entry / risk
        net_r = gross_r - fee_r

        trades.append({
            "sym": sym, "side": want, "dev": dev * 100,
            "risk_pct": risk / entry * 100,
            "bars": exit_i - i, "gross_r": gross_r, "fee_r": fee_r,
            "net_r": net_r, "reason": exit_reason, "peak_r": peak_r,
        })
        i = exit_i + cooldown

    return trades


def stats(trades):
    if not trades:
        return None
    rs = [t["net_r"] for t in trades]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq = peak = 0.0
    maxdd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)
    gw = sum(wins) if wins else 0
    gl = abs(sum(losses)) if losses else 0
    why = {}
    for t in trades:
        why[t["reason"]] = why.get(t["reason"], 0) + 1
    return {
        "n": n, "win": len(wins) / n * 100, "total": sum(rs),
        "avg": statistics.mean(rs), "pf": gw / gl if gl > 0 else 999,
        "dd": maxdd, "why": why,
        "fee": sum(t["fee_r"] for t in trades),
        "gross": sum(t["gross_r"] for t in trades),
        "med_risk": statistics.median(t["risk_pct"] for t in trades),
        "avg_bars": statistics.mean(t["bars"] for t in trades),
    }


def load(interval, days):
    pat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                       f"*_{interval}_{days}d.json")
    out = {}
    for f in sorted(glob.glob(pat)):
        sym = os.path.basename(f).split("_")[0]
        try:
            ks = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if len(ks) >= 800:
            out[sym] = ks
    return out


def main():
    interval = sys.argv[1] if len(sys.argv) > 1 else "15m"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    data = load(interval, days)
    print(f"\n{'='*100}")
    print(f"S14 VWAP偏离回归 · 改造版  |  {interval} × {days}天 × {len(data)}币")
    print(f"手续费 taker {FEE*100:.2f}% 双程 + 滑点 {SLIP*100:.2f}%")
    print(f"{'='*100}\n")

    # 原版参数（换算到 15m：VWAP 96根=24h，持仓 32根=8h）
    print("【A · 原版参数直接拉长周期】")
    print(f"{'配置':<38}{'笔数':>6}{'胜率':>8}{'净R':>9}{'期望':>8}{'盈亏比':>7}{'回撤R':>8}")
    print("-" * 100)

    cases = [
        ("原版(偏离3% 止盈50% 止损0.8×dev)", dict(dev_pct=3.0, exit_frac=0.5, stop_mult=0.8)),
        ("偏离3% 止盈30%", dict(dev_pct=3.0, exit_frac=0.3, stop_mult=0.8)),
        ("偏离2% 止盈50%", dict(dev_pct=2.0, exit_frac=0.5, stop_mult=0.8)),
        ("偏离5% 止盈50%", dict(dev_pct=5.0, exit_frac=0.5, stop_mult=0.8)),
        ("偏离3% 止损放宽到1.5×dev", dict(dev_pct=3.0, exit_frac=0.5, stop_mult=1.5)),
        ("偏离3% 止损放宽到2.0×dev", dict(dev_pct=3.0, exit_frac=0.5, stop_mult=2.0)),
    ]
    for label, kw in cases:
        t = []
        for sym, ks in data.items():
            t.extend(simulate_s14(sym, ks, **kw))
        r = stats(t)
        if not r:
            print(f"{label:<38}无成交"); continue
        print(f"{label:<38}{r['n']:>6}{r['win']:>7.1f}%{r['total']:>+9.2f}{r['avg']:>+8.3f}"
              f"{r['pf']:>7.2f}{r['dd']:>8.1f}")

    print(f"\n【B · 加震荡市过滤（趋势效率 ≤ 阈值才做）】")
    print(f"{'配置':<38}{'笔数':>6}{'胜率':>8}{'净R':>9}{'期望':>8}{'盈亏比':>7}{'回撤R':>8}")
    print("-" * 100)
    for eff in (0.20, 0.30, 0.40):
        t = []
        for sym, ks in data.items():
            t.extend(simulate_s14(sym, ks, dev_pct=3.0, exit_frac=0.5,
                                  stop_mult=0.8, eff_max=eff))
        r = stats(t)
        if not r:
            print(f"效率≤{eff:<33}无成交"); continue
        print(f"{'偏离3% + 趋势效率≤' + str(eff):<38}{r['n']:>6}{r['win']:>7.1f}%{r['total']:>+9.2f}"
              f"{r['avg']:>+8.3f}{r['pf']:>7.2f}{r['dd']:>8.1f}")

    print(f"\n【C · 加量能萎缩过滤（模拟 taker 衰竭）】")
    print(f"{'配置':<38}{'笔数':>6}{'胜率':>8}{'净R':>9}{'期望':>8}{'盈亏比':>7}{'回撤R':>8}")
    print("-" * 100)
    for vm in (1.0, 0.7):
        t = []
        for sym, ks in data.items():
            t.extend(simulate_s14(sym, ks, dev_pct=3.0, exit_frac=0.5,
                                  stop_mult=0.8, vol_max=vm))
        r = stats(t)
        if not r:
            print(f"量能<{vm}×均{'':<28}无成交"); continue
        print(f"{'偏离3% + 成交量<' + str(vm) + '×20均':<38}{r['n']:>6}{r['win']:>7.1f}%{r['total']:>+9.2f}"
              f"{r['avg']:>+8.3f}{r['pf']:>7.2f}{r['dd']:>8.1f}")

    # 最佳组合做详细输出
    print(f"\n【D · 全部过滤叠加（震荡市 + 量能萎缩）】")
    t = []
    for sym, ks in data.items():
        t.extend(simulate_s14(sym, ks, dev_pct=3.0, exit_frac=0.5,
                              stop_mult=0.8, eff_max=0.30, vol_max=1.0))
    r = stats(t)
    if r:
        print(f"  {r['n']} 笔  胜率 {r['win']:.1f}%  净 {r['total']:+.2f}R  期望 {r['avg']:+.3f}R")
        print(f"  盈亏比 {r['pf']:.2f}  回撤 {r['dd']:.1f}R  手续费 {r['fee']:.2f}R")
        print(f"  出场分布: {r['why']}")
        print(f"  止损距离中位 {r['med_risk']:.2f}%  平均持仓 {r['avg_bars']:.0f} 根")
    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
