"""「挤压+扩张」这道闸，拦掉的主要是假突破还是错方向？

方法：对每一个 CE 翻转 + HA 关键K线的信号，不管过不过闸，都：
  1. 算出它在「静态 70」和「挤压+扩张」两道闸下分别过不过
  2. 完整模拟出场，得到 MFE（进场后最大有利偏移）和最终 R
  3. 按 MFE 归类成失败模式
  4. 分别看两道闸「通过组 / 拒绝组」的失败模式分布

四类失败模式：
  赚钱            r > 0
  进场就错        MFE < 0.3R          假方向（判反了）
  走一点就回      0.3 <= MFE < 1.0R   假突破（动了没延续）
  方向对但被吃    MFE >= 1.0R 还亏     走对了被更大反向行情吃掉

「挤压+扩张」的判定（浩哥 2026-11-08 定）：
  ① 过去 5 根里 BBW 分位最低点 < 30      挤压过
  ② 当前分位 - 最低点 >= 30              已经弹起来
  ③ 当根带宽 > 上一根带宽                还在张开

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/squeeze_failure_mode.py
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

SQ_WIN = 5
SQ_LOW = 30.0
SQ_RISE = 30.0
STATIC_THR = 70.0


def squeeze_pass(bp, bbw, i):
    """挤压 + 扩张触发。"""
    if i < SQ_WIN:
        return False
    seg = [bp[j] for j in range(i - SQ_WIN + 1, i + 1)]
    if any(v is None for v in seg) or bp[i] is None:
        return False
    low = min(seg)
    if low >= SQ_LOW:                       # ① 没挤压过
        return False
    if bp[i] - low < SQ_RISE:               # ② 没弹够
        return False
    if bbw[i] is None or bbw[i - 1] is None or bbw[i] <= bbw[i - 1]:  # ③ 不在张开
        return False
    return True


def scan_signals(ks, ind):
    """扫出所有信号，记录两道闸的通过情况 + 完整模拟结果。"""
    ho, hc = ind["open"], ind["close"]
    zl, dirs, bp = ind["zlsma"], ind["dir"], ind["bbw_pct"]
    bbw = ind["bbw"]
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

        st_pass = bp[i] is not None and bp[i] >= STATIC_THR
        sq_pass = squeeze_pass(bp, bbw, i)
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
                        st=st_pass, sq=sq_pass, exit_ts=int(ks[ei].get("ts", ei))))
        i = ei + 1
    return out


def classify(t):
    if t["r"] > 0:
        return "赚钱"
    if t["mfe"] < 0.3:
        return "进场就错"
    if t["mfe"] < 1.0:
        return "走一点就回"
    return "方向对但被吃"


CATS = ["赚钱", "进场就错", "走一点就回", "方向对但被吃"]


def mode_table(name, passed, rejected, allc):
    """打印一道闸的通过组 vs 拒绝组对比。"""
    print()
    print("=" * 100)
    print(f"【{name}】")
    print("=" * 100)
    print(f"{'类别':<18}{'通过':>12}{'拒绝':>12}{'全部':>12}{'通过-拒绝':>14}")
    print("-" * 100)

    def pc(grp, cat):
        return sum(1 for t in grp if classify(t) == cat) / len(grp) * 100 if grp else 0.0

    for cat in CATS:
        a, b, c = pc(passed, cat), pc(rejected, cat), pc(allc, cat)
        print(f"{cat:<18}{a:>11.1f}%{b:>11.1f}%{c:>11.1f}%{a - b:>+13.1f}pp")
    print("-" * 100)
    print(f"{'笔数':<18}{len(passed):>12}{len(rejected):>12}{len(allc):>12}")
    print()
    print(f"  {name} 收下了 {len(passed)} 笔，拒绝 {len(rejected)} 笔"
          f"（保留率 {len(passed) / max(1, len(allc)) * 100:.1f}%）")


def explain(name, passed, rejected):
    """把改善拆到具体失败模式上，算各自贡献占比。"""
    def pc(grp, cat):
        return sum(1 for t in grp if classify(t) == cat) / len(grp) * 100 if grp else 0.0

    gain_win = pc(passed, "赚钱") - pc(rejected, "赚钱")
    cut_fb = pc(rejected, "走一点就回") - pc(passed, "走一点就回")
    cut_fd = pc(rejected, "进场就错") - pc(passed, "进场就错")
    cut_eat = pc(rejected, "方向对但被吃") - pc(passed, "方向对但被吃")
    total = cut_fb + cut_fd + cut_eat
    print()
    print(f"  ── {name}：通过组比拒绝组好 {gain_win:+.1f}pp（赚钱比例）")
    if total <= 0.01:
        print("     拒绝组的各类失败并不比通过组多，这道闸几乎没在区别什么")
        return
    print(f"     拆开看它多拦掉的失败：")
    print(f"       假突破（走一点就回）  {cut_fb:>+6.1f}pp  占 {cut_fb / total * 100:>5.1f}%")
    print(f"       假方向（进场就错）    {cut_fd:>+6.1f}pp  占 {cut_fd / total * 100:>5.1f}%")
    print(f"       方向对但被吃          {cut_eat:>+6.1f}pp  占 {cut_eat / total * 100:>5.1f}%")
    if cut_fb > cut_fd * 1.5:
        print(f"     → 主要是防【假突破】")
    elif cut_fd > cut_fb * 1.5:
        print(f"     → 主要是防【错方向】")
    else:
        print(f"     → 【两者都在防】，没有明显侧重")


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
        allt.extend(scan_signals(ks, _ce_zlsma(ks, 1, 2.0, 50, True, True)))
    allt = [t for t in allt if t["bbw"] is not None]
    print(f"全部信号 {len(allt)} 笔（不管过不过闸）")
    print()

    st_p = [t for t in allt if t["st"]]
    st_r = [t for t in allt if not t["st"]]
    sq_p = [t for t in allt if t["sq"]]
    sq_r = [t for t in allt if not t["sq"]]

    mode_table("静态分位 >= 70", st_p, st_r, allt)
    mode_table("挤压+扩张 (5/30/30)", sq_p, sq_r, allt)

    print()
    print("=" * 100)
    print("两道闸到底各防什么（改善拆解）")
    print("=" * 100)
    explain("静态 >= 70", st_p, st_r)
    explain("挤压+扩张", sq_p, sq_r)

    # 两组重叠情况
    both = [t for t in allt if t["st"] and t["sq"]]
    only_sq = [t for t in allt if t["sq"] and not t["st"]]
    only_st = [t for t in allt if t["st"] and not t["sq"]]
    neither = [t for t in allt if not t["st"] and not t["sq"]]
    print()
    print("=" * 100)
    print("两道闸的关系（它们拦的是同一批信号吗）")
    print("=" * 100)
    for label, grp in (("两道都过", both), ("只过挤压", only_sq),
                       ("只过静态", only_st), ("两道都拒", neither)):
        if not grp:
            print(f"  {label:<10}   0 笔")
            continue
        avg = sum(t["r"] for t in grp) / len(grp)
        win = sum(1 for t in grp if t["r"] > 0) / len(grp) * 100
        print(f"  {label:<10}{len(grp):>5} 笔   均笔 {avg:+.3f}R   胜率 {win:>5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
