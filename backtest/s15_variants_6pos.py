"""线上口径 + 6 仓真实约束 —— 出场方案全变体对比

这是唯一有决策价值的测试：
  · 指标用线上 _ce_zlsma（use_close=true，与 strategies.yaml 一致）
  · 仓位上限按 max_concurrent_coins=6 真实模拟
  · 每个变体都跑同一个宇宙、同一份数据

为什么要这样：「无限仓」口径下 744 笔全吃得到，实盘一天最多同时持 6 个，
持仓越长周转越慢，信号被挡在门外的越多。只有 6 仓口径能反映实盘。

用法：python backtest/s15_variants_6pos.py
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma

FEE = 0.0005
SWING = 20
BBW_MIN = 70.0
MAXH = 64
MAX_RISK_PCT = 30.0
MAX_POS = 6


def simulate(ks, ind, cfg):
    ho, hc = ind["open"], ind["close"]
    zl, dirs = ind["zlsma"], ind["dir"]
    bp = ind["bbw_pct"]
    hi_raw, lo_raw = ind["high_raw"], ind["low_raw"]
    n = len(hc)
    out = []
    i = max(1, 60, SWING)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None or bp[i] is None:
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
        if not key or bp[i] < BBW_MIN:
            i += 1
            continue
        entry = float(ks[i + 1]["open"])
        if entry <= 0:
            i += 1
            continue
        lo = max(0, i - SWING + 1)
        stop = (min(lo_raw[j] for j in range(lo, i + 1)) if want == "long"
                else max(hi_raw[j] for j in range(lo, i + 1)))
        risk = (entry - stop) if want == "long" else (stop - entry)
        if risk <= 0 or risk / entry * 100 > MAX_RISK_PCT:
            i += 1
            continue

        sign = 1 if want == "long" else -1
        peak = 0.0
        half_done = False
        ma_cnt = 0
        ep = er = ei = None
        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            if want == "long" and l_ <= stop:
                ep, er, ei = min(o_, stop), "stop", j
                break
            if want == "short" and h_ >= stop:
                ep, er, ei = max(o_, stop), "stop", j
                break
            peak = max(peak, ((h_ - entry) if want == "long" else (entry - l_)) / risk)
            cur = ((c_ - entry) if want == "long" else (entry - c_)) / risk
            ha = cfg["half_at"]
            if ha > 0 and peak >= ha and not half_done:
                half_done = True
            if not half_done and (j - i) >= MAXH:
                ep, er, ei = c_, "timeout", j
                break
            if half_done and cfg["half_tp"] > 0 and (cur - ha) >= cfg["half_tp"]:
                ep, er, ei = c_, "half_tp", j
                break
            # 「不半仓、到 N R 整仓一次走」——与半仓是互斥的两种落袋方式
            tp_all = cfg.get("tp_all", 0.0)
            if tp_all > 0 and peak >= tp_all:
                ep, er, ei = c_, "tp_all", j
                break
            if cfg["mirror"]:
                flip = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                    else (dirs[j] == 1 and dirs[j - 1] == -1)
                ha_ok = (hc[j] < ho[j] and hc[j] < zl[j]) if want == "long" \
                    else (hc[j] > ho[j] and hc[j] > zl[j])
                ok_bp = (not cfg["bbw_exit"]) or (bp[j] is not None and bp[j] >= BBW_MIN)
                if flip and ha_ok and ok_bp:
                    ep, er, ei = c_, "mirror", j
                    break
            else:
                rev = (c_ < zl[j]) if want == "long" else (c_ > zl[j])
                ma_cnt = ma_cnt + 1 if rev else 0
                nb = 5 if peak < 2.0 else 2
                nd = min(peak * 0.5, 0.8) if 0.05 < peak < 2.0 else 0.8
                if ma_cnt >= nb and (peak - cur) >= nd:
                    ep, er, ei = c_, "ma_trail", j
                    break
        if ep is None:
            ei, ep, er = n - 1, float(ks[-1]["close"]), "eod"
        g = (ep - entry) * sign
        r = (g - (entry + ep) * FEE) / risk
        if half_done:
            r = 0.5 * cfg["half_at"] + 0.5 * r
        out.append(dict(r=r, bars=ei - i,
                        entry_ts=int(ks[i + 1].get("ts", i)),
                        exit_ts=int(ks[ei].get("ts", ei))))
        i = ei + 1
    return out


VARIANTS = [
    ("① 旧 破均线+回撤 / 2R平半+再赚2R", dict(mirror=False, bbw_exit=False, half_at=2.0, half_tp=2.0)),
    ("② 镜像+BBW / 2R平半+再赚1R【当前线上】", dict(mirror=True, bbw_exit=True, half_at=2.0, half_tp=1.0)),
    ("③ 镜像+BBW / 平地2R全平", dict(mirror=True, bbw_exit=True, half_at=0.0, tp_all=2.0)),
    ("④ 镜像无BBW / 平地2R全平", dict(mirror=True, bbw_exit=False, half_at=0.0, tp_all=2.0)),
    ("⑤ 旧破均线 / 2R平半+再赚1R", dict(mirror=False, bbw_exit=False, half_at=2.0, half_tp=1.0)),
    ("⑥ 镜像+BBW / 平半@1.5R + 再赚1R", dict(mirror=True, bbw_exit=True, half_at=1.5, half_tp=1.0)),
    ("⑦ 镜像无BBW / 平半@2R + 再赚1R", dict(mirror=True, bbw_exit=False, half_at=2.0, half_tp=1.0)),
    ("⑧ 镜像+BBW / 平半@2R + 再赚0.5R", dict(mirror=True, bbw_exit=True, half_at=2.0, half_tp=0.5)),
    ("⑨ 旧破均线 / 平地2R全平", dict(mirror=False, bbw_exit=False, half_at=0.0, tp_all=2.0)),
    ("⑩ 纯镜像出场 / 不设任何R止盈", dict(mirror=True, bbw_exit=False, half_at=0.0)),
]


def run6(trades, max_pos=MAX_POS):
    trades = sorted(trades, key=lambda x: x["entry_ts"])
    held, taken = [], []
    skipped = 0
    for t in trades:
        held = [(et, r) for et, r in held if et > t["entry_ts"]]
        if len(held) >= max_pos:
            skipped += 1
            continue
        held.append((t["exit_ts"], t["r"]))
        taken.append(t)
    return taken, skipped


def stats(ts):
    if not ts:
        return dict(n=0, r=0.0, avg=0.0, win=0.0, mdd=0.0, bars=0.0)
    r = sum(t["r"] for t in ts)
    w = sum(1 for t in ts if t["r"] > 0) / len(ts) * 100
    cum = pk = mdd = 0.0
    for t in sorted(ts, key=lambda z: z["exit_ts"]):
        cum += t["r"]
        pk = max(pk, cum)
        mdd = min(mdd, cum - pk)
    return dict(n=len(ts), r=r, avg=r / len(ts), win=w, mdd=mdd,
                bars=sum(t["bars"] for t in ts) / len(ts))


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    cache = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        cache.append((sym, ks, _ce_zlsma(ks, 1, 2.0, 50, True, True)))
    print(f"载入 {len(cache)} 币（线上口径 use_close=true）\n")

    print("=" * 112)
    print(f"{'出场方案':<36}{'无限仓':>8}{'无限R':>9}{'':>3}{'6仓笔数':>8}{'6仓R':>9}{'均笔R':>8}{'胜率':>7}{'6仓回撤':>9}{'持仓根':>7}")
    print("-" * 112)
    results = []
    for label, cfg in VARIANTS:
        allt = []
        for sym, ks, ind in cache:
            allt.extend(simulate(ks, ind, cfg))
        s0 = stats(allt)
        taken, _ = run6(list(allt))
        s6 = stats(taken)
        results.append((label, s0, s6))
        print(f"{label:<36}{s0['n']:>8}{s0['r']:>+9.1f}{'':>3}{s6['n']:>8}{s6['r']:>+9.1f}"
              f"{s6['avg']:>+8.3f}{s6['win']:>6.1f}%{s6['mdd']:>+9.1f}{s6['bars']:>7.1f}")
    print("=" * 112)

    best = max(results, key=lambda x: x[2]["r"])
    print(f"\n6 仓真实口径最优：{best[0]}")
    print(f"  {best[2]['n']} 笔 / {best[2]['r']:+.1f}R / 均笔 {best[2]['avg']:+.3f} / "
          f"胜率 {best[2]['win']:.1f}% / 回撤 {best[2]['mdd']:+.1f} / 均持仓 {best[2]['bars']:.1f} 根")
    return 0


if __name__ == "__main__":
    sys.exit(main())
