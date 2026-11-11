"""S15 回测引擎：对比不同出场规则的表现。

用途
----
系统当前只实现了「优化模式」出场（ATR 止损 + 1.5R 止盈），没有
ZLSMA 趋势反转出场。这个脚本用真实历史 K 线，对比三种出场规则：

  baseline  现状：ATR×5.0 止损 + 1.5R 止盈（当前线上跑的就是这个）
  overlay   叠加：在上面基础上，再加「收盘价穿越 ZLSMA 就平仓」
  original  原版：关键 K 线高低点止损 + 收盘穿越 ZLSMA 止盈（pine 的另一种模式）

设计原则
--------
1. 指标计算完全复用 backend.strategies 的 _linreg / _rma / _ce_zlsma，
   保证回测和实盘是同一套数学，不会「回测好看实盘跑偏」。
2. 入场用第 i+1 根的 open 成交（信号出现在第 i 根收盘后），
   比用信号那根的 close 更接近真实。
3. 出场用 bar 的 high/low 判断是否触及止损/止盈（挂单真实触发方式），
   ZLSMA 出场用收盘价判断（收盘才确认穿越）。
4. 手续费、滑点、多空分开算，不做「理想成交」。

用法
----
    python -m backtest.s15_backtest --symbols 20 --days 15
    python -m backtest.s15_backtest --symbols 100 --days 30 --interval 15m
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import binance                     # noqa: E402
from backend.strategies import _linreg, _rma   # noqa: E402

CACHE = ROOT / "backtest" / "data"
CACHE.mkdir(parents=True, exist_ok=True)

FEE = 0.0005           # taker 双边各收一次
SLIP = 0.0003          # 模拟盘用的滑点

# 出场规则定义。
# 粗暴的「价格一穿 ZLSMA 就跑」在震荡市会疯狂进出：实测 20 币 20 天
# 触发 2720 次 zlsma 出场（占 96%），手续费吃掉 220R，把毛盈利全抹平。
# 真正的「趋势反转」需要确认，所以这里并排 4 种更克制的写法。
EXIT_MODES = {
    # 现状：只有 ATR 止损 + 1.5R 止盈，没有趋势出场
    "baseline":     {"zlsma": 0},
    # 一穿就走（最激进，用来当反面参照）
    "zlsma_1bar":   {"zlsma": 1},
    # 连续 2 根收在均线另一侧才算反转
    "zlsma_2bar":   {"zlsma": 2},
    # 只在已有浮盈时才用趋势出场保护利润；亏损时仍交给 ATR 止损，
    # 给趋势留回踩空间 —— 这是趋势跟踪的标准用法
    "zlsma_profit": {"zlsma": 1, "only_profit": True, "min_profit_r": 0.3},
    # 收盘穿越 且 ZLSMA 本身斜率已经掉头（真正的方向改变）
    "zlsma_slope":  {"zlsma": 1, "need_slope": True},
    # pine 的原版模式：关键K线止损 + 无固定止盈
    "original":     {"zlsma": 1, "keybar_stop": True, "no_tp": True},
}


# ── 数据 ────────────────────────────────────────────────────────
def cache_path(symbol, interval, days):
    return CACHE / f"{symbol}_{interval}_{days}d.json"


async def fetch_klines(symbol, interval, days, use_cache=True):
    """拉够 days 天的 K 线。币安单次最多 1500 根，不够就分页往前补。"""
    p = cache_path(symbol, interval, days)
    if use_cache and p.exists() and time.time() - p.stat().st_mtime < 86400 * 3:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}[interval]
    need = int(days * 86400 / secs) + 300          # 多拉 300 根给指标预热
    out = []
    end = int(time.time() * 1000)
    while len(out) < need:
        limit = min(1500, need - len(out))
        try:
            raw = await binance._get(binance.FUT, "/fapi/v1/klines",
                                     {"symbol": symbol, "interval": interval,
                                      "limit": limit, "endTime": end})
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {symbol} 拉取失败: {exc}")
            return []
        if not raw:
            break
        batch = []
        for k in raw:
            try:
                o, h, l, c, v = (float(k[1]), float(k[2]), float(k[3]),
                                 float(k[4]), float(k[5]))
            except (TypeError, ValueError, IndexError):
                return []
            if not all(math.isfinite(x) for x in (o, h, l, c, v)):
                return []
            if min(o, h, l, c) <= 0 or h < l:
                return []
            batch.append({"ts": int(k[0] // 1000), "open": o, "high": h,
                          "low": l, "close": c, "volume": v})
        out = batch + out
        end = int(raw[0][0]) - 1
        if len(raw) < limit:
            break
        await asyncio.sleep(0.12)                  # 别打太快
    out.sort(key=lambda x: x["ts"])
    p.write_text(json.dumps(out), encoding="utf-8")
    return out


# ── 指标 ────────────────────────────────────────────────────────
def build_indicators(ks, ce_len=22, ce_mult=3.0, z_len=50, use_close=True):
    closes = [k["close"] for k in ks]
    highs = [k["high"] for k in ks]
    lows = [k["low"] for k in ks]
    n = len(ks)

    # ZLSMA：lsma + (lsma - lsma2)，与实盘同一实现
    lsma1 = _linreg(closes, z_len, 0)
    lsma2 = _linreg(lsma1, z_len, 0)
    zl = [None if (a is None or b is None) else a + (a - b)
          for a, b in zip(lsma1, lsma2)]

    # 真实波幅 -> Wilder ATR
    tr = [None] * n
    for i in range(1, n):
        pc = closes[i - 1]
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
    atr_ce = _rma(tr, ce_len)              # CE 用的 ATR
    atr_raw = _rma(tr, ce_len)             # 出场止损用的 ATR（pine 里同一条）

    # Chandelier Exit
    long_stop = [None] * n
    short_stop = [None] * n
    dirs = [1] * n
    for i in range(n):
        if i < ce_len or atr_ce[i] is None:
            continue
        win_h = closes[i - ce_len + 1:i + 1] if use_close else highs[i - ce_len + 1:i + 1]
        win_l = closes[i - ce_len + 1:i + 1] if use_close else lows[i - ce_len + 1:i + 1]
        a = ce_mult * atr_ce[i]
        ls_raw = float(max(win_h)) - a
        ss_raw = float(min(win_l)) + a
        ls_prev = long_stop[i - 1] if long_stop[i - 1] is not None else ls_raw
        ss_prev = short_stop[i - 1] if short_stop[i - 1] is not None else ss_raw
        long_stop[i] = max(ls_raw, ls_prev) if closes[i - 1] > ls_prev else ls_raw
        short_stop[i] = min(ss_raw, ss_prev) if closes[i - 1] < ss_prev else ss_raw
        if closes[i] > ss_prev:
            dirs[i] = 1
        elif closes[i] < ls_prev:
            dirs[i] = -1
        else:
            dirs[i] = dirs[i - 1]

    return {"zlsma": zl, "dirs": dirs, "atr": atr_raw,
            "long_stop": long_stop, "short_stop": short_stop}


# ── 模拟 ────────────────────────────────────────────────────────
def _check_exit(ks, ind, pos, i, cfg, max_hold_bars):
    """判断第 i 根是否该出场。返回 (原因, 价格) 或 None。"""
    zl = ind["zlsma"]
    k = ks[i]
    # 1) 挂单优先：止损 / 止盈
    if pos["side"] == "long":
        if k["low"] <= pos["stop"]:
            return ("stop", pos["stop"])
        if pos["tp"] and k["high"] >= pos["tp"]:
            return ("tp", pos["tp"])
    else:
        if k["high"] >= pos["stop"]:
            return ("stop", pos["stop"])
        if pos["tp"] and k["low"] <= pos["tp"]:
            return ("tp", pos["tp"])

    # 2) 趋势反转出场
    need = cfg.get("zlsma", 0)
    if need:
        ok = True
        z_prev = zl[i - 1]
        if z_prev is None:
            ok = False
        if need > 1:
            for back in range(need):
                j = i - back
                if j < 1 or zl[j] is None:
                    ok = False
                    break
                if pos["side"] == "long" and ks[j]["close"] >= zl[j]:
                    ok = False
                    break
                if pos["side"] == "short" and ks[j]["close"] <= zl[j]:
                    ok = False
                    break
        if ok and cfg.get("only_profit"):
            sign = 1 if pos["side"] == "long" else -1
            profit_r = (k["close"] - pos["entry"]) * sign / pos["risk"]
            if profit_r < cfg.get("min_profit_r", 0.0):
                ok = False
        if ok and cfg.get("need_slope"):
            if zl[i - 2] is None:
                ok = False
            else:
                slope = z_prev - zl[i - 2]
                if pos["side"] == "long" and slope >= 0:
                    ok = False
                if pos["side"] == "short" and slope <= 0:
                    ok = False
        if ok:
            if pos["side"] == "long" and k["close"] < z_prev:
                return ("zlsma", k["close"])
            if pos["side"] == "short" and k["close"] > z_prev:
                return ("zlsma", k["close"])

    # 3) 超时
    if i - pos["entry_i"] >= max_hold_bars:
        return ("timeout", k["close"])
    return None


def _close_trade(pos, px, why, i, ts=None):
    """把一次平仓结算成一条成交记录（含费用，R 用净额算）。"""
    sign = 1 if pos["side"] == "long" else -1
    gross = (px - pos["entry"]) * sign
    fees = (pos["entry"] + px) * FEE
    slip = (pos["entry"] + px) * SLIP * 0.5
    net = gross - fees - slip
    return {
        "side": pos["side"], "entry": pos["entry"], "exit": px, "why": why,
        "net": net, "r": net / pos["risk"], "r_gross": gross / pos["risk"],
        "fee_r": (fees + slip) / pos["risk"],
        "bars": i - pos["entry_i"], "sym": pos.get("sym", ""), "ts": ts,
    }


def simulate_portfolio(datasets, mode, max_pos=8, sl_atr=5.0, rr=1.5,
                       max_hold_bars=288, allow_long=True, allow_short=True):
    """多币组合模拟 —— 带全局持仓上限，这才是实盘的真实约束。

    单独跑每个币会严重高估交易次数：实盘 8 个仓位满了之后，再多信号
    也只能看着。而这个策略的盈利恰恰集中在少数几笔大趋势上，能抢到
    哪几笔，完全取决于仓位空不空。

    datasets: [(sym, ks, ind), ...]
    """
    cfg = EXIT_MODES[mode]
    idx_map = {sym: {k["ts"]: i for i, k in enumerate(ks)}
               for sym, ks, ind in datasets}
    all_ts = sorted({k["ts"] for _, ks, _ in datasets for k in ks})
    by_sym = {sym: (ks, ind) for sym, ks, ind in datasets}

    open_pos = {}
    trades = []

    for t in all_ts:
        # ── ① 先处理出场（已有的仓位） ──
        for sym in list(open_pos):
            ks, ind = by_sym[sym]
            i = idx_map[sym].get(t)
            if i is None or i < 2:
                continue
            hit = _check_exit(ks, ind, open_pos[sym], i, cfg, max_hold_bars)
            if hit:
                why, px = hit
                trades.append(_close_trade(open_pos[sym], px, why, i, ts=t))
                del open_pos[sym]

        # ── ② 再找入场（受全局仓位上限约束） ──
        if len(open_pos) >= max_pos:
            continue
        cands = []
        for sym, (ks, ind) in by_sym.items():
            if sym in open_pos:
                continue
            i = idx_map[sym].get(t)
            if i is None or i < 2 or i + 1 >= len(ks):
                continue
            zl, dirs, atr = ind["zlsma"], ind["dirs"], ind["atr"]
            j = i
            if zl[j] is None or atr[j] is None or atr[j] <= 0:
                continue
            long_ok = (allow_long and dirs[j] == 1
                       and ks[j]["close"] > zl[j] and ks[j]["close"] > ks[j]["open"])
            short_ok = (allow_short and dirs[j] == -1
                        and ks[j]["close"] < zl[j] and ks[j]["close"] < ks[j]["open"])
            if not (long_ok or short_ok):
                continue
            # 信号强度：偏离均线的幅度（以 ATR 计），用来排序抢仓位
            strength = abs(ks[j]["close"] - zl[j]) / atr[j]
            cands.append((strength, sym, "long" if long_ok else "short"))

        cands.sort(reverse=True)
        for strength, sym, side in cands:
            if len(open_pos) >= max_pos:
                break
            ks, ind = by_sym[sym]
            i = idx_map[sym].get(t)
            if i is None:
                continue
            atr = ind["atr"]
            px = ks[i + 1]["open"]
            if px <= 0:
                continue
            if cfg.get("keybar_stop"):
                stop = ks[i]["low"] if side == "long" else ks[i]["high"]
                tp = None
            else:
                a = atr[i]
                if side == "long":
                    stop = px - a * sl_atr
                    tp = px + (px - stop) * rr
                else:
                    stop = px + a * sl_atr
                    tp = px - (stop - px) * rr
            if stop <= 0 or (tp is not None and tp <= 0):
                continue
            if side == "long" and stop >= px:
                continue
            if side == "short" and stop <= px:
                continue
            open_pos[sym] = {"side": side, "entry": px, "stop": stop, "tp": tp,
                             "entry_i": i + 1, "risk": abs(px - stop) or 1e-9,
                             "sym": sym}

    return trades


def simulate(ks, ind, mode, sl_atr=5.0, rr=1.5, max_hold_bars=288,
             allow_long=True, allow_short=True):
    """逐根模拟。返回成交列表。

    信号在 bar i 收盘后确认，用 bar i+1 的 open 成交。
    出场逐根检查：先看止损/止盈是否被 high/low 触及，再看趋势反转出场。
    """
    cfg = EXIT_MODES[mode]
    zl, dirs, atr = ind["zlsma"], ind["dirs"], ind["atr"]
    n = len(ks)
    trades = []
    pos = None          # {side, entry, stop, tp, entry_i, risk}

    for i in range(2, n - 1):
        # ---------- 持仓中：检查出场 ----------
        if pos is not None:
            k = ks[i]
            hit = None
            if pos["side"] == "long":
                # 保守：同一根内若止损止盈都可能触及，按止损算（悲观）
                if k["low"] <= pos["stop"]:
                    hit = ("stop", pos["stop"])
                elif pos["tp"] and k["high"] >= pos["tp"]:
                    hit = ("tp", pos["tp"])
            else:
                if k["high"] >= pos["stop"]:
                    hit = ("stop", pos["stop"])
                elif pos["tp"] and k["low"] <= pos["tp"]:
                    hit = ("tp", pos["tp"])

            # ── 趋势反转出场 ──
            need = cfg.get("zlsma", 0)
            if hit is None and need:
                ok = True
                z_prev = zl[i - 1]

                # 确认根数：连续 need 根收在均线另一侧
                if need > 1:
                    for back in range(need):
                        j = i - back
                        if j < 1 or zl[j] is None:
                            ok = False
                            break
                        if pos["side"] == "long" and ks[j]["close"] >= zl[j]:
                            ok = False
                            break
                        if pos["side"] == "short" and ks[j]["close"] <= zl[j]:
                            ok = False
                            break

                # 只在已有足够浮盈时才走（亏损时交给 ATR 止损）
                if ok and cfg.get("only_profit"):
                    sign = 1 if pos["side"] == "long" else -1
                    profit_r = (k["close"] - pos["entry"]) * sign / pos["risk"]
                    if profit_r < cfg.get("min_profit_r", 0.0):
                        ok = False

                # 均线本身必须已经转向（斜率与持仓方向相反）
                if ok and cfg.get("need_slope"):
                    if z_prev is None or zl[i - 2] is None:
                        ok = False
                    else:
                        slope = z_prev - zl[i - 2]
                        if pos["side"] == "long" and slope >= 0:
                            ok = False
                        if pos["side"] == "short" and slope <= 0:
                            ok = False

                if ok:
                    if pos["side"] == "long" and k["close"] < z_prev:
                        hit = ("zlsma", k["close"])
                    elif pos["side"] == "short" and k["close"] > z_prev:
                        hit = ("zlsma", k["close"])

            # 超时出场
            if hit is None and i - pos["entry_i"] >= max_hold_bars:
                hit = ("timeout", k["close"])

            if hit:
                why, px = hit
                sign = 1 if pos["side"] == "long" else -1
                gross = (px - pos["entry"]) * sign
                fees = (pos["entry"] + px) * FEE
                slip = (pos["entry"] + px) * SLIP * 0.5
                net = gross - fees - slip
                # R 必须用「净盈亏 / 每笔风险」，不能用毛盈亏 —— 否则
                # 高频出入的策略会被手续费吃掉却仍显示为正 R。实测：
                # overlay 2852 笔，每笔费用 ≈0.077R，光费用就 ~220R，
                # 用毛 R 算出来 +25R 是假象。
                risk_amt = pos["risk"] or 1e-9
                r_mult = gross / risk_amt          # 毛 R（看方向对不对）
                r_net = net / risk_amt             # 净 R（真实赚亏）
                trades.append({
                    "side": pos["side"], "entry": pos["entry"], "exit": px,
                    "why": why, "net": net, "r": r_net, "r_gross": r_mult,
                    "fee_r": (fees + slip) / risk_amt,
                    "bars": i - pos["entry_i"],
                })
                pos = None
            else:
                continue

        if pos is not None:
            continue

        # ---------- 空仓：找入场信号 ----------
        j = i                      # 信号确认根
        if zl[j] is None or atr[j] is None or atr[j] <= 0:
            continue
        px = ks[i + 1]["open"]     # 下一根开盘成交
        if px <= 0:
            continue

        long_ok = (allow_long and dirs[j] == 1
                   and ks[j]["close"] > zl[j] and ks[j]["close"] > ks[j]["open"])
        short_ok = (allow_short and dirs[j] == -1
                    and ks[j]["close"] < zl[j] and ks[j]["close"] < ks[j]["open"])
        if not (long_ok or short_ok):
            continue

        side = "long" if long_ok else "short"
        if cfg.get("keybar_stop"):
            # 原版：关键 K 线高低点止损
            stop = ks[j]["low"] if side == "long" else ks[j]["high"]
            tp = None if cfg.get("no_tp") else None
        else:
            # 现状 / 各变体：ATR 止损 + R 倍止盈
            a = atr[j]
            if side == "long":
                stop = px - a * sl_atr
                tp = px + (px - stop) * rr
            else:
                stop = px + a * sl_atr
                tp = px - (stop - px) * rr
            if stop <= 0 or (tp is not None and tp <= 0):
                continue

        if side == "long" and stop >= px:
            continue
        if side == "short" and stop <= px:
            continue

        risk = abs(px - stop) or 1e-9
        pos = {"side": side, "entry": px, "stop": stop, "tp": tp,
               "entry_i": i + 1, "risk": risk}

    return trades


# ── 统计 ────────────────────────────────────────────────────────
def stats(trades, label=""):
    if not trades:
        return {"label": label, "n": 0, "win": 0.0, "avg_r": 0.0,
                "total_r": 0.0, "pf": 0.0, "maxdd_r": 0.0,
                "long_win": 0.0, "short_win": 0.0,
                "why": {}}
    n = len(trades)
    wins = [t for t in trades if t["net"] > 0]
    gross_win = sum(t["net"] for t in trades if t["net"] > 0)
    gross_loss = -sum(t["net"] for t in trades if t["net"] < 0)

    # 用 R 的累计曲线算最大回撤
    curve, peak, mdd = 0.0, 0.0, 0.0
    for t in trades:
        curve += t["r"]
        peak = max(peak, curve)
        mdd = min(mdd, curve - peak)

    longs = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]
    why = {}
    for t in trades:
        why[t["why"]] = why.get(t["why"], 0) + 1

    return {
        "label": label, "n": n,
        "win": len(wins) / n * 100,
        "avg_r": sum(t["r"] for t in trades) / n,
        "total_r": sum(t["r"] for t in trades),
        "gross_r": sum(t.get("r_gross", t["r"]) for t in trades),
        "fee_r": sum(t.get("fee_r", 0.0) for t in trades),
        "pf": (gross_win / gross_loss) if gross_loss > 0 else 99.9,
        "maxdd_r": mdd,
        "long_win": (sum(1 for t in longs if t["net"] > 0) / len(longs) * 100) if longs else 0,
        "short_win": (sum(1 for t in shorts if t["net"] > 0) / len(shorts) * 100) if shorts else 0,
        "why": why,
    }


# ── 主流程 ──────────────────────────────────────────────────────
async def run(symbols, interval, days, ce_len, ce_mult, z_len, sl_atr, rr,
              max_pos=8, portfolio=True):
    print(f"\n{'='*78}")
    print(f"S15 出场规则对比回测  |  {len(symbols)} 个币  |  {interval}  |  {days} 天")
    print(f"参数: ceLen={ce_len} ceMult={ce_mult} zLen={z_len} "
          f"slAtr={sl_atr} rr={rr}")
    print(f"模式: {'组合级（全局最多 ' + str(max_pos) + ' 仓，接近实盘）' if portfolio else '单币独立（高估交易次数）'}")
    print(f"{'='*78}\n")

    hv = {"15m": 288, "1h": 72, "5m": 864}.get(interval, 288)   # 72 小时
    datasets = []
    for idx, sym in enumerate(symbols, 1):
        ks = await fetch_klines(sym, interval, days)
        if len(ks) < 300:
            print(f"  [{idx}/{len(symbols)}] {sym:14s} 数据不足({len(ks)}根)，跳过")
            continue
        datasets.append((sym, ks, build_indicators(ks, ce_len, ce_mult, z_len)))
        if idx % 10 == 0 or idx == len(symbols):
            print(f"  已加载 {idx}/{len(symbols)} 个币")

    print()
    all_tr = {}
    for mode in EXIT_MODES:
        if portfolio:
            tr = simulate_portfolio(datasets, mode, max_pos=max_pos,
                                    sl_atr=sl_atr, rr=rr, max_hold_bars=hv)
        else:
            tr = []
            for _, ks, ind in datasets:
                tr.extend(simulate(ks, ind, mode, sl_atr, rr, hv))
        all_tr[mode] = tr

    print(f"{'='*78}")
    print(f"汇总（{len(datasets)} 个币）")
    print(f"{'='*78}")
    rows = [stats(tr, m) for m, tr in all_tr.items()]

    print(f"{'出场规则':<13} {'笔数':>6} {'胜率':>7} {'净均R':>7} {'净总R':>9} "
          f"{'毛总R':>9} {'费用R':>8} {'盈亏比':>7} {'最大回撤R':>10}")
    print("-" * 92)
    best = max(rows, key=lambda r: r["total_r"]) if rows else None
    for r in rows:
        mark = "  ← 最优" if (best and r is best) else ""
        print(f"{r['label']:<13} {r['n']:>6} {r['win']:>6.1f}% {r['avg_r']:>7.3f} "
              f"{r['total_r']:>9.2f} {r['gross_r']:>9.2f} {r['fee_r']:>8.1f} "
              f"{r['pf']:>7.2f} {r['maxdd_r']:>10.2f}{mark}")
    print()
    for r in rows:
        print(f"  {r['label']:<13} 出场原因: {r['why']}")
        print(f"  {'':<13} 多胜率 {r['long_win']:.1f}%  空胜率 {r['short_win']:.1f}%")

    out = ROOT / "backtest" / f"result_{int(time.time())}.json"
    out.write_text(json.dumps({"params": {"symbols": len(datasets), "interval": interval,
                                          "days": days, "ce_len": ce_len,
                                          "ce_mult": ce_mult, "z_len": z_len,
                                          "sl_atr": sl_atr, "rr": rr,
                                          "max_pos": max_pos, "portfolio": portfolio},
                               "summary": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n结果已存: {out}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=20, help="跑多少个币")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--ce-len", type=int, default=22)
    ap.add_argument("--ce-mult", type=float, default=3.0)
    ap.add_argument("--z-len", type=int, default=50)
    ap.add_argument("--sl-atr", type=float, default=5.0)
    ap.add_argument("--rr", type=float, default=1.5)
    ap.add_argument("--max-pos", type=int, default=8, help="全局最大同时持仓")
    ap.add_argument("--single", action="store_true", help="单币独立模式（不推荐）")
    ap.add_argument("--nocache", action="store_true")
    args = ap.parse_args()

    # 币种列表：优先用候选池，否则用默认池
    syms = []
    cand = ROOT / "data" / "candidates.json"
    if cand.exists():
        try:
            d = json.loads(cand.read_text(encoding="utf-8"))
            syms = list((d.get("meta") or {}).get("picked") or d.get("symbols") or [])
            if not syms:
                syms = [s for s in (d.get("list") or []) if isinstance(s, str)]
        except Exception:  # noqa: BLE001
            pass
    if not syms:
        from backend import config
        syms = list(config.DEFAULT_UNIVERSE)
    syms = [s for s in syms if isinstance(s, str) and s.endswith("USDT")][: args.symbols]
    if not syms:
        print("没有可用币种")
        return
    asyncio.run(run(syms, args.interval, args.days,
                    args.ce_len, args.ce_mult, args.z_len,
                    args.sl_atr, args.rr,
                    max_pos=args.max_pos, portfolio=not args.single))


if __name__ == "__main__":
    main()
