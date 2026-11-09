# -*- coding: utf-8 -*-
"""光闸结论列的口径测试 —— 防「四闸全过」被误显示成「已开仓」

背景（真实事故，2026-11-09）：
  RSR 06:45 四闸全过、信号已生成，但被风控拦下（skip_reason「已达日目标，
  今日停止开新仓」），页面上却显示绿色「已开仓」。浩哥发现「显示已开仓，实际没有」。

根因：
  strategies.evaluate() 在 append(Signal) 时就写了 why="已开仓"，
  但那一刻还没走 risk.allow_entry 九道闸，也没有任何仓位。

修法：
  ① 策略层只能说「四闸全过」（它不知道风控结果）；
  ② /api/gates 用两样真实事实覆盖结论列：
       持仓表里有这个币        → 「已开仓」
       本根 K 线的最新信号被拒 → 直接显示它的 skip_reason
       都没有                 → 「四闸全过·未成交」

本测试锁死这三条分支，防止以后再退回「凭 ok 字段就说已开仓」。
"""
import asyncio
import sys
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import backend.main as M          # noqa: E402
import backend.strategies as S    # noqa: E402
import backend.quality as Q       # noqa: E402
import backend.db as D            # noqa: E402
import backend.config as C        # noqa: E402

# 用「刚刚」的时间戳，而不是固定的历史 K 线 —— 因为结论列现在要求信号
# 足够新鲜（5 分钟内），用写死的老时间戳会让所有涉及 skip_reason 的用例失真。
import time as _t
BAR = int(_t.time()) - 60        # 1 分钟前的 K 线（新鲜）
SYM = "RSRUSDT"

_fails = []


def ck(name, cond, extra=""):
    print(f"    {'PASS' if cond else 'FAIL'}  {name}{('  → ' + str(extra)) if extra else ''}")
    if not cond:
        _fails.append(name)


def _setup(held_syms=(), signals=None, in_pool=True, eff=0.55):
    """打桩：一个四闸全过的币 + 指定持仓/信号/是否在监控池"""
    S._GATE_STATE.clear()
    S._GATE_STATE[SYM] = {
        "ts": BAR, "dir": "空", "ce": True, "ha": True,
        "bbw": 90.0, "bbw_thr": 70.0, "dev": 1.885, "dev_thr": 0.5,
        "atr_pct": 3.077, "ok": True, "why": "四闸全过",
    }
    Q.monitor_list = lambda: [SYM] if in_pool else []
    # snapshot 打桩：只需 eff / why，别真去算指标
    Q.snapshot = lambda: {"data": {SYM: {"eff": eff, "ok": True, "why": ""}},
                          "monitor": [SYM] if in_pool else []}
    Q.EFF_MIN = 0.30
    D.open_positions = lambda *a, **k: [{"symbol": s} for s in held_syms]
    D.recent_signals = lambda limit=50: (signals or [])
    C.UNIVERSE = [SYM]


def run_gates():
    return asyncio.run(M.api_gates())


def main():
    print("=" * 78)
    print("光闸结论列口径测试")
    print("=" * 78)

    # ── 1. 真在持仓 → 已开仓 ────────────────────────────────
    print("\n  1. 持仓表里有这个币 → 必须显示「已开仓」")
    _setup(held_syms=[SYM])
    d = run_gates()
    r = d["rows"][0]
    ck("opened=True", r.get("opened") is True, r.get("opened"))
    ck("verdict=已开仓", r.get("verdict") == "已开仓", r.get("verdict"))

    # ── 2. 没持仓 + 信号被风控拒 → 显示真实原因 ─────────────────
    print("\n  2. 没持仓 + 本根信号被拒 → 必须显示 skip_reason（事故复现场景）")
    _setup(held_syms=[], signals=[{
        "symbol": SYM, "ts": BAR + 917, "acted": 0,
        "skip_reason": "已达日目标，今日停止开新仓",
    }])
    d = run_gates()
    r = d["rows"][0]
    ck("opened=False", r.get("opened") is False, r.get("opened"))
    ck("verdict≠已开仓", r.get("verdict") != "已开仓", r.get("verdict"))
    ck("verdict=已达日目标…", r.get("verdict") == "已达日目标，今日停止开新仓", r.get("verdict"))
    ck("ok 仍为 True（四闸全过事实不变）", r.get("ok") is True, r.get("ok"))

    # ── 3. 没持仓 + 有信号但没 skip_reason（执行失败等）────────────
    print("\n  3. 没持仓 + 信号无 skip_reason → 「四闸全过·未成交」")
    _setup(held_syms=[], signals=[{
        "symbol": SYM, "ts": BAR + 917, "acted": 0, "skip_reason": "",
    }])
    d = run_gates()
    r = d["rows"][0]
    ck("opened=False", r.get("opened") is False, r.get("opened"))
    ck("verdict=四闸全过·待成交", r.get("verdict") == "四闸全过·待成交", r.get("verdict"))

    # ── 4. 信号是上一根 K 线的（过期）→ 不能拿来当结论 ────────────
    print("\n  4. 信号来自更早的 K 线（过期）→ 不得套用它的 skip_reason")
    _setup(held_syms=[], signals=[{
        "symbol": SYM, "ts": BAR - 3600, "acted": 0,
        "skip_reason": "并发币数已达上限",
    }])
    d = run_gates()
    r = d["rows"][0]
    ck("verdict 不套用旧原因", r.get("verdict") == "四闸全过·待成交", r.get("verdict"))

    # ── 5. 四闸没全过 → 保持原诊断 ──────────────────────────
    print("\n  5. 四闸没全过 → 结论用诊断原文")
    S._GATE_STATE.clear()
    S._GATE_STATE[SYM] = {
        "ts": BAR, "dir": "多", "ce": False, "ha": True,
        "bbw": 3.0, "bbw_thr": 70.0, "dev": None, "dev_thr": 0.5,
        "atr_pct": 1.2, "ok": False, "why": "CE没翻转",
    }
    D.open_positions = lambda *a, **k: []
    D.recent_signals = lambda limit=50: []
    d = run_gates()
    r = d["rows"][0]
    ck("opened=False", r.get("opened") is False, r.get("opened"))
    ck("verdict=CE没翻转", r.get("verdict") == "CE没翻转", r.get("verdict"))

    # ── 6. 策略层文案不得再写「已开仓」 ────────────────────────
    print("\n  6. strategies.py 源码里不得再出现「已开仓」字样")
    src = open(os.path.join(ROOT, "backend", "strategies.py"), encoding="utf-8").read()
    ck("无「已开仓」硬编码", "已开仓" not in src)

    # ── 7. 这一轮四闸没过 + 有更新的信号 → 必须报诊断原文 ─────────
    print("\n  7. 四闸没过（CE没翻）但有更新的被拒信号 → 报诊断原文，不能自相矛盾")
    _setup(held_syms=[], signals=[{
        "symbol": SYM, "ts": BAR + 3600, "acted": 0,
        "skip_reason": "已达日目标，今日停止开新仓",
    }])
    S._GATE_STATE[SYM]["ok"] = False
    S._GATE_STATE[SYM]["ce"] = False
    S._GATE_STATE[SYM]["why"] = "CE没翻转"
    d = run_gates()
    r = d["rows"][0]
    ck("verdict=CE没翻转（不套日目标）", r.get("verdict") == "CE没翻转", r.get("verdict"))
    ck("opened=False", r.get("opened") is False, r.get("opened"))

    # ── 8. 重置场景：刚点了「今日重置」，旧拒绝原因必须立刻失效 ─────────
    print("\n  8. 重置后：6 分钟前的「已达日目标」旧信号不得再挂着")
    _setup(held_syms=[], signals=[{
        "symbol": SYM, "ts": BAR - 360, "acted": 0,
        "skip_reason": "已达日目标，今日停止开新仓",
    }])
    d = run_gates()
    r = d["rows"][0]
    ck("旧拒绝原因已失效", r.get("verdict") == "四闸全过·待成交", r.get("verdict"))
    ck("不再是「已达日目标」", "已达日目标" not in (r.get("verdict") or ""), r.get("verdict"))

    # ── 9. 池外的币：四闸是历史残留，绝不能显示「待成交」─────────────
    print("\n  9. 池外 + 四闸历史全过 → 必须说「池外」，不能挂在「待成交」")
    _setup(held_syms=[], in_pool=False, eff=0.153)
    d = run_gates()
    r = d["rows"][0]
    ck("in_monitor=False", r.get("in_monitor") is False, r.get("in_monitor"))
    ck("不再是「待成交」", "待成交" not in (r.get("verdict") or ""), r.get("verdict"))
    ck("说明是池外", "池外" in (r.get("verdict") or ""), r.get("verdict"))
    ck("带上 EFF 原因", "0.15" in (r.get("verdict") or ""), r.get("verdict"))

    # ── 10. 池外但 EFF 达标（池满被挤）→ 说「池满未入选」───────────
    print("\n  10. 池外但 EFF 达标 → 是池满被挤，不是指标差")
    _setup(held_syms=[], in_pool=False, eff=0.42)
    d = run_gates()
    r = d["rows"][0]
    ck("池满未入选", r.get("verdict") == "池外·池满未入选", r.get("verdict"))

    # ── 11. 池外但持仓里 → 仍是「已开仓」（持仓优先级最高）───────────
    print("\n  11. 池外但真在持仓 → 仍显示「已开仓」")
    _setup(held_syms=[SYM], in_pool=False, eff=0.20)
    d = run_gates()
    r = d["rows"][0]
    ck("持仓优先", r.get("verdict") == "已开仓", r.get("verdict"))
    ck("opened=True", r.get("opened") is True, r.get("opened"))

    print("\n" + "=" * 78)
    print(f"结果：{'全部通过' if not _fails else str(len(_fails)) + ' 项失败：' + ', '.join(_fails)}")
    print("=" * 78)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
