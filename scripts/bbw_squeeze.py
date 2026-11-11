"""BBW 挤压 + 扩张触发 vs 现在的静态分位 70 —— 回测对比。

现在的做法（静态）：
    bbw_pct[i] >= 70        "现在宽不宽"

浩哥提的做法（挤压+扩张）：
    ① 过去 N 根里 BBW 分位最低点 < LOW            "刚经历过挤压"
    ② bbw_pct[i] - 最低点 >= RISE                 "已经从挤压里弹起来了"
    ③ bbw[i] > bbw[i-1]                          "还在继续张开"
    三条同时满足才通过

这是经典的布林带挤压突破（Squeeze Breakout）逻辑：
不看绝对宽度，看「从窄变宽」这个动作。

口径与线上同源：直接 import _ce_zlsma，use_close=True，
2R 平半 + 镜像出场 + 16h + 硬止损 + fee 0.0005。

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/bbw_squeeze.py
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma  # noqa: E402

FEE = 0.0005
SWING = 20
MAXH = 64
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


# ─────────────────────────────────────────────────────────────
# 闸门函数：传入 (bbw, bbw_pct, i)，返回 True 表示通过
# ─────────────────────────────────────────────────────────────
def gate_static_70(bbw, bp, i):
    """现在线上的做法。"""
    return bp[i] is not None and bp[i] >= 70.0


def gate_none(bbw, bp, i):
    """不过滤，用来当基准（不加这道闸会怎样）。"""
    return True


def make_squeeze_gate(win=5, low_thr=30.0, rise=30.0, need_rising=True):
    """挤压 + 扩张触发。

    win       看过去几根找低点
    low_thr   低点必须低于这个分位才算「挤压过」
    rise      从低点弹起来多少点才算「扩张」
    need_rising  要求当根带宽 > 上一根带宽
    """
    def gate(bbw, bp, i):
        if i < win:
            return False
        lo = i - win + 1
        seg = [bp[j] for j in range(lo, i + 1)]
        if any(v is None for v in seg) or bp[i] is None:
            return False
        low_point = min(seg)
        # ① 过去 win 根里出现过挤压
        if low_point >= low_thr:
            return False
        # ② 已经从挤压里弹起来足够多
        if bp[i] - low_point < rise:
            return False
        # ③ 还在继续张开
        if need_rising:
            if bbw[i] is None or bbw[i - 1] is None or bbw[i] <= bbw[i - 1]:
                return False
        return True
    return gate


def make_and_gate(win=5, low_thr=30.0, rise=30.0, static_thr=70.0):
    """组合：既要求现在够宽（静态 >=70），又要求刚从挤压里释放出来。"""
    sq = make_squeeze_gate(win, low_thr, rise, True)

    def gate(bbw, bp, i):
        if bp[i] is None or bp[i] < static_thr:
            return False
        return sq(bbw, bp, i)
    return gate


def simulate_all(ks, ind, gate):
    """与 s15_live_parity.simulate 同源，BBW 判断换成注入的 gate。"""
    ho, hc = ind["open"], ind["close"]
    zl, dirs = ind["zlsma"], ind["dir"]
    bbw, bp = ind["bbw"], ind["bbw_pct"]
    hi_raw, lo_raw = ind["high_raw"], ind["low_raw"]
    n = len(hc)
    out = []
    i = max(1, 60, SWING)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None:
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
        if not gate(bbw, bp, i):
            i += 1
            continue

        entry = float(ks[i + 1]["open"])
        if entry <= 0:
            i += 1
            continue
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
            if peak >= 2.0 and not half_done:
                half_done = True
            if not half_done and (j - i) >= MAXH:
                ep, er, ei = c_, "timeout", j
                break
            if half_done and (cur - 2.0) >= 1.0:
                ep, er, ei = c_, "half_tp", j
                break
            flip = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                else (dirs[j] == 1 and dirs[j - 1] == -1)
            ha_ok = (hc[j] < ho[j] and hc[j] < zl[j]) if want == "long" \
                else (hc[j] > ho[j] and hc[j] > zl[j])
            if flip and ha_ok:
                ep, er, ei = c_, "mirror", j
                break
        if ep is None:
            ei = n - 1
            ep = float(ks[-1]["close"])
            er = "eod"
        assert ep is not None and er is not None
        g = (ep - entry) * sign
        r = (g - (entry + ep) * FEE) / risk
        if half_done:
            r = 0.5 * 2.0 + 0.5 * r
        out.append(dict(r=r, bars=ei - i, reason=er,
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
    print(f"载入 {len(cache)} 币 × 60 天 15m")
    print()

    inds = {sym: _ce_zlsma(ks, 1, 2.0, 50, True, True) for sym, ks in cache}

    def run(gate):
        allt = []
        for sym, ks in cache:
            allt.extend(simulate_all(ks, inds[sym], gate))
        return stats(allt)

    print("=" * 100)
    print("方案对比（同一批币、同一段行情、同一套出场）")
    print("=" * 100)
    print(f"{'方案':<34}{'笔数':>7}{'净R':>10}{'均笔R':>10}{'胜率':>8}{'回撤':>10}{'均持仓':>9}")
    print("-" * 100)

    rows = [
        ("不过滤（基准，不加这道闸）", run(gate_none)),
        ("现在：静态分位 >= 70", run(gate_static_70)),
        ("浩哥方案：挤压+扩张 (5/30/30)", run(make_squeeze_gate(5, 30.0, 30.0, True))),
        ("组合：静态70 且 挤压+扩张", run(make_and_gate(5, 30.0, 30.0, 70.0))),
        ("组合：静态70 且 挤压+扩张(涨幅40)", run(make_and_gate(5, 30.0, 40.0, 70.0))),
    ]
    for label, s in rows:
        print(f"{label:<34}{s['n']:>7}{s['r']:>+10.1f}{s['avg']:>+10.3f}"
              f"{s['win']:>7.1f}%{s['mdd']:>+10.1f}{s['bars']:>9.1f}")
    print()

    # ── 参数敏感度 ─────────────────────────────────────────
    print("=" * 100)
    print("挤压+扩张 参数敏感度（窗口 / 挤压低点阈值 / 反弹幅度）")
    print("=" * 100)
    print(f"{'窗口':>5}{'低点<':>7}{'涨幅>=':>9}{'笔数':>7}{'净R':>10}{'均笔R':>10}{'胜率':>8}{'回撤':>10}")
    print("-" * 100)
    for win in (3, 5, 8, 10):
        for low_thr in (20.0, 30.0, 40.0):
            for rise in (20.0, 30.0, 40.0):
                s = run(make_squeeze_gate(win, low_thr, rise, True))
                mark = "  <-" if (win == 5 and low_thr == 30.0 and rise == 30.0) else ""
                print(f"{win:>5}{low_thr:>7.0f}{rise:>9.0f}{s['n']:>7}{s['r']:>+10.1f}"
                      f"{s['avg']:>+10.3f}{s['win']:>7.1f}%{s['mdd']:>+10.1f}{mark}")
    print()

    # ── 去掉③（不要求当根继续张开）看影响 ─────────────────────
    print("=" * 100)
    print("去掉③「当根带宽 > 上一根」会怎样")
    print("=" * 100)
    a = run(make_squeeze_gate(5, 30.0, 30.0, True))
    b = run(make_squeeze_gate(5, 30.0, 30.0, False))
    print(f"  带③（浩哥方案）  {a['n']:>5} 笔  净 {a['r']:>+8.1f}R  均笔 {a['avg']:>+.3f}R  胜率 {a['win']:.1f}%")
    print(f"  去掉③           {b['n']:>5} 笔  净 {b['r']:>+8.1f}R  均笔 {b['avg']:>+.3f}R  胜率 {b['win']:.1f}%")
    print()
    print("=" * 100)
    print("结论")
    print("=" * 100)
    base = rows[1][1]
    new = rows[2][1]
    print(f"  现在（>=70）     {base['n']:>5} 笔  净 {base['r']:>+8.1f}R  均笔 {base['avg']:>+.3f}R  胜率 {base['win']:.1f}%  回撤 {base['mdd']:+.1f}")
    print(f"  挤压+扩张        {new['n']:>5} 笔  净 {new['r']:>+8.1f}R  均笔 {new['avg']:>+.3f}R  胜率 {new['win']:.1f}%  回撤 {new['mdd']:+.1f}")
    print()
    print(f"  笔数  {new['n'] - base['n']:+d} ({new['n'] / max(1, base['n']) * 100 - 100:+.0f}%)")
    print(f"  净R   {new['r'] - base['r']:+.1f}")
    print(f"  均笔  {new['avg'] - base['avg']:+.3f}")
    print(f"  胜率  {new['win'] - base['win']:+.1f} 个百分点")
    print(f"  回撤  {new['mdd'] - base['mdd']:+.1f}（越接近 0 越好）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
