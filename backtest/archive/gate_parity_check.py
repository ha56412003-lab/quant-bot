# -*- coding: utf-8 -*-
"""口径对齐验证：线上 evaluate() 是否与回测脚本 simulate_combo 判定一致

为什么必须有这一步（这是本项目最容易出错的地方，已连错三次）：
  回测脚本是另写的一套逻辑，线上是 strategies.py 另一套。两边「看起来一样」不算数，
  必须用同一批真实 K 线，逐点对账：回测说能进，线上也必须能进；回测算的 dev，
  线上算的 dev 必须同一个数。

做法：
  ① 用 simulate_combo（dev 闸关闭）扫出所有「三闸全过」候选点，记下每个点的 dev 值
  ② 对每个候选点，把 K 线截断到该点（+1 根），调用真实 S15CeZlsma.evaluate()
  ③ 对比三件事：
       · 两边是否都认为「这里是个进场点」
       · 新闸（dev>0.5%）两边判定是否一致
       · 换掉 min_dev_pct 后两边是否同步变化
  ④ 抽样 30 个点做全量对比（避免 O(n²) 太慢），一致率必须是 100%

用法：python backtest/gate_parity_check.py
"""
import argparse
import asyncio
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backend.strategies as S            # noqa: E402
from backend.strategies import _ce_zlsma  # noqa: E402

SWING = 20
BBW_MIN = 70.0
MAX_RISK_PCT = 30.0
MIN_DEV = 0.5


class _FakeDB:
    def update_position(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass


S.db = _FakeDB()


class Ctx:
    def __init__(self, ks):
        self.ks = ks

    def has_position(self, sym, sid):
        return False

    async def klines(self, sym, iv, need):
        return self.ks                       # 不要截断：evaluate 用 len(ks)-2 定位

    def price(self, sym):
        return float(self.ks[-1]["close"])


def candidates(ks, ind):
    """回测侧：找出三闸全过点（不含 dev 闸），返回 (i, dev_pct)"""
    ho, hc = ind["open"], ind["close"]
    zl, dirs, bp = ind["zlsma"], ind["dir"], ind["bbw_pct"]
    hi_raw, lo_raw = ind["high_raw"], ind["low_raw"]
    out = []
    n = len(hc)
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
        if bp[i] is None or bp[i] < BBW_MIN:
            i += 1
            continue
        px = float(ks[i]["close"])
        lo_i = max(0, i - SWING + 1)
        s_lo = min(lo_raw[j] for j in range(lo_i, i + 1))
        s_hi = max(hi_raw[j] for j in range(lo_i, i + 1))
        stop = s_lo if want == "long" else s_hi
        risk = (px - stop) if want == "long" else (stop - px)
        if risk <= 0 or risk / px * 100 > MAX_RISK_PCT:
            i += 1
            continue
        dev = abs(px - zl[i]) / zl[i] * 100.0
        out.append((i, dev, want))
        i += 1
    return out


def make_strat(min_dev):
    st = S.S15CeZlsma.__new__(S.S15CeZlsma)
    st.p = {
        "kline_interval": "15m", "ce_len": 1, "ce_mult": 2.0, "zlsma_len": 50,
        "use_close": True, "use_ha": True, "swing_lookback": SWING,
        "min_atr_pct": 0, "bbw_pct_min": BBW_MIN, "max_risk_pct": MAX_RISK_PCT,
        "fee_rate": 0.0005, "slip_pct": 0.0, "half_at_r": 2.0, "half_tp_r": 1.0,
        "min_dev_pct": min_dev, "quickfail_hours": 4.0, "quickfail_min_r": 0.5,
    }
    st.sid = "S15_CE_ZLSMA"
    st.max_hold = 16 * 3600
    st.syms = lambda ctx: ["X"]
    return st


async def live_check(ks, i, min_dev):
    """线上侧：把 K 线截到 i（+1 根），让 evaluate 判那一刻"""
    S._LAST_BAR.clear()
    S._GATE_STATE.clear()
    st = make_strat(min_dev)
    sub = ks[: i + 2]      # len=i+2 → evaluate 的 len-2 == i
    sig = await st.evaluate(Ctx(sub))
    g = S._GATE_STATE.get("X") or {}
    return len(sig), g.get("dev"), g.get("why")


FULL = False


def main():
    global FULL
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="验证每个候选点（更慢更严）")
    a = ap.parse_args()
    FULL = a.full
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    picks = []
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        ind = _ce_zlsma(ks, 1, 2.0, 50, True, True)
        cs = candidates(ks, ind)
        if cs:
            picks.append((sym, ks, cs))

    print("=" * 92)
    print("口径对齐验证：线上 evaluate()  vs  回测 candidates()")
    print("=" * 92)
    scope = "全部候选点" if FULL else "每币抽样 3 个点"
    print(f"可用币种 {len(picks)}，验证范围：{scope}（全部候选点均为三闸全过）\n")

    tot = same_sig = same_dev = same_gate = 0
    mism = []
    for sym, ks, cs in picks:
        # --full 全量；否则抽首、中、尾各一个
        idxs = range(len(cs)) if FULL else sorted({0, len(cs) // 2, len(cs) - 1})
        for k in idxs:
            i, dev_bt, want = cs[k]
            if i + 2 < 200:
                continue
            n_on, dev_live, why = asyncio.run(live_check(ks, i, 0.0))
            n_off, dev_live2, why2 = asyncio.run(live_check(ks, i, MIN_DEV))
            tot += 1
            sig_ok = n_on >= 1
            dev_ok = dev_live is not None and abs(float(dev_live) - dev_bt) < 0.01
            # dev 闸开时：dev>0.5 必须出信号，dev<=0.5 必须不出
            expect_off = 1 if dev_bt > MIN_DEV else 0
            gate_ok = (n_off == expect_off)
            if sig_ok:
                same_sig += 1
            if dev_ok:
                same_dev += 1
            if gate_ok:
                same_gate += 1
            if not (sig_ok and dev_ok and gate_ok):
                mism.append((sym, ks[i]["ts"], dev_bt, dev_live,
                             n_on, n_off, expect_off, why, why2))

    print(f"抽检 {tot} 个点：")
    print(f"  ① 回测说能进 → 线上也出信号     {same_sig}/{tot}")
    print(f"  ② dev 值两边算的一致            {same_dev}/{tot}")
    print(f"  ③ dev 闸开关两边结论一致        {same_gate}/{tot}")
    ok = tot > 0 and same_sig == tot and same_dev == tot and same_gate == tot
    print()
    if mism:
        print("不一致明细：")
        for m in mism[:10]:
            print(f"  {m[0]} ts={m[1]} 回测dev={m[2]:.3f} 线上dev={m[3]} "
                  f"线上信号(闸关)={m[4]} 线上信号(闸开)={m[5]} 期望={m[6]}")
            print(f"      why={m[7]} / {m[8]}")
    print("=" * 92)
    print(f"结论：{'✅ 口径一致，线上代码与回测脚本判定相同' if ok else '❌ 存在不一致，需排查'}")
    print("=" * 92)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
