"""影子模式 —— 用因子给信号打分，但不干预实际交易。

对每个信号同时记录三套规则的判定：
  R1 入场过滤：方向与资金拥挤度冲突时拦下
  R2 方向确认：主动买卖比要与方向一致
  R3 仓位调整：费率极端时建议减半

被拦/被减的信号也会虚拟跟踪到出场，2 周后统计各规则的胜率差异。

⚠️ 2026-11-05 现状（改之前先读 docs/SHADOW_SYSTEM.md）：
  1. 4 个因子（funding_agg / taker_flow / lsr_top_pos / cvd）的采集已关闭，
     省 ~530 MB/天流量。record/settle 照常工作（结算只依赖价格）。
  2. R1/R3 阈值 0.030% 脱离币安实际费率（正常 ±0.005%），492 单从未触发。
  3. R2 拦下的单平均 +0.617R、放行的 +0.556R —— 拦掉的更赚，无正向筛选力。
  4. 结论：暂时不接进实盘。攒够 1000+ 单样本再回头评估（采样办法见文档）。
"""
from __future__ import annotations

import json
import logging
import time

from . import db

log = logging.getLogger(__name__)

# ── 阈值（可调）───────────────────────────────────────────────
FUNDING_HOT_LONG = 0.030    # 费率高于此值 = 多头拥挤（单位 %，0.03%）
FUNDING_HOT_SHORT = -0.010  # 费率低于此值 = 空头拥挤
TAKER_CONFIRM = 1.00        # taker 主动买卖比，>1 买盘强，<1 卖盘强
HALF_SIZE_FUNDING = 0.030   # 超过此绝对值时建议仓位减半


def _factor(sym: str, name: str):
    """读取因子当前值；返回 dict 或 None。"""
    row = db.get_factor(name, sym)
    if not row:
        return None
    try:
        return json.loads(row) if isinstance(row, str) else row
    except (TypeError, ValueError):
        return None


def _num(v, key="latest"):
    if v is None:
        return None
    if isinstance(v, dict):
        x = v.get(key)
        return float(x) if isinstance(x, (int, float)) else None
    return float(v) if isinstance(v, (int, float)) else None


def judge(symbol: str, side: str):
    """对单个信号跑三套规则。返回 dict。"""
    f = _num(_factor(symbol, "funding_agg"))
    taker = _num(_factor(symbol, "taker_flow"))
    lsr = _num(_factor(symbol, "lsr_top_pos"))
    cvd = _num(_factor(symbol, "cvd"))

    r1_block = False
    r1_why = ""
    if f is not None:
        if side == "long" and f > FUNDING_HOT_LONG:
            r1_block, r1_why = True, f"多头拥挤(费率{f:+.3f}%)"
        elif side == "short" and f < FUNDING_HOT_SHORT:
            r1_block, r1_why = True, f"空头拥挤(费率{f:+.3f}%)"

    r2_block = False
    r2_why = ""
    if taker is not None:
        if side == "long" and taker < TAKER_CONFIRM:
            r2_block, r2_why = True, f"买盘不足(taker {taker:.2f})"
        elif side == "short" and taker > TAKER_CONFIRM:
            r2_block, r2_why = True, f"卖盘不足(taker {taker:.2f})"

    r3_half = bool(f is not None and abs(f) >= HALF_SIZE_FUNDING)

    return dict(funding=f, taker=taker, lsr=lsr, cvd=cvd,
                r1_block=r1_block, r1_why=r1_why,
                r2_block=r2_block, r2_why=r2_why,
                r3_half=r3_half)


def record(sig, px, stop, tp, acted: bool):
    """记录一个信号的三套判定（无论实际是否开仓）。"""
    j = judge(sig.symbol, sig.side)
    db.shadow_insert(
        ts=int(time.time()), strategy=sig.strategy, symbol=sig.symbol,
        side=sig.side, strength=float(getattr(sig, "strength", 0) or 0),
        fresh=int(getattr(sig, "fresh", 999) or 999),
        px=float(px), stop=float(stop), tp=float(tp),
        funding=j["funding"], taker=j["taker"], lsr=j["lsr"], cvd=j["cvd"],
        r1=int(j["r1_block"]), r1_why=j["r1_why"],
        r2=int(j["r2_block"]), r2_why=j["r2_why"],
        r3=int(j["r3_half"]), acted=1 if acted else 0)


def settle(price_fn, max_hold_sec=72 * 3600):
    """虚拟结算：检查未结算的影子信号是否触及 stop/tp/超时。"""
    rows = db.shadow_pending()
    if not rows:
        return 0
    now = int(time.time())
    n = 0
    for r in rows:
        px = price_fn(r["symbol"])
        if not px:
            continue
        side, stop, tp, entry = r["side"], r["stop"], r["tp"], r["px"]
        hit = None
        if side == "long":
            if px <= stop:
                hit = "stop"
            elif px >= tp:
                hit = "tp"
        else:
            if px >= stop:
                hit = "stop"
            elif px <= tp:
                hit = "tp"
        if hit is None and now - r["ts"] >= max_hold_sec:
            hit = "timeout"
        if hit:
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            gain = (px - entry) if side == "long" else (entry - px)
            r_mult = round(gain / risk, 3) if hit != "timeout" else round(gain / risk, 3)
            db.shadow_settle(r["id"], hit, r_mult)
            n += 1
    return n
