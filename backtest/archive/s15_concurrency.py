"""并发模拟：最多 6 仓的真实约束下，新旧两套出场方案的收益对比

为什么必须做：
  前面的 +100.1R 是「无限仓位」口径 —— 信号来了就开，不管手上有没有仓。
  实盘 max_concurrent_coins=6，镜像方案平均持仓 35 根（8.75 小时），
  比旧方案 24.9 根（6.2 小时）长 43%，新信号更容易被卡在门外。
  回测能无限开仓，实盘不能 —— 两者差距就是「并发折损」。

做法：
  1. 两种出场方案各自跑出全部候选交易（含 entry_ts / exit_ts）
  2. 按 entry_ts 时间排序，逐个尝试入场
  3. 入场时统计「同一时刻已持仓数」，>= 6 就跳过（这笔实盘吃不到）
  4. 汇总实际能成交的那些交易

用法：python backtest/s15_concurrency.py
"""
import json
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 指标直接调用线上实现，杜绝回测口径与线上不一致（2026-11-08 对账：
# s15_final.build() 用 high/low 极值，线上 use_close=true 用 close 极值）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.strategies import _ce_zlsma

FEE = 0.0005
SWING = 20
BBW_MIN = 70.0
MAXH = 64
MAX_CONCURRENT = 6


def gen_trades(sym, ks, ind, mode):
    """跑出该币全部候选交易。mode: 'mirror'（新）| 'legacy'（旧）"""
    ho, hh, hl, hc = ind["open"], ind["high_raw"], ind["low_raw"], ind["close"]
    zl, dirs, atr, bp = ind["zlsma"], ind["dir"], ind["atr"], ind["bbw_pct"]
    n = len(hc)
    out = []
    i = max(1, 60, SWING)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None or atr[i] is None:
            i += 1
            continue
        sl = dirs[i] == 1 and dirs[i - 1] == -1 and hc[i] > zl[i]
        ss = dirs[i] == -1 and dirs[i - 1] == 1 and hc[i] < zl[i]
        if not (sl or ss):
            i += 1
            continue
        want = "long" if sl else "short"
        key = (hc[i] > ho[i] and hc[i] > zl[i]) if want == "long" \
            else (hc[i] < ho[i] and hc[i] < zl[i])
        if not key:
            i += 1
            continue
        if bp[i] is None or bp[i] < BBW_MIN:
            i += 1
            continue
        entry = float(ks[i + 1]["open"])
        if entry <= 0:
            i += 1
            continue
        lo = max(0, i - SWING + 1)
        s_lo = min(float(ks[j]["low"]) for j in range(lo, i + 1))
        s_hi = max(float(ks[j]["high"]) for j in range(lo, i + 1))
        stop = s_lo if want == "long" else s_hi
        risk = (entry - stop) if want == "long" else (stop - entry)
        if risk <= 0 or risk / entry * 100 > 30:
            i += 1
            continue
        sign = 1 if want == "long" else -1
        peak = 0.0
        half_done = False
        ep = er = ei = None
        ma_cnt = 0
        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            if want == "long" and l_ <= stop:
                ep, er, ei = min(o_, stop), "hard_stop", j
                break
            if want == "short" and h_ >= stop:
                ep, er, ei = max(o_, stop), "hard_stop", j
                break
            peak = max(peak, ((h_ - entry) if want == "long" else (entry - l_)) / risk)
            cur = ((c_ - entry) if want == "long" else (entry - c_)) / risk
            # 2R 平一半
            if peak >= 2.0 and not half_done:
                half_done = True
            if not half_done and (j - i) >= MAXH:
                ep, er, ei = c_, "timeout", j
                break
            if half_done:
                # 新方案：再赚 1R 全平；旧方案：再赚 2R 全平
                need = 1.0 if mode == "mirror" else 2.0
                if (cur - 2.0) >= need:
                    ep, er, ei = c_, "half_tp", j
                    break
            if mode == "mirror":
                flip = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                    else (dirs[j] == 1 and dirs[j - 1] == -1)
                ha_ok = (hc[j] < ho[j] and hc[j] < zl[j]) if want == "long" \
                    else (hc[j] > ho[j] and hc[j] > zl[j])
                if flip and ha_ok:
                    if bp[j] is not None and bp[j] >= BBW_MIN:
                        ep, er, ei = c_, "mirror", j
                        break
            else:
                rev = (c_ < zl[j]) if want == "long" else (c_ > zl[j])
                ma_cnt = ma_cnt + 1 if rev else 0
                need_bars = 5 if peak < 2.0 else 2
                need_dd = min(peak * 0.5, 0.8) if 0.05 < peak < 2.0 else 0.8
                if ma_cnt >= need_bars and (peak - cur) >= need_dd:
                    ep, er, ei = c_, "ma_trail", j
                    break
        if ep is None:
            ei = n - 1
            ep = float(ks[-1]["close"])
            er = "eod"
        assert ei is not None
        g = (ep - entry) * sign
        r = (g - (entry + ep) * FEE) / risk
        if half_done:
            r = 0.5 * 2.0 + 0.5 * r
        out.append(dict(sym=sym, r=r, entry_ts=int(ks[i + 1].get("ts", i)),
                        exit_ts=int(ks[ei].get("ts", ei)), reason=er,
                        bars=ei - i))
        i = ei + 1
    return out


def load():
    c = []
    for fp in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "*_15m_60d.json"))):
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        try:
            c.append((sym, ks, _ce_zlsma(ks, 1, 2.0, 50, True, True)))
        except Exception:
            pass
    return c


def run_concurrent(all_trades, max_pos=MAX_CONCURRENT):
    """按时间顺序分配仓位，同一时刻最多 max_pos 个。"""
    all_trades.sort(key=lambda x: x["entry_ts"])
    held = []          # [(exit_ts, r)]
    taken = []
    skipped = 0
    for t in all_trades:
        # 先释放已到期的
        held = [(et, r) for et, r in held if et > t["entry_ts"]]
        if len(held) >= max_pos:
            skipped += 1
            continue
        held.append((t["exit_ts"], t["r"]))
        taken.append(t)
    return taken, skipped


def main():
    cache = load()
    print(f"载入 {len(cache)} 币\n")
    report = {}
    for mode, label in (("legacy", "旧方案 破均线+回撤 / 再赚2R"),
                        ("mirror", "新方案 镜像+布林带 / 再赚1R")):
        all_trades = []
        for sym, ks, ind in cache:
            try:
                all_trades.extend(gen_trades(sym, ks, ind, mode))
            except Exception as e:  # noqa: BLE001
                print(f"  ERR {sym}: {e}")
        # 无限仓口径
        n0 = len(all_trades)
        r0 = sum(x["r"] for x in all_trades)
        # 6 仓并发口径
        taken, skipped = run_concurrent(list(all_trades))
        n1 = len(taken)
        r1 = sum(x["r"] for x in taken)
        cum = pk = mdd = 0.0
        for x in sorted(taken, key=lambda z: z["exit_ts"]):
            cum += x["r"]
            pk = max(pk, cum)
            mdd = min(mdd, cum - pk)
        report[mode] = dict(n0=n0, r0=r0, n1=n1, r1=r1, skipped=skipped, mdd=mdd)

    print("=" * 104)
    print(f"{'方案':<34}{'无限仓笔数':>11}{'无限仓R':>11}{'6仓笔数':>10}{'6仓R':>10}{'被跳过':>9}{'6仓回撤':>10}")
    print("-" * 104)
    for mode, label in (("legacy", "旧方案 破均线+回撤 / 再赚2R"),
                        ("mirror", "新方案 镜像+布林带 / 再赚1R")):
        d = report[mode]
        print(f"{label:<34}{d['n0']:>11}{d['r0']:>+11.1f}{d['n1']:>10}{d['r1']:>+10.1f}"
              f"{d['skipped']:>9}{d['mdd']:>+10.1f}")
    print("\n" + "=" * 104)
    lo, mi = report["legacy"], report["mirror"]
    print(f"并发折损（无限仓 → 6仓）：旧 {lo['r0']:+.1f} → {lo['r1']:+.1f} "
          f"（保留 {lo['r1']/lo['r0']*100:.0f}%）")
    print(f"                          新 {mi['r0']:+.1f} → {mi['r1']:+.1f} "
          f"（保留 {mi['r1']/mi['r0']*100:.0f}%）")
    print(f"\n6 仓真实口径下 → 新方案比旧方案 {'多' if mi['r1'] > lo['r1'] else '少'}赚 "
          f"{abs(mi['r1'] - lo['r1']):.1f}R")


if __name__ == "__main__":
    main()
