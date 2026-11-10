"""浩哥 2026-11-08 出场方案回测

方案（他的原话）：
  1. 超时：持仓满 16 小时（平过半的豁免）
  2. 硬止损：价格碰到开仓时算的止损价（多=前20根最低点，空=前20根最高点）
  3. S15 核心：跟多空开仓镜像
       做多时 ① CE 方向翻转 ② HA 阴线站在 ZLSMA50 下方
       做空时 ① CE 方向翻转 ② HA 阳线站在 ZLSMA50 上方
       （布林带宽度分位>70% 要不要加？—— 本脚本两种都跑）
  4. 赚到 2R 全平

对比基准：线上现状（破均线回撤 5根/50% + 2R 平一半）

用法：python backtest/s15_exit_mirror.py
"""
import json, glob, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s15_final import build

FEE = 0.0005          # taker 0.05%
SLIP = 0.0
MAX_HOLD_BARS = 64    # 16 小时 = 64 根 15m
SWING = 20
BBW_MIN = 70.0


def simulate_mirror(sym, ks, ind, *, rr_at=2.0, half=False, bbw_exit=False,
                    use_mirror=True, ma_bars=5, trail_pct=0.5, trail_r=0.8):
    """浩哥方案模拟器。use_mirror=False 时退回现状的「破均线回撤」出场。"""
    ho, hh, hl, hc = ind["ho"], ind["hh"], ind["hl"], ind["hc"]
    zl, dirs, atr = ind["zlsma"], ind["dir"], ind["atr"]
    bbw_pct = ind["bbw_pct"]
    n = len(hc)
    trades = []
    i = max(1, 60, SWING)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None or atr[i] is None:
            i += 1
            continue
        # ── 入场（与线上一致：CE 翻转 + HA 关键K线 + BBW 分位）──
        sig_long = dirs[i] == 1 and dirs[i - 1] == -1 and hc[i] > zl[i]
        sig_short = dirs[i] == -1 and dirs[i - 1] == 1 and hc[i] < zl[i]
        if not (sig_long or sig_short):
            i += 1; continue
        want = "long" if sig_long else "short"
        key_ok = (hc[i] > ho[i] and hc[i] > zl[i]) if want == "long" \
            else (hc[i] < ho[i] and hc[i] < zl[i])
        if not key_ok:
            i += 1; continue
        bp = bbw_pct[i]
        if bp is None or bp < BBW_MIN:
            i += 1; continue

        entry = float(ks[i + 1]["open"])
        if entry <= 0:
            i += 1; continue
        lo = max(0, i - SWING + 1)
        s_low = min(float(ks[j]["low"]) for j in range(lo, i + 1))
        s_high = max(float(ks[j]["high"]) for j in range(lo, i + 1))
        if want == "long":
            stop = s_low; risk = entry - stop
        else:
            stop = s_high; risk = stop - entry
        if risk <= 0 or risk / entry * 100 > 30.0:
            i += 1; continue

        sign = 1 if want == "long" else -1
        peak_r = 0.0
        half_done = False
        half_pnl = 0.0
        exit_px = exit_reason = exit_i = None
        ma_cnt = 0
        ce_flip_missed = 0     # CE 翻转了但 HA 不配合的次数（统计用）

        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))

            # 1) 硬止损
            if want == "long" and l_ <= stop:
                exit_px, exit_reason, exit_i = min(o_, stop), "硬止损", j; break
            if want == "short" and h_ >= stop:
                exit_px, exit_reason, exit_i = max(o_, stop), "硬止损", j; break

            peak_r = max(peak_r, ((h_ - entry) if want == "long" else (entry - l_)) / risk)
            cur_r = ((c_ - entry) if want == "long" else (entry - c_)) / risk

            # 2) 到 2R：全平 或 平一半
            if peak_r >= rr_at and not half_done:
                if half:
                    half_done = True
                    half_pnl = rr_at * 0.5 * risk
                    peak_r_after = 0.0
                else:
                    exit_px, exit_reason, exit_i = c_, f"{rr_at:.1f}R全平", j; break

            # 3) 超时
            if not half_done and (j - i) >= MAX_HOLD_BARS:
                exit_px, exit_reason, exit_i = c_, "超时16h", j; break

            # 4) 出场判定
            if use_mirror:
                # 浩哥方案：开仓条件的完全镜像，必须同一根同时满足
                flipped = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                    else (dirs[j] == 1 and dirs[j - 1] == -1)
                ha_bear = hc[j] < ho[j] and hc[j] < zl[j]
                ha_bull = hc[j] > ho[j] and hc[j] > zl[j]
                mirror_ok = flipped and (ha_bear if want == "long" else ha_bull)
                if not mirror_ok and flipped:
                    ce_flip_missed += 1
                if mirror_ok:
                    if bbw_exit:
                        bpj = bbw_pct[j]
                        if bpj is None or bpj < BBW_MIN:
                            mirror_ok = False
                    if mirror_ok:
                        exit_px, exit_reason, exit_i = c_, "镜像出场", j; break
                # 半仓后剩余部分：回撤 0.8R 出场
                if half_done and (peak_r - cur_r) >= 0.8:
                    exit_px, exit_reason, exit_i = c_, "半仓回撤0.8R", j; break
            else:
                # 现状：破均线 + 回撤
                reversed_ = (c_ < zl[j]) if want == "long" else (c_ > zl[j])
                if reversed_:
                    ma_cnt += 1
                else:
                    ma_cnt = 0
                need_bars = 5 if peak_r < 2.0 else 2
                need_dd = min(peak_r * trail_pct, 0.8) if 0.05 < peak_r < 2.0 else trail_r
                if ma_cnt >= need_bars and (peak_r - cur_r) >= need_dd:
                    exit_px, exit_reason, exit_i = c_, "破均线回撤", j; break

        if exit_px is None:
            exit_i = n - 1
            exit_px = float(ks[-1]["close"])
            exit_reason = "样本结束"
            cur_r = ((exit_px - entry) if want == "long" else (entry - exit_px)) / risk

        gross = (exit_px - entry) * sign
        cost = (entry + exit_px) * FEE
        pnl_per_unit = gross - cost
        r_mult = pnl_per_unit / risk
        if half_done:
            # 一半在 2R 结算，另一半按最终价结算
            r_mult = 0.5 * rr_at + 0.5 * r_mult
        trades.append(dict(sym=sym, side=want, r=r_mult, peak_r=peak_r,
                           risk_pct=risk / entry * 100, reason=exit_reason,
                           bars=exit_i - i, entry_i=i,
                           ce_missed=ce_flip_missed))
        i = exit_i + 1
    return trades


def load_cache():
    cache = []
    for fp in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data", "*_15m_60d.json"))):
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        try:
            cache.append((sym, ks, build(ks)))
        except Exception:
            pass
    return cache


def stats(T):
    n = len(T)
    if not n:
        return None
    r = sum(x["r"] for x in T)
    big = [x for x in T if x["r"] >= 2.0]
    # 简易最大回撤（按 R 累计）
    cum = 0.0; peak = 0.0; mdd = 0.0
    for x in T:
        cum += x["r"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    reasons = {}
    for x in T:
        reasons[x["reason"]] = reasons.get(x["reason"], 0) + 1
    return dict(n=n, r=r, avg=r / n, win=sum(1 for x in T if x["r"] > 0) / n * 100,
                nbig=len(big), mdd=mdd, maxr=max(x["r"] for x in T),
                reasons=reasons,
                avg_bars=sum(x["bars"] for x in T) / n)


def main():
    cache = load_cache()
    print(f"载入 {len(cache)} 个币\n")

    def run(tag, **kw):
        T = []
        for sym, ks, ind in cache:
            try:
                T.extend(simulate_mirror(sym, ks, ind, **kw))
            except Exception as e:
                print(f"  ERR {sym}: {e}")
        return tag, T

    cases = [
        ("现状：破均线回撤5根/50% + 2R平半", dict(use_mirror=False, half=True)),
        ("现状：破均线回撤5根/50% + 2R全平", dict(use_mirror=False, half=False)),
        ("★浩哥方案：镜像出场 + 2R全平",      dict(use_mirror=True, half=False, bbw_exit=False)),
        ("浩哥方案+布林带过滤",               dict(use_mirror=True, half=False, bbw_exit=True)),
        ("镜像出场 + 2R平一半",               dict(use_mirror=True, half=True, bbw_exit=False)),
        ("镜像出场 + 无2R（只靠镜像）",        dict(use_mirror=True, rr_at=999.0, half=False)),
    ]

    print("=" * 118)
    print("EXIT MODEL COMPARISON  (20 coins x 60d, taker 0.05%)")
    print("=" * 118)
    print(f"{'model':<38}{'n':>6}{'sumR':>9}{'avgR':>9}{'win%':>7}{'n>=2R':>7}{'maxDD':>9}{'avgBars':>8}")
    print("-" * 118)
    results = []
    for tag, kw in cases:
        t, T = run(tag, **kw)
        s = stats(T)
        if not s:
            continue
        results.append((tag, s, T))
        print(f"{tag:<38}{s['n']:>6}{s['r']:>+9.1f}{s['avg']:>+9.3f}{s['win']:>6.1f}%"
              f"{s['nbig']:>7}{s['mdd']:>+9.1f}{s['avg_bars']:>8.1f}")

    print("\n" + "=" * 118)
    print("EXIT REASON BREAKDOWN")
    print("=" * 118)
    for tag, s, T in results:
        print(f"\n{tag}")
        for k, v in sorted(s["reasons"].items(), key=lambda z: -z[1]):
            sub = [x["r"] for x in T if x["reason"] == k]
            print(f"   {k:<14}{v:>5} 笔   贡献 {sum(sub):>+8.1f}R   均笔 {sum(sub)/len(sub):>+.2f}R")

    # CE 翻转但 HA 不配合的统计
    mirror_case = [r for r in results if r[0].startswith("★浩哥方案")][0]
    miss = sum(x["ce_missed"] for x in mirror_case[2])
    print(f"\n[诊断] 浩哥方案里，CE 翻转了但 HA 不配合的次数：{miss} 次（这些时刻没能出场，只能等下一次）")


if __name__ == "__main__":
    main()
