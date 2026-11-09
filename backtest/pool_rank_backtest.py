"""监控池「40 个名额怎么排」的回测对照（2026-11-09）。

## 要回答的问题

现在 40 个名额按**成交额**排。实测有个趋势效率 0.95 的币被 40 个弱币挡在外面
（SEI：EFF 0.340 达标、成交额仅 10M，排不进 40）。改成按**趋势效率**排符合
既定铁律（行情分类禁止用 24h 涨跌幅，必须用 EFF），但会改变选币结果 → 先回测。

## 三种排序

  A  成交额（quote volume）降序          ← 现状基线
  B  趋势效率（EFF）降序                  ← 用户倾向
  C  EFF 降序 + 成交额准入门槛            ← 折中：先把流动性太差的剔掉，再按 EFF 排

三者**准入门槛完全相同**（止损距离 ≥ 1.0%、EFF ≥ 0.30，取自线上
STOP_MIN_PCT / EFF_MIN），差别只在「名额不够时谁先上」。

## 口径（铁律：回测口径必须与线上一致）

- 指标：直接 import 线上的 `backend.strategies._ce_zlsma`
  （ce_len=1, mult=2.0, zl_len=50, use_close=True, HA=True）
- 出场：直接调用 `backtest/s15_live_parity.simulate`
  （want_mirror=True, use_bbw=True，即线上封版的那套）
- 池子准入：直接 import 线上的 `backend.quality._eff` / `_stop_dist`
- 只使用「该时刻之前」的 K 线，绝不引入未来数据

**自证**：`--selftest` 会验证「不设名额上限时，三种排序的交易集合完全一致」
（= 池子不构成约束时，排序不该产生任何影响）。这是本脚本的唯一正确性检查。

## 名额说明（重要）

数据里能相互对齐的币只有 17 个，没有 200 个，所以**无法复刻线上的 40/200**。
为了让排序真正产生竞争，本脚本把名额压到 4/6/8（相当于 17 抢 4~8）。
线上是 200 抢 40（20%），本测试是 17 抢 4（23%），竞争强度接近。
**绝对收益数字不能直接搬去预测线上，只能看「三种排序谁更好、差多少」。**

用法：
    python backtest/pool_rank_backtest.py            # 默认名额 4 6 8
    python backtest/pool_rank_backtest.py --slots 5 10
    python backtest/pool_rank_backtest.py --selftest # 口径自证
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _ce_zlsma            # noqa: E402  线上同一份实现
from backend.quality import _eff, _stop_dist       # noqa: E402  线上同一份实现
from backtest.s15_live_parity import simulate      # noqa: E402  线上封版出场

# 线上口径常量
EFF_MIN = 0.30
STOP_MIN_PCT = 1.00
EFF_WIN = 96          # 96 根 15m = 24h
STOP_LB = 20
QV_WIN = 96
MAX_CONCURRENT = 8
MAX_SAME_SIDE = 6
MIN_DAYS = 55


# ─────────────────────────── 数据 ───────────────────────────

def load_pool():
    """载入所有 >=55 天的 15m 数据，取公共时间窗。"""
    best = {}
    for fp in glob.glob(os.path.join(ROOT, "backtest", "data", "*15m*.json")):
        fn = os.path.basename(fp)
        m = re.search(r"([A-Z0-9]+USDT)_15m_(\d+)", fn)
        if not m:
            continue
        sym = m.group(1)
        try:
            ks = json.load(open(fp, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(ks, list) or len(ks) < 1000:
            continue
        days = (ks[-1]["ts"] - ks[0]["ts"]) / 86400
        if days < MIN_DAYS:
            continue
        if sym not in best or len(ks) > len(best[sym]):
            best[sym] = ks

    if not best:
        raise SystemExit("没有 >=55 天的 15m 数据")

    # 公共时间窗：取所有币的最大起点、最小终点
    t0 = max(ks[0]["ts"] for ks in best.values())
    t1 = min(ks[-1]["ts"] for ks in best.values())
    out = {}
    for sym, ks in best.items():
        seg = [b for b in ks if t0 <= b["ts"] <= t1]
        if len(seg) >= 1000:
            out[sym] = seg
    return out


# ──────────────────── 每根 K 线的池子指标 ────────────────────

def _rolling_min(arr, w):
    """滑窗最小值 O(n)（单调双端队列）。"""
    from collections import deque
    dq, out = deque(), [None] * len(arr)
    for i, v in enumerate(arr):
        while dq and arr[dq[-1]] >= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.popleft()
        if i >= w - 1:
            out[i] = arr[dq[0]]
    return out


def _rolling_max(arr, w):
    from collections import deque
    dq, out = deque(), [None] * len(arr)
    for i, v in enumerate(arr):
        while dq and arr[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.popleft()
        if i >= w - 1:
            out[i] = arr[dq[0]]
    return out


def build_metrics(ks):
    """对每根 K 线算 (eff, stop_dist, quote_volume)，只用该根及之前的数据。

    性能：原来逐根构造 96 个 dict 再调用线上函数 —— 17 币 × 5760 根 × 96
    = 940 万次 dict 创建，跑了 10 分钟还没完。这里改成滚动和 + 单调队列，
    O(n)。**算法必须与 quality._eff / _stop_dist 完全等价**，由 --verify 对拍。
    """
    n = len(ks)
    closes = [b["close"] for b in ks]
    # 数据里两种字段名都有：新抓的是 volume，orig_* 是 vol（单位一致，都是基础币量）
    vols = [(b.get("volume") if b.get("volume") is not None else b.get("vol") or 0.0)
            for b in ks]
    lows = [b["low"] for b in ks]
    highs = [b["high"] for b in ks]

    # 成交额滚动和（96 根）
    qvs = [None] * n
    run = 0.0
    for i in range(n):
        run += vols[i] * closes[i]
        if i >= QV_WIN:
            run -= vols[i - QV_WIN] * closes[i - QV_WIN]
        if i >= QV_WIN - 1:
            qvs[i] = run

    # 96 根窗口的 min(low) / max(high) —— 对应 _eff 的分母
    wlo96 = _rolling_min(lows, EFF_WIN)
    whi96 = _rolling_max(highs, EFF_WIN)
    # 20 根窗口的 min(low) / max(high) —— 对应 _stop_dist 的区间
    wlo20 = _rolling_min(lows, STOP_LB)
    whi20 = _rolling_max(highs, STOP_LB)

    effs = [None] * n
    sds = [None] * n
    for i in range(n):
        if i < EFF_WIN - 1:
            continue
        p0 = closes[i - EFF_WIN + 1]
        p1 = closes[i]
        lo, hi = wlo96[i], whi96[i]
        # _eff: |p1/p0-1| / ((hi-lo)/lo)
        if p0 and p0 > 0 and lo and lo > 0:
            amp = (hi - lo) / lo
            if amp > 0:
                effs[i] = abs(p1 / p0 - 1.0) / amp
        # _stop_dist: 取 max(做多, 做空)，与线上一致
        # 线上有 if px > lo else 0.0 / if hi > px else 0.0 两道守卫，必须照抄
        if i >= STOP_LB - 1 and closes[i] > 0 and wlo20[i] and whi20[i]:
            px = closes[i]
            lo20, hi20 = wlo20[i], whi20[i]
            dl = (px - lo20) / px * 100.0 if px > lo20 else 0.0
            ds = (hi20 - px) / px * 100.0 if hi20 > px else 0.0
            sds[i] = max(dl, ds)
    return effs, sds, qvs


def verify_metrics(data, M, samples=60):
    """对拍：向量化结果 vs 线上 quality._eff / _stop_dist，必须完全一致。"""
    import random
    bad = 0
    checked = 0
    rng = random.Random(20261109)
    for sym, ks in data.items():
        n = len(ks)
        idxs = rng.sample(range(EFF_WIN, n), min(samples, n - EFF_WIN))
        for i in idxs:
            seg = ks[i - EFF_WIN + 1:i + 1]
            ref_eff = _eff(seg, EFF_WIN)
            # 注意：传整个 seg。_stop_dist 内部取 bars[-lb:]，只传 20 根会触发
            # len(bars) < lb+1 的 None 分支 —— 首次对拍就是栽在这个假差异上。
            ref_sd = _stop_dist(seg, STOP_LB)
            got_eff = M[sym]["eff"][i]
            got_sd = M[sym]["sd"][i]
            checked += 1
            e_ok = (ref_eff is None and got_eff is None) or (
                ref_eff is not None and got_eff is not None and abs(ref_eff - got_eff) < 1e-12)
            s_ok = (ref_sd is None and got_sd is None) or (
                ref_sd is not None and got_sd is not None and abs(ref_sd - got_sd) < 1e-12)
            if not (e_ok and s_ok):
                bad += 1
                if bad <= 3:
                    print(f"    ★ {sym}[{i}] eff {ref_eff} vs {got_eff} / "
                          f"sd {ref_sd} vs {got_sd}")
    print(f"  指标对拍：{checked} 个点，不一致 {bad} 个 "
          f"{'✓ 完全等价' if bad == 0 else '★ 有差异'}")
    return bad == 0


# ──────────────────── 池子筛选 ────────────────────

def eligible(sym, i, M, qv_floor):
    """该币在第 i 根时是否过池子准入门槛。"""
    eff = M[sym]["eff"][i]
    sd = M[sym]["sd"][i]
    qv = M[sym]["qv"][i]
    if eff is None or sd is None or qv is None:
        return False
    if not (sd >= STOP_MIN_PCT and eff >= EFF_MIN):
        return False
    if qv_floor is not None and qv < qv_floor:
        return False
    return True


def pick_pool(syms, i, M, mode, slots, qv_floor):
    """在第 i 根时按指定排序选出 <= slots 个币。"""
    cand = [s for s in syms if eligible(s, i, M, qv_floor)]
    if mode == "A":            # 成交额降序（现状）
        cand.sort(key=lambda s: -M[s]["qv"][i])
    elif mode == "B":          # EFF 降序
        cand.sort(key=lambda s: (-M[s]["eff"][i], -M[s]["qv"][i]))
    elif mode == "C":          # EFF 降序（qv 已在 eligible 里当门槛）
        cand.sort(key=lambda s: (-M[s]["eff"][i], -M[s]["qv"][i]))
    else:
        raise ValueError(mode)
    return cand[:slots]


# ──────────────────── 重放 ────────────────────

def replay(trades_by_sym, M, syms, mode, slots, qv_floor=None, use_pool=True):
    """按时间重放所有交易，只接受「入场时刻在池内」的，并按并发/同向限制。

    trades_by_sym: {sym: [trade...]}，trade 里已带 entry_ts / exit_ts / r。
    """
    all_tr = []
    for sym, ts in trades_by_sym.items():
        for t in ts:
            t = dict(t)
            t["sym"] = sym
            all_tr.append(t)
    all_tr.sort(key=lambda t: t["entry_ts"])

    # ts -> 该币的 bar 索引
    idx_of = {}
    for sym, ts_list in M.items():
        idx_of[sym] = ts_list["ts_index"]

    # 池子快照缓存：同一根 K 线只算一次
    pool_cache = {}

    def pool_at(sym, ts):
        """该币在 ts 这根是否在池内。"""
        i = idx_of[sym].get(ts)
        if i is None:
            return False
        key = (i, mode, qv_floor)
        if key not in pool_cache:
            pool_cache[key] = set(pick_pool(syms, i, M, mode, slots, qv_floor))
        return sym in pool_cache[key]

    taken = []
    open_until = []        # [(exit_ts, side)]
    per_sym_last_exit = {}

    for t in all_tr:
        if use_pool and not pool_at(t["sym"], t["entry_ts"]):
            continue
        # 同币不可同时两笔（线上：禁止对锁/加仓）
        if per_sym_last_exit.get(t["sym"], 0) > t["entry_ts"]:
            continue
        # 并发 / 同向
        act = [x for x in open_until if x[0] > t["entry_ts"]]
        open_until = act
        if len(act) >= MAX_CONCURRENT:
            continue
        if sum(1 for x in act if x[1] == t["side"]) >= MAX_SAME_SIDE:
            continue
        open_until.append((t["exit_ts"], t["side"]))
        per_sym_last_exit[t["sym"]] = t["exit_ts"]
        taken.append(t)
    return taken


def stats(trades):
    if not trades:
        return dict(n=0, r=0.0, avg=0.0, win=0.0, mdd=0.0, bars=0.0, coins=0, conc=0.0)
    r = sum(t["r"] for t in trades)
    w = sum(1 for t in trades if t["r"] > 0) / len(trades) * 100
    cum = pk = mdd = 0.0
    for t in sorted(trades, key=lambda z: z["exit_ts"]):
        cum += t["r"]
        pk = max(pk, cum)
        mdd = min(mdd, cum - pk)
    cnt = Counter(t["sym"] for t in trades)
    top = sum(v for _k, v in cnt.most_common(3))
    return dict(n=len(trades), r=r, avg=r / len(trades), win=w, mdd=mdd,
                bars=sum(t["bars"] for t in trades) / len(trades),
                coins=len(cnt), conc=top / len(trades) * 100)


# ──────────────────── 主流程 ────────────────────

def missed_analysis(trades_by_sym, M, syms, slots=40, qv_floor=None):
    """直接回答用户的问题：被池子挡住的「合格交易」，赚得比池内多还是少？

    不做名额对照，只把每笔交易的入场时刻分成三类：
      ① 在池内（过了准入门槛 + 排进名额）
      ② 池外·合格（过了准入门槛，只是名额不够）
      ③ 池外·不合格（止损太窄 / EFF 不够 / 数据不足）
    然后比这三类的均笔 R。如果 ② 明显好于 ①，说明「名额按成交额排」在选错人。
    """
    rows = []
    for sym, ts_list in trades_by_sym.items():
        for t in ts_list:
            i = M[sym]["ts_index"].get(t["entry_ts"])
            if i is None:
                continue
            eff, sd, qv = M[sym]["eff"][i], M[sym]["sd"][i], M[sym]["qv"][i]
            if eff is None or sd is None or qv is None:
                cat = "③ 数据不足"
            elif not (sd >= STOP_MIN_PCT and eff >= EFF_MIN):
                cat = "③ 不合格"
            else:
                pool = set(pick_pool(syms, i, M, "A", slots, qv_floor))
                cat = "① 在池内" if sym in pool else "② 池外·合格"
            rows.append((cat, t["r"], eff or 0.0, sd or 0.0, sym, t["bars"]))

    print("=" * 92)
    print(f"按「入场时刻」把每一笔交易归类（名额 {slots}，与线上 40/200 竞争比接近）")
    print("=" * 92)
    print(f"{'类别':<14}{'笔数':>7}{'净R':>10}{'均笔R':>10}{'胜率':>8}{'均EFF':>9}{'均止损%':>10}")
    print("-" * 92)
    out = {}
    for cat in ("① 在池内", "② 池外·合格", "③ 不合格", "③ 数据不足"):
        sub = [r for r in rows if r[0] == cat]
        if not sub:
            continue
        n = len(sub)
        r_sum = sum(x[1] for x in sub)
        win = sum(1 for x in sub if x[1] > 0) / n * 100
        avg_eff = sum(x[2] for x in sub) / n
        avg_sd = sum(x[3] for x in sub) / n
        print(f"{cat:<14}{n:>7}{r_sum:>+10.1f}{r_sum / n:>+10.3f}"
              f"{win:>7.1f}%{avg_eff:>9.3f}{avg_sd:>10.2f}")
        out[cat] = dict(n=n, r=r_sum, avg=r_sum / n, win=win, eff=avg_eff, sd=avg_sd)
    print("=" * 92)

    inside = out.get("① 在池内")
    outside = out.get("② 池外·合格")
    if inside and outside:
        diff = outside["avg"] - inside["avg"]
        print(f"\n【核心问题】池外合格 vs 池内：均笔 {outside['avg']:+.3f}R vs "
              f"{inside['avg']:+.3f}R，差 {diff:+.3f}R")
        print(f"  池外合格共 {outside['n']} 笔，如果放进来可多赚 {outside['r']:+.1f}R")
        if diff > 0.02:
            print("  → 池子确实在选错人（池外合格的平均表现更好），改排序有依据")
        elif diff > -0.02:
            print("  → 两者几乎没差别，换排序键意义不大")
        else:
            print("  → 池内的反而更好：现在的成交额排序是有价值的（流动性过滤）")
        if outside["eff"] > inside["eff"]:
            print(f"  但池外合格的 EFF 更高（{outside['eff']:.3f} vs "
                  f"{inside['eff']:.3f}）—— 强趋势币确实被挡在外面")
        if outside["sd"] < inside["sd"]:
            print(f"  代价：池外合格的止损更窄（{outside['sd']:.2f}% vs "
                  f"{inside['sd']:.2f}%）—— 手续费占比更高，可能抵消趋势优势")
    return out


def eff_buckets_split(trades_by_sym, M):
    """把时间切成前后两半，各自跑一遍 EFF 分档 —— 检验结论是否稳健（防过拟合）。"""
    all_ts = []
    for sym, ts_list in trades_by_sym.items():
        for t in ts_list:
            all_ts.append(t["entry_ts"])
    if not all_ts:
        return
    mid = (min(all_ts) + max(all_ts)) // 2
    buckets = [(0.0, 0.30), (0.30, 0.45), (0.45, 0.60), (0.60, 0.75), (0.75, 1.01)]
    print("=" * 96)
    print("稳健性检验：前一半 vs 后一半时间，EFF 分档的均笔 R 是否一致")
    print("=" * 96)
    hdr = "".join(f"{f'{lo:.2f}+':>14}" for lo, _hi in buckets)
    print(f"{'时间段':<12}{'笔数':>7}" + hdr)
    print("-" * 96)
    for name, cond in (("前半段", lambda x: x < mid), ("后半段", lambda x: x >= mid)):
        row = []
        n_all = 0
        for lo, hi in buckets:
            vals = []
            for sym, ts_list in trades_by_sym.items():
                for t in ts_list:
                    if not cond(t["entry_ts"]):
                        continue
                    i = M[sym]["ts_index"].get(t["entry_ts"])
                    if i is None:
                        continue
                    eff = M[sym]["eff"][i]
                    if eff is not None and lo <= eff < hi:
                        vals.append(t["r"])
            n_all += len(vals)
            row.append(f"{sum(vals) / len(vals):+14.3f}" if vals else f"{'-':>14}")
        print(f"{name:<12}{n_all:>7}" + "".join(row))
    print("=" * 96)
    print("  如果两段同向、量级接近 → 结论可用；如果相反 → 是噪声，不能据此改策略")


def eff_buckets(trades_by_sym, M):
    """按入场时的 EFF 分档，看「趋势越强是否真的越赚」。

    这才是「按 EFF 排序」有没有意义的根本依据：
    如果 0.9+ 档的均笔 R 并不高于 0.3~0.5 档，那按 EFF 排就是把名额
    分给了「看起来趋势强、实际不赚钱」的币。
    """
    buckets = [(0.0, 0.30), (0.30, 0.45), (0.45, 0.60), (0.60, 0.75), (0.75, 1.01)]
    rows = []
    for sym, ts_list in trades_by_sym.items():
        for t in ts_list:
            i = M[sym]["ts_index"].get(t["entry_ts"])
            if i is None:
                continue
            eff = M[sym]["eff"][i]
            if eff is None:
                continue
            rows.append((eff, t["r"], t["bars"], sym))
    print("=" * 84)
    print("按入场时刻的趋势效率（EFF）分档，看趋势强度与收益的关系")
    print("=" * 84)
    print(f"{'EFF 区间':<14}{'笔数':>7}{'净R':>10}{'均笔R':>10}{'胜率':>8}{'均持仓根':>10}")
    print("-" * 84)
    for lo, hi in buckets:
        sub = [x for x in rows if lo <= x[0] < hi]
        if not sub:
            continue
        n = len(sub)
        r = sum(x[1] for x in sub)
        win = sum(1 for x in sub if x[1] > 0) / n * 100
        print(f"{f'{lo:.2f}~{hi:.2f}':<14}{n:>7}{r:>+10.1f}{r / n:>+10.3f}"
              f"{win:>7.1f}%{sum(x[2] for x in sub) / n:>10.1f}")
    print("=" * 84)
    return rows


def eff_cap_test(trades_by_sym, M, syms, slots=8):
    """测「给 EFF 加上限」的效果：把追尾的高 EFF 币挡在池外，能多赚多少。

    依据：EFF ≥0.60 的入场均笔 R 为负，且前后两段都成立。
    现在池子只有下限（EFF_MIN=0.30），没有上限。
    """
    print("=" * 96)
    print("给 EFF 加上限（剔除「趋势已经走完」的追尾币）")
    print("=" * 96)
    print(f"{'EFF 上限':<10}{'池内交易':>10}{'净R':>10}{'均笔R':>10}{'胜率':>8}"
          f"{'池子大小(均)':>14}")
    print("-" * 96)
    base_r = None
    for cap in (None, 0.90, 0.80, 0.75, 0.70, 0.65):
        taken, sizes = [], []
        per_sym_last = {}
        open_until = []
        for t in sorted([dict(x, sym=sy) for sy, ts in trades_by_sym.items() for x in ts],
                        key=lambda z: z["entry_ts"]):
            sym = t["sym"]
            i = M[sym]["ts_index"].get(t["entry_ts"])
            if i is None:
                continue
            eff, sd, qv = M[sym]["eff"][i], M[sym]["sd"][i], M[sym]["qv"][i]
            if eff is None or sd is None or qv is None:
                continue
            if not (sd >= STOP_MIN_PCT and eff >= EFF_MIN):
                continue
            if cap is not None and eff >= cap:
                continue
            # 等价于「合格即进池」——池子不成为约束，只看 EFF 上限的影响
            if per_sym_last.get(sym, 0) > t["entry_ts"]:
                continue
            act = [x for x in open_until if x[0] > t["entry_ts"]]
            open_until = act
            if len(act) >= MAX_CONCURRENT:
                continue
            open_until.append((t["exit_ts"], t["side"]))
            per_sym_last[sym] = t["exit_ts"]
            taken.append(t)
            sizes.append(len(act) + 1)
        n = len(taken)
        r = sum(x["r"] for x in taken)
        win = sum(1 for x in taken if x["r"] > 0) / n * 100 if n else 0
        avg_sz = sum(sizes) / len(sizes) if sizes else 0
        if base_r is None:
            base_r = r
        label = "无上限(现状)" if cap is None else f"{cap:.2f}"
        delta = "" if cap is None else f"  ({r - base_r:+.1f}R)"
        print(f"{label:<10}{n:>10}{r:>+10.1f}{r / n if n else 0:>+10.3f}"
              f"{win:>7.1f}%{avg_sz:>14.1f}{delta}")
    print("=" * 96)


def eff_cap_split(trades_by_sym, M):
    """EFF 上限的稳健性检验：前后两半各自最优的上限是否一致。"""
    all_ts = [t["entry_ts"] for ts in trades_by_sym.values() for t in ts]
    if not all_ts:
        return
    mid = (min(all_ts) + max(all_ts)) // 2
    print("=" * 96)
    print("EFF 上限的稳健性：前后两半各自跑一遍")
    print("=" * 96)
    print(f"{'EFF 上限':<12}{'前半净R':>10}{'前半笔数':>10}"
          f"{'后半净R':>10}{'后半笔数':>10}{'两段都改善?':>14}")
    print("-" * 96)
    base = {}
    for cap in (None, 0.90, 0.80, 0.75, 0.70, 0.65, 0.60):
        seg = {}
        for name, cond in (("h1", lambda x: x < mid), ("h2", lambda x: x >= mid)):
            taken, per_sym_last, open_until = [], {}, []
            for t in sorted([dict(x, sym=sy) for sy, ts in trades_by_sym.items() for x in ts],
                            key=lambda z: z["entry_ts"]):
                if not cond(t["entry_ts"]):
                    continue
                sym = t["sym"]
                i = M[sym]["ts_index"].get(t["entry_ts"])
                if i is None:
                    continue
                eff, sd = M[sym]["eff"][i], M[sym]["sd"][i]
                if eff is None or sd is None:
                    continue
                if not (sd >= STOP_MIN_PCT and eff >= EFF_MIN):
                    continue
                if cap is not None and eff >= cap:
                    continue
                if per_sym_last.get(sym, 0) > t["entry_ts"]:
                    continue
                act = [x for x in open_until if x[0] > t["entry_ts"]]
                open_until = act
                if len(act) >= MAX_CONCURRENT:
                    continue
                open_until.append((t["exit_ts"], t["side"]))
                per_sym_last[sym] = t["exit_ts"]
                taken.append(t)
            seg[name] = (sum(x["r"] for x in taken), len(taken))
        if cap is None:
            base = seg
        label = "无上限" if cap is None else f"{cap:.2f}"
        d1 = seg["h1"][0] - base["h1"][0]
        d2 = seg["h2"][0] - base["h2"][0]
        both = "✓ 两段都赢" if (d1 > 0 and d2 > 0) else (
            "✗ 只有一段赢" if (d1 > 0 or d2 > 0) else "✗ 两段都输")
        print(f"{label:<12}{seg['h1'][0]:>+10.1f}{seg['h1'][1]:>10}"
              f"{seg['h2'][0]:>+10.1f}{seg['h2'][1]:>10}{both:>14}"
              f"   ({d1:+.1f} / {d2:+.1f})")
    print("=" * 96)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", nargs="+", type=int, default=[4, 6, 8])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--buckets", action="store_true", help="按 EFF 分档看收益")
    ap.add_argument("--effcap", action="store_true", help="测 EFF 上限（追尾过滤）")
    ap.add_argument("--missed", type=int, default=None,
                    help="只做「池外合格交易赚不赚」分析，参数为名额")
    args = ap.parse_args()

    data = load_pool()
    syms = sorted(data)
    common = min(len(v) for v in data.values())
    print(f"载入 {len(syms)} 币，公共窗口 {common} 根 15m "
          f"（{common / 96:.1f} 天）")
    print(f"时间窗：{data[syms[0]][0]['ts']} ~ {data[syms[0]][-1]['ts']}")

    # 指标（线上函数）
    M = {}
    for sym, ks in data.items():
        ind = _ce_zlsma(ks, 1, 2.0, 50, True, True)
        effs, sds, qvs = build_metrics(ks)
        M[sym] = dict(eff=effs, sd=sds, qv=qvs, ks=ks, ind=ind,
                      ts_index={b["ts"]: i for i, b in enumerate(ks)})
    print("指标计算完成")

    # 每币交易（线上封版出场）
    trades_by_sym = {}
    for sym, ks in data.items():
        trades_by_sym[sym] = simulate(ks, M[sym]["ind"], want_mirror=True, use_bbw=True)
    total_raw = sum(len(v) for v in trades_by_sym.values())
    print(f"单币回测共 {total_raw} 笔（未受池子/并发约束）\n")

    # ── 口径自证 ──
    print("==" * 40)
    print("口径自证")
    print("==" * 40)
    if not verify_metrics(data, M):
        print("\n★ 指标与线上不等价，回测结论不可用，终止。")
        return 2
    if args.effcap:
        eff_cap_test(trades_by_sym, M, syms)
        eff_cap_split(trades_by_sym, M)
        return 0
    if args.buckets:
        eff_buckets(trades_by_sym, M)
        eff_buckets_split(trades_by_sym, M)
        return 0
    if args.missed is not None:
        missed_analysis(trades_by_sym, M, syms, slots=args.missed)
        return 0
    if args.selftest:
        ok = True
        for mode in ("A", "B", "C"):
            got = replay(trades_by_sym, M, syms, mode, slots=99, qv_floor=None,
                         use_pool=False)
            s = stats(got)
            print(f"  自证[{mode}] 不设池子 → {s['n']} 笔 / {s['r']:+.1f}R")
            if s["n"] != stats(replay(trades_by_sym, M, syms, "A", 99, None, False))["n"]:
                ok = False
        print(f"\n  自证结果：{'通过 ✓（三种排序在无约束下等价）' if ok else '★ 失败'}")
        return 0 if ok else 1

    # ── 对照跑 ──
    # 方案 C 的成交额门槛：取全体候选的中位数（避免流动性极差的币靠 EFF 挤进池）
    all_qv = []
    for sym in syms:
        for i in range(EFF_WIN, len(M[sym]["ks"])):
            if eligible(sym, i, M, None):
                all_qv.append(M[sym]["qv"][i])
    all_qv.sort()
    qv_floor = all_qv[len(all_qv) // 2] if all_qv else 0.0

    rows = []
    for slots in args.slots:
        for mode, label in (("A", "A 成交额（现状）"),
                            ("B", "B 趋势效率"),
                            ("C", "C EFF+流动性门槛")):
            got = replay(trades_by_sym, M, syms, mode, slots, qv_floor)
            s = stats(got)
            s["pool"] = f"{mode}/{slots}"
            s["label"] = label
            s["slots"] = slots
            s["mode"] = mode
            rows.append(s)

    print("=" * 104)
    print(f"{'名额':<6}{'排序':<20}{'笔数':>7}{'净R':>10}{'均笔R':>9}"
          f"{'胜率':>8}{'回撤':>10}{'均持仓':>9}{'币数':>7}{'前3币占比':>11}")
    print("-" * 104)
    for s in rows:
        print(f"{s['slots']:<6}{s['label']:<20}{s['n']:>7}{s['r']:>+10.1f}"
              f"{s['avg']:>+9.3f}{s['win']:>7.1f}%{s['mdd']:>+10.1f}"
              f"{s['bars']:>9.1f}{s['coins']:>7}{s['conc']:>10.1f}%")
    print("=" * 104)

    # ── 结论：每个名额档位下，谁赢 ──
    print("\n【每个名额下，最优排序】")
    for slots in args.slots:
        grp = [s for s in rows if s["slots"] == slots]
        best = max(grp, key=lambda s: s["r"])
        base = next(s for s in grp if s["mode"] == "A")
        print(f"  名额 {slots}: 最优 = {best['label']} "
              f"({best['r']:+.1f}R / {best['n']} 笔)；"
              f"vs 现状 A {base['r']:+.1f}R → "
              f"{best['r'] - base['r']:+.1f}R")

    return 0


if __name__ == "__main__":
    sys.exit(main())
