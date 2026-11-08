"""BBW 到底在防「假突破」还是「假方向」—— 按 MFE 分类看出场原因。

先分清两个概念：
  假突破（false breakout）：价格穿过了关键位，但没延续，很快回来。
                            位置概念 —— 穿过去了但站不住。
  假方向（false direction）：指标判了方向，市场走的是另一边。
                            方向概念 —— 判断本身错了。

S15 里没有传统区间突破。CE 翻转是「价格穿过 Chandelier Exit 动态止损线」，
所以两者在代码层面是同一次翻转。要分开，只能看「进场后价格先走了多远」：

  MFE < 0.3R   进场就错，几乎没赚过   → 典型假方向（判反了）
  0.3~1.0R     走了一点就回来         → 中间地带
  MFE >= 1.0R  走对了方向但没吃到     → 方向对，被更大的反向行情吃掉
  （再配最终盈亏看出场原因）

按 BBW 分位分组，看两组在这几类里各占多少 —— 就能看出 BBW 真正在拦哪一类。

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/bbw_failure_mode.py
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
MAX_RISK_PCT = 30.0


def simulate_all(ks, ind):
    """同源回测，不设 BBW 门槛，记录 MFE 和 BBW 分位。"""
    ho, hc = ind["open"], ind["close"]
    zl, dirs, bp = ind["zlsma"], ind["dir"], ind["bbw_pct"]
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
        bbw_at = bp[i]

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
        out.append(dict(r=r, mfe=peak, bars=ei - i, reason=er, bbw=bbw_at,
                        exit_ts=int(ks[ei].get("ts", ei))))
        i = ei + 1
    return out


def classify(t):
    """把一笔交易归到失败模式里。"""
    if t["r"] > 0:
        return "赚钱"
    if t["mfe"] < 0.3:
        return "进场就错"          # 假方向：判反了，几乎没赚过
    if t["mfe"] < 1.0:
        return "走一点就回"        # 假突破：动了一下没延续
    return "方向对但被吃"          # 走对了 1R 以上还亏


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    cache = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) >= 800:
            cache.append((sym, ks))
    print(f"载入 {len(cache)} 币 × 60 天 15m")
    print()

    allt = []
    for sym, ks in cache:
        allt.extend(simulate_all(ks, _ce_zlsma(ks, 1, 2.0, 50, True, True)))

    valid = [t for t in allt if t["bbw"] is not None]
    print(f"加 BBW 闸之前，全部信号 {len(valid)} 笔")
    print()

    lo = [t for t in valid if t["bbw"] < 70]
    hi = [t for t in valid if t["bbw"] >= 70]

    cats = ["赚钱", "进场就错", "走一点就回", "方向对但被吃"]

    print("=" * 96)
    print("失败模式分布（按进场时 BBW 分位分两组）")
    print("=" * 96)
    print(f"{'类别':<18}{'BBW<70（被拦）':>22}{'BBW>=70（放行）':>22}{'差异':>16}")
    print("-" * 96)

    def pct(grp, cat):
        if not grp:
            return 0.0
        return sum(1 for t in grp if classify(t) == cat) / len(grp) * 100

    for cat in cats:
        a, b = pct(lo, cat), pct(hi, cat)
        print(f"{cat:<18}{a:>20.1f}%{b:>21.1f}%{b - a:>+15.1f}pp")
    print("-" * 96)
    print(f"{'笔数':<18}{len(lo):>20}{len(hi):>21}")

    print()
    print("=" * 96)
    print("同一批数据的出场原因（已含半仓逻辑）")
    print("=" * 96)
    for label, grp in (("BBW<70  被拦", lo), ("BBW>=70 放行", hi)):
        if not grp:
            continue
        cnt = {}
        for t in grp:
            cnt[t["reason"]] = cnt.get(t["reason"], 0) + 1
        tot = len(grp)
        parts = [f"{k} {v / tot * 100:.0f}%" for k, v in sorted(cnt.items(), key=lambda x: -x[1])]
        print(f"  {label:<14} {' / '.join(parts)}")

    print()
    print("=" * 96)
    print("每种失败模式的平均亏损（看 BBW 拦掉的是「深亏」还是「浅亏」）")
    print("=" * 96)
    print(f"{'类别':<18}{'BBW<70 均亏':>16}{'BBW>=70 均亏':>16}{'BBW<70 笔数':>14}{'BBW>=70 笔数':>14}")
    print("-" * 96)
    for cat in cats[1:]:
        a = [t["r"] for t in lo if classify(t) == cat]
        b = [t["r"] for t in hi if classify(t) == cat]
        am = f"{sum(a) / len(a):+.3f}R" if a else "-"
        bm = f"{sum(b) / len(b):+.3f}R" if b else "-"
        print(f"{cat:<18}{am:>16}{bm:>16}{len(a):>14}{len(b):>14}")

    print()
    print("=" * 96)
    print("MFE 分布（进场后最多赚到几个 R）")
    print("=" * 96)
    buckets = [(0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99)]
    print(f"{'MFE 区间':<14}{'BBW<70 占比':>16}{'BBW>=70 占比':>16}")
    print("-" * 96)
    for a, b in buckets:
        la = sum(1 for t in lo if a <= t["mfe"] < b) / max(1, len(lo)) * 100
        lb = sum(1 for t in hi if a <= t["mfe"] < b) / max(1, len(hi)) * 100
        label = f"{a:.1f}~{b:.1f}R" if b < 99 else "2.0R+"
        print(f"{label:<14}{la:>15.1f}%{lb:>15.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
