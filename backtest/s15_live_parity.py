"""与线上 100% 同源的出场回测 —— 直接调用 backend.strategies._ce_zlsma

为什么要有这个脚本：
  backtest/s15_final.py 的 ce_dir() 用 HA 的 high/low 取极值，
  而线上 strategies.yaml 写着 use_close: true（用 HA 收盘价取极值）。
  两者 CE 方向约有 10% 的 K 线不同 → 之前所有回测结论都不是线上口径。
  （2026-11-08 对账发现，这是第三次「回测口径与线上不一致」）

  这个脚本不再自己实现任何指标，直接 import 线上那个函数。
  线上怎么算，回测就怎么算，口径差异从根上消失。

同时跑 use_close=True / False 两套，量化这个参数到底值多少钱。

用法：python backtest/s15_live_parity.py
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma      # ← 线上同一份实现

FEE = 0.0005
SWING = 20
BBW_MIN = 70.0
MAXH = 64              # 16 小时 / 15m
# ★ 2026-11-11 浩哥定 B 方案：从 strategies.yaml 读，回测口径永远跟线上一致。
#   原来是写死 30.0，而线上 max_risk_pct = 5.0 —— 用它跑出来的结果
#   （放行了止损 5%~30% 的单子）不代表真实表现，跨期比较时是陷阱。
def _yaml_max_risk_pct(default=5.0):
    """读 strategies.yaml 的 max_risk_pct；读不到就退回 5.0（线上值）。"""
    try:
        import yaml as _y
        with open(os.path.join(ROOT, "strategies.yaml"), encoding="utf-8") as _f:
            _n = _y.safe_load(_f)["strategies"]["S15_CE_ZLSMA"]
        return float(_n.get("max_risk_pct") or default)
    except Exception:                                   # noqa: BLE001
        return default


MAX_RISK_PCT = _yaml_max_risk_pct()


def simulate(ks, ind, want_mirror=True, use_bbw=True,
             half_at_r=2.0, half_tp_r=1.0, bbw_thr=BBW_MIN,
             trail_pct=0.5, trail_r=0.8):
    """单币逐笔回测。指标全部来自传入的 ind（线上 _ce_zlsma 的输出）。"""
    ho, hc = ind["open"], ind["close"]
    zl, dirs, atr, bp = ind["zlsma"], ind["dir"], ind["atr"], ind["bbw_pct"]
    hi_raw = ind["high_raw"]
    lo_raw = ind["low_raw"]
    n = len(hc)
    out = []
    i = max(1, 60, SWING)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None or atr[i] is None:
            i += 1
            continue
        # 入场：CE 方向翻转（事件）+ HA 关键K线同侧 + BBW 分位
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
        if bp[i] is None or bp[i] < bbw_thr:
            i += 1
            continue
        entry = float(ks[i + 1]["open"])
        if entry <= 0:
            i += 1
            continue
        # 止损：前 SWING 根真实高低点（用原始 K 线，不是 HA）
        lo = max(0, i - SWING + 1)
        s_lo = min(lo_raw[j] for j in range(lo, i + 1))
        s_hi = max(hi_raw[j] for j in range(lo, i + 1))
        stop = s_lo if want == "long" else s_hi
        risk = (entry - stop) if want == "long" else (stop - entry)
        if risk <= 0 or risk / entry * 100 > MAX_RISK_PCT:
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
            # ① 硬止损（用原始价，HA 价实盘成交不了）
            if want == "long" and l_ <= stop:
                ep, er, ei = min(o_, stop), "hard_stop", j
                break
            if want == "short" and h_ >= stop:
                ep, er, ei = max(o_, stop), "hard_stop", j
                break
            peak = max(peak, ((h_ - entry) if want == "long" else (entry - l_)) / risk)
            cur = ((c_ - entry) if want == "long" else (entry - c_)) / risk
            # ② 2R 平一半
            if peak >= half_at_r and not half_done:
                half_done = True
            # ③ 16h 超时（平过半豁免）
            if not half_done and (j - i) >= MAXH:
                ep, er, ei = c_, "timeout", j
                break
            # ④ 半仓后：再赚 half_tp_r 全平
            #    1e-9 容差：2.8-1.8 在 float 下是 0.9999999999999996，
            #    不加容差会漏掉「恰好到止盈线」的单子（线上 strategies.py 已同步修）
            if half_done and half_tp_r > 0 and (cur - half_at_r) >= half_tp_r - 1e-9:
                ep, er, ei = c_, "half_tp", j
                break
            if want_mirror:
                # ⑤ 镜像出场：CE 翻转 + HA 反向关键K线站错边 + BBW
                flip = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                    else (dirs[j] == 1 and dirs[j - 1] == -1)
                ha_ok = (hc[j] < ho[j] and hc[j] < zl[j]) if want == "long" \
                    else (hc[j] > ho[j] and hc[j] > zl[j])
                ok_bp = (not use_bbw) or (bp[j] is not None and bp[j] >= bbw_thr)
                if flip and ha_ok and ok_bp:
                    ep, er, ei = c_, "mirror", j
                    break
            else:
                # 旧：破均线 + 回撤
                rev = (c_ < zl[j]) if want == "long" else (c_ > zl[j])
                ma_cnt = ma_cnt + 1 if rev else 0
                need_bars = 5 if peak < 2.0 else 2
                need_dd = min(peak * trail_pct, 0.8) if 0.05 < peak < 2.0 else trail_r
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
            r = 0.5 * half_at_r + 0.5 * r
        out.append(dict(sym=None, side=want, r=r, bars=ei - i, reason=er,
                        entry_ts=int(ks[i + 1].get("ts", i)),
                        exit_ts=int(ks[ei].get("ts", ei))))
        i = ei + 1
    return out


def stats(trades):
    if not trades:
        return dict(n=0, r=0.0, avg=0.0, win=0.0, mdd=0.0, bars=0.0)
    r = sum(t["r"] for t in trades)
    w = sum(1 for t in trades if t["r"] > 0) / len(trades) * 100
    cum = pk = mdd = 0.0
    for t in sorted(trades, key=lambda z: z["exit_ts"]):
        cum += t["r"]
        pk = max(pk, cum)
        mdd = min(mdd, cum - pk)
    return dict(n=len(trades), r=r, avg=r / len(trades), win=w, mdd=mdd,
                bars=sum(t["bars"] for t in trades) / len(trades))


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    cache = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        cache.append((sym, ks))
    print(f"载入 {len(cache)} 币\n")

    # 预计算两种口径的指标（线上函数，唯一实现）
    inds = {}
    for uc in (True, False):
        inds[uc] = {}
        for sym, ks in cache:
            inds[uc][sym] = _ce_zlsma(ks, 1, 2.0, 50, uc, True)

    rows = []
    for uc in (True, False):
        base = []
        mirror = []
        for sym, ks in cache:
            ind = inds[uc][sym]
            base.extend(simulate(ks, ind, want_mirror=False, use_bbw=False))
            mirror.extend(simulate(ks, ind, want_mirror=True, use_bbw=True))
        rows.append((uc, stats(base), stats(mirror)))

    print("=" * 108)
    print(f"{'口径':<26}{'方案':<18}{'笔数':>7}{'净R':>10}{'均笔R':>9}{'胜率':>8}{'回撤':>10}{'均持仓根':>10}")
    print("-" * 108)
    for uc, b, m in rows:
        label = "use_close=True(线上)" if uc else "use_close=False(旧回测)"
        print(f"{label:<26}{'旧 破均线+回撤':<18}{b['n']:>7}{b['r']:>+10.1f}{b['avg']:>+9.3f}"
              f"{b['win']:>7.1f}%{b['mdd']:>+10.1f}{b['bars']:>10.1f}")
        print(f"{'':<26}{'新 镜像+布林带':<18}{m['n']:>7}{m['r']:>+10.1f}{m['avg']:>+9.3f}"
              f"{m['win']:>7.1f}%{m['mdd']:>+10.1f}{m['bars']:>10.1f}")
        print("-" * 108)

    _uc_t, ut, mt = rows[0]
    _uc_f, uf, mf = rows[1]
    print("\n【线上口径下，新方案 vs 旧方案】")
    print(f"  净R      {mt['r']:+.1f}  vs  {ut['r']:+.1f}   （{'多' if mt['r']>ut['r'] else '少'}赚 {abs(mt['r']-ut['r']):.1f}R）")
    print(f"  均笔R    {mt['avg']:+.3f}  vs  {ut['avg']:+.3f}")
    print(f"  胜率     {mt['win']:.1f}%  vs  {ut['win']:.1f}%")
    print(f"  最大回撤 {mt['mdd']:+.1f}  vs  {ut['mdd']:+.1f}")
    print(f"  均持仓   {mt['bars']:.1f}根  vs  {ut['bars']:.1f}根")
    print("\n【use_close 这个参数本身值多少钱（同为新方案）】")
    print(f"  True(线上)  {mt['r']:+.1f}R / {mt['n']} 笔")
    print(f"  False(回测) {mf['r']:+.1f}R / {mf['n']} 笔")
    print(f"  差异        {mt['r']-mf['r']:+.1f}R")
    return 0


if __name__ == "__main__":
    sys.exit(main())
