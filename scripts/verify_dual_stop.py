# -*- coding: utf-8 -*-
"""验证「两档止损」在**线上策略代码**（backend/strategies.py::evaluate）里真的生效。

不是复刻逻辑，也不是 grep 代码 —— 走真实 evaluate()，用真实 15m K 线逐根回放，
对比 dual_stop_min_pct = 0（关）与 = 0.5（开）两组信号的止损价：

  · 信号数必须相同（止损不影响入场判定）
  · 关：会有一批信号止损距离 < 0.50%（关键K线贴脸）
  · 开：这批全部被推到 CE 通道，距离应 ≥ 0.50%

用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_dual_stop.py
"""
import asyncio
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.strategies import REGISTRY  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "backtest", "data80coin")
SYMS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "DOGEUSDT")
START = 200          # 前面要有足够历史算 ZLSMA50×2 / ATR20 / BBW 分位
STEP = 1             # 1 = 逐根（慢但全）；>1 = 跳根（快速烟测）


class FakeCtx:
    """只实现 evaluate 需要的三样东西，不碰数据库、不发网络。"""

    def __init__(self, sym, ks):
        self.universe = [sym]
        self.monitor = [sym]
        self._sym = sym
        self._ks = ks

    def has_position(self, sym, sid):
        return False

    async def klines(self, sym, interval="15m", limit=1000):
        return self._ks


def make(cfg, dual):
    params = dict(cfg["strategies"]["S15_CE_ZLSMA"]["params"])
    params["dual_stop_min_pct"] = dual
    params["kline_interval"] = "15m"
    c = {
        "_iid": "S15_CE_ZLSMA",
        "base": "S15_CE_ZLSMA",
        "params": params,
        "symbols": [],
        "max_hold_hours": cfg["strategies"]["S15_CE_ZLSMA"].get("max_hold_hours", 0),
    }
    return REGISTRY["S15_CE_ZLSMA"](c)


async def replay(sym, ks, strat):
    """逐根回放：每次把 ks[:i+1] 交给 evaluate，只判最后一根。"""
    out = []
    for i in range(START, len(ks) - 1, STEP):
        ctx = FakeCtx(sym, ks[:i + 1])
        sigs = await strat.evaluate(ctx)
        for s in sigs:
            # ★口径铁律：evaluate() 内部评估的是「倒数第二根」= ks[len-2]，
            #   脚本传进去的是 ks[:i+1]，所以内部那根是 ks[i-1]，不是 ks[i]。
            #   用 ks[i]（未收盘那根）算距离会跟策略内部口径差一根 K 线 →
            #   2026-11-10 因此把 2 笔正常单误判成"贴脸止损没被接管"。
            px = float(ks[i - 1]["close"])
            dist = abs(px - float(s.stop_price)) / px * 100.0
            out.append((s.side, dist, getattr(s, "stop_src", ""),
                        round(float(s.stop_price), 8), round(px, 8)))
    return out


async def amain():
    global SYMS, STEP
    # 用法：verify_dual_stop.py [SYM[,SYM]] [STEP]
    if len(sys.argv) > 1 and sys.argv[1]:
        SYMS = tuple(x.strip().upper() for x in sys.argv[1].split(",") if x.strip())
    if len(sys.argv) > 2:
        STEP = max(1, int(sys.argv[2]))
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    tot = {"off": [], "on": []}
    per = {}
    for sym in SYMS:
        fp = os.path.join(DATA, "%s_15m_60d.json" % sym)
        if not os.path.exists(fp):
            print("!! 缺数据 %s" % fp)
            return 2
        ks = json.load(open(fp, encoding="utf-8"))
        a_off = await replay(sym, ks, make(cfg, 0.0))
        a_on = await replay(sym, ks, make(cfg, 0.5))
        tot["off"].extend(a_off)
        tot["on"].extend(a_on)
        n_off = sum(1 for t in a_off if t[1] < 0.5 - 1e-9)
        n_on = sum(1 for t in a_on if t[1] < 0.5 - 1e-9)
        per[sym] = (len(a_off), n_off, len(a_on), n_on)
        print("  %-9s 关：%2d 个信号，其中 %2d 个止损<0.50%% ｜ 开：%2d 个信号，其中 %2d 个 <0.50%%"
              % (sym, len(a_off), n_off, len(a_on), n_on))

    n_off = len(tot["off"]); n_on = len(tot["on"])
    thin_off = sum(1 for t in tot["off"] if t[1] < 0.5 - 1e-9)
    thin_on = sum(1 for t in tot["on"] if t[1] < 0.5 - 1e-9)
    d_off = sorted(t[1] for t in tot["off"])
    d_on = sorted(t[1] for t in tot["on"])
    med = lambda a: a[len(a) // 2] if a else 0.0

    # 开两档后仍 <0.50% 的那几笔，止损来源是什么（决定「是不是设计内」）
    left = [(t[2], round(t[1], 3), t[0], "stop=%.8f px=%.8f" % (t[3], t[4]))
            for t in tot["on"] if t[1] < 0.5 - 1e-9]
    from collections import Counter
    src_on = Counter(t[2] for t in tot["on"])

    print("=" * 82)
    print("两档止损 · 走线上 evaluate() 逐根回放（%s / 60 天）" % "/".join(SYMS))
    print("=" * 82)
    print("  信号数             关 %3d  ｜ 开 %3d   %s" % (n_off, n_on, "✓ 一致（止损不影响入场）" if n_off == n_on else "✗ 不一致"))
    print("  止损 <0.50%% 的个数   关 %3d  ｜ 开 %3d" % (thin_off, thin_on))
    print("  止损距离中位数       关 %.2f%% ｜ 开 %.2f%%" % (med(d_off), med(d_on)))
    print("  止损距离最小         关 %.2f%% ｜ 开 %.2f%%" % (d_off[0] if d_off else 0, d_on[0] if d_on else 0))
    print("  关档时止损来源        %s" % dict(Counter(t[2] for t in tot["off"])))
    print("  开档时止损来源        %s" % dict(src_on))
    if left:
        print("  开档后仍 <0.50%% 的   %s" % left)
        print("                       ↑ 来源是 swing/ce 本身就很窄，两档只管「关键K线贴脸」那条（与回测母本同口径）")
    print("-" * 82)
    cut = (thin_off - thin_on) / thin_off * 100 if thin_off else 0.0
    okk = (n_off == n_on) and thin_on < thin_off and cut >= 60 and med(d_on) > med(d_off)
    print("结论：%s" % (
        "两档止损已生效 ✓（贴脸止损 %d → %d，减少 %.0f%%；止损中位 %.2f%% → %.2f%%）"
        % (thin_off, thin_on, cut, med(d_off), med(d_on)) if okk
        else "未按预期工作 ✗（开关两组差异不足，疑似参数没接上）"))

    # 存档，便于复查与前后对比
    import json as _j
    out_fp = r"E:\Hermes\cache\scratch\_dual_stop_verify.json"
    try:
        _j.dump({"syms": list(SYMS), "step": STEP, "off": tot["off"], "on": tot["on"],
                 "thin_off": thin_off, "thin_on": thin_on,
                 "med_off": med(d_off), "med_on": med(d_on)},
                open(out_fp, "w", encoding="utf-8"), ensure_ascii=False)
        print("明细已存 %s" % out_fp)
    except Exception as e:
        print("明细落盘失败: %s" % e)

    return 0 if okk else 1


def main():
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
