# -*- coding: utf-8 -*-
"""实盘口径 vs 回测口径：同一批 K 线，两条路径的最终收益差多少。

背景（接实盘前必须回答的问题）：
  回测 sim()   → 每根 15m K 线，用 close 判定出场、用 high/low 记峰值
  实盘 engine  → 每 0.5 秒轮询，用实时价判定出场、用实时价记峰值

本脚本回答的是：同一批 15m K 线，线上 check_exit 函数与回测 sim() 是否
给出同样的出场 —— 即两条「实现」是否一致。

⚠ 它不能回答「0.5 秒轮询 vs 15m 收盘差多少」：手上只有 15m K 线，
复现不出 K 线内部的实时采样。那个问题必须用 1m 数据另做，
不要把本脚本的差值当成采样频率的代价。

跑法： PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/exit_live_parity.py
"""
import asyncio
import glob
import json
import sys
import time

sys.path.insert(0, '.')

import yaml
from backend.strategies import S15CeZlsma, _ce_zlsma
import backend.strategies as S
from backtest.s15_live_parity import stats
import importlib.util
_spec = importlib.util.spec_from_file_location("est", "backtest/exit_speed_test.py")
est = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(est)

BLK = yaml.safe_load(open('strategies.yaml', encoding='utf-8'))['strategies']['S15_CE_ZLSMA']
ST = S15CeZlsma(BLK)
CAP = 5.0
MAXH = 64          # 16 小时 = 64 根 15m（线上 max_hold_hours: 16）

BARS = []
for fp in sorted(glob.glob("backtest/data/*_15m_60d.json")):
    ks = json.load(open(fp, encoding="utf-8"))
    if len(ks) < 800:
        continue
    sym = fp.replace("\\", "/").split("/")[-1].replace("_15m_60d.json", "")
    BARS.append((sym, ks))


class Ctx:
    """线上 ctx 的最小复现：给价 + 给截止到某根的 K 线"""
    def __init__(self, px, ks, upto):
        self._px, self._ks, self._upto = px, ks, upto

    def price(self, sym):
        return self._px

    async def klines(self, sym, interval=None, limit=None):
        return self._ks[max(0, self._upto - 200):self._upto]


class _FakeTime:
    """把 strategies 模块内的 time.time() 钉到历史 K 线时刻。

    为什么必须这么做：_mirror_exit 第 872 行有保护
        if int(ks[-1].get("ts", 0)) < cur_bar: return None
    其中 cur_bar = int(time.time() // 900) * 900 用的是真实系统时间。
    回放 60 天前的 K 线时系统时间远大于 K 线 ts，这一条永远成立 →
    镜像出场一次都不会执行 → 测出来的根本不是同一条路径。
    """
    now = 0.0

    @classmethod
    def time(cls):
        return cls.now


_ORIG_S_TIME = S.time


async def live_path(ks, sym):
    """复现线上引擎：逐根 K 线，用 close 当实时价调 check_exit，峰值取 K 线极值。"""
    ind = _ce_zlsma(ks, 1, 2.0, 50, True, True)
    dirs = ind["dir"]
    n = len(ks)
    trades = []
    S.time = _FakeTime                     # 让 _mirror_exit 活在历史时区
    try:
        i = 80
        while i < n - 2:
            if dirs[i] is None or dirs[i - 1] is None:
                i += 1
                continue
            want = None
            if dirs[i] == 1 and dirs[i - 1] == -1:
                want = "long"
            elif dirs[i] == -1 and dirs[i - 1] == 1:
                want = "short"
            if not want:
                i += 1
                continue
            entry = float(ks[i + 1]["open"])
            if entry <= 0:
                i += 1
                continue
            lo_i = max(0, i - 20 + 1)
            s_lo = min(float(ks[j]["low"]) for j in range(lo_i, i + 1))
            s_hi = max(float(ks[j]["high"]) for j in range(lo_i, i + 1))
            stop = s_lo if want == "long" else s_hi
            risk = (entry - stop) if want == "long" else (stop - entry)
            if risk <= 0 or risk / entry * 100 > CAP:
                i += 1
                continue

            sign = 1 if want == "long" else -1
            entry_ts = float(ks[i + 1].get("ts", 0))
            p = dict(id=len(trades) + 1, symbol=sym, strategy="S15_CE_ZLSMA", side=want,
                     entry_price=entry, stop_price=stop, opened_at=entry_ts,
                     half_closed=0, peak_r=0.0, mfe_pct=0.0, ma_pct=0.0, qty=1.0)
            S._S15_POS_STATE.clear()
            S._S15_MIRROR_STATE.clear()

            half_r = None       # 平半时的 R
            peak = 0.0
            ep = er = ei = None
            j = i + 1
            while j < n:
                o_ = float(ks[j]["open"])
                h_ = float(ks[j]["high"])
                l_ = float(ks[j]["low"])
                c_ = float(ks[j]["close"])
                # 时间轴对齐：判定发生在第 j 根收盘那一刻
                bar_ts = float(ks[j].get("ts", 0))
                _FakeTime.now = bar_ts + 900
                S._S15_ZL_CACHE.clear()     # 缓存键含 cur_bar，历史回放必须清
                # ① 硬止损（与回测同序：先判止损）
                if want == "long" and l_ <= stop:
                    ep, er, ei = min(o_, stop), "hard_stop", j
                    break
                if want == "short" and h_ >= stop:
                    ep, er, ei = max(o_, stop), "hard_stop", j
                    break
                # ② 12h 强平（engine 层；平半后照样平）
                if (j - i) >= MAXH:
                    ep, er, ei = c_, "timeout", j
                    break
                # 峰值用 K 线极值（线上 0.5s 采样能抓到，回测也这么算）
                peak = max(peak, ((h_ - entry) if want == "long" else (entry - l_)) / risk)
                ST._touch_pos_state(p, entry, risk, peak)
                # ③ 其余分支交给线上真函数
                # ⚠ Ctx 喂到 j+2 不是 j+1：_mirror_exit 的保护是
                #   if int(ks[-1]["ts"]) < cur_bar: return None
                #   而 cur_bar = bar_ts + 900（本根收盘后的下一格）。只喂到 j 时
                #   ks[-1].ts = bar_ts < cur_bar 恒成立 → 镜像一次都不执行。
                #   线上能过是因为那一刻交易所已生成下一根；多喂的这根只做
                #   「窗口已生成」占位，指标取 i = len-2 仍指向第 j 根，不偷看未来。
                r = await ST.check_exit(Ctx(c_, ks, j + 2), p)
                if r:
                    if r.startswith("HALF:"):
                        p["half_closed"] = 1
                        half_r = (c_ - entry) * sign / risk
                        j += 1
                        continue
                    kind = ("half_tp" if "半仓再赚" in r else
                            "trail" if "移动止盈" in r else
                            "quickfail" if "快速失败" in r else
                            "mirror" if "镜像" in r else "other")
                    ep, er, ei = c_, kind, j
                    break
                j += 1
            if ep is None:
                ep, er, ei = float(ks[-1]["close"]), "eod", n - 1

            r_r = (ep - entry) * sign / risk
            if half_r is not None:
                r_r = 0.5 * half_r + 0.5 * r_r
            trades.append({"r": r_r, "reason": er, "bars": ei - i,
                           "peak": peak, "exit_ts": ei, "risk_pct": risk / entry * 100})
            # ⚠ 必须跳到出场之后再找下一个入场点 —— 单币单仓串行，
            #   和回测 sim() 的 i = ei + 1 一致，也和实盘的
            #   risk.allow_entry「同币已有仓位拒绝」一致。
            #   写成 i += 1 会变成重叠持仓，笔数虚增 7 倍。
            i = ei + 1
    finally:
        S.time = _ORIG_S_TIME
    return trades


async def main():
    live_all = []
    sim_all = []
    per = []
    for sym, ks in BARS:
        lt = await live_path(ks, sym)
        st_ = est.sim(ks, _ce_zlsma(ks, 1, 2.0, 50, True, True),
                      max_risk_pct=CAP, half_at_r=1.8, half_tp_r=1.0,
                      trail_arm=1.5, trail_dd=1.2, trail_after_half=True,
                      time_stop_always=True, maxh=MAXH,
                      qf_bars=16, qf_min_r=0.5)   # 线上 _quick_fail 4h/0.5R
        live_all += lt
        sim_all += st_
        if lt and st_:
            a, b = stats(lt), stats(st_)
            per.append((sym, a["n"], a["r"], b["n"], b["r"]))

    print("=" * 96)
    print("实盘口径（逐根 close 判定，峰值取极值） vs 回测口径  —— 全部 %d 个币" % len(BARS))
    print("=" * 96)
    A, B = stats(live_all), stats(sim_all)
    print("  %-14s %8s %10s %9s %10s %9s" % ("路径", "笔数", "净R", "胜率", "回撤", "均笔R"))
    print("  " + "-" * 68)
    print("  %-14s %8d %+10.1f %8.1f%% %+10.1f %+9.3f"
          % ("线上路径", A["n"], A["r"], A["win"], A["mdd"], A["avg"]))
    print("  %-14s %8d %+10.1f %8.1f%% %+10.1f %+9.3f"
          % ("回测 sim", B["n"], B["r"], B["win"], B["mdd"], B["avg"]))
    print("  " + "-" * 68)
    d_n = A["n"] - B["n"]
    d_r = A["r"] - B["r"]
    pct = (d_r / abs(B["r"]) * 100) if B["r"] else 0
    print("  %-14s %+8d %+10.1f %9.1f%%" % ("差值", d_n, d_r, pct))

    # 出场结构对比
    from collections import Counter
    ca = Counter(t["reason"] for t in live_all)
    cb = Counter(t["reason"] for t in sim_all)
    print()
    print("  出场结构：")
    keys = sorted(set(ca) | set(cb))
    print("    %-12s %10s %10s" % ("出口", "线上", "回测"))
    for k in keys:
        print("    %-12s %10d %10d" % (k, ca.get(k, 0), cb.get(k, 0)))

    # 逐币差异
    print()
    print("  逐币净 R 差异（线上 - 回测）：")
    diff = [(s, a_r - b_r, a_n, b_n) for s, a_n, a_r, b_n, b_r in per]
    diff.sort(key=lambda x: x[1])
    bad = [x for x in diff if abs(x[1]) > 3.0]
    print("    %-14s %10s %8s %8s" % ("币", "Δ净R", "线上笔", "回测笔"))
    for s, d, an, bn in diff[:6] + (diff[-6:] if len(diff) > 12 else diff[6:]):
        flag = " ←" if abs(d) > 3.0 else ""
        print("    %-14s %+10.1f %8d %8d%s" % (s, d, an, bn, flag))
    print()
    print("  差异 > 3R 的币：%d 个" % len(bad))
    if bad:
        for s, d, an, bn in bad:
            print("    · %-12s Δ%+.1fR" % (s, d))

    print()
    print("=" * 96)
    ok = abs(pct) < 10.0
    print("  判定：结算差 %.1f%%（阈值 ±10%%）→ %s"
          % (pct, "口径可接受，可以接实盘" if ok else "口径偏差过大，需先查清"))
    print("=" * 96)
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
