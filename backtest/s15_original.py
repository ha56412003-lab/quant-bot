"""
S15 · Chandelier Exit (everget) + ZLSMA —— 严格复刻版回测

本文件严格按作者原始规则实现，不做任何"优化"改动。与线上 s15_backtest.py
（优化模式）的区别在此列清，避免以后再混淆：

┌────────────────┬──────────────────────┬────────────────────────┐
│ 项             │ 本文件（作者原版）    │ s15_backtest.py（优化）│
├────────────────┼──────────────────────┼────────────────────────┤
│ K 线类型       │ Heikin Ashi          │ 原始 OHLC              │
│ CE ATR 周期    │ 1                    │ 22                     │
│ CE ATR 乘积    │ 2                    │ 3.0                    │
│ ZLSMA 长度     │ 50                   │ 50                     │
│ 主出场         │ 收盘穿越 ZLSMA       │ ATR×5 止损 + 1.5R 止盈 │
│ 止损           │ 关键K线低/高点        │ ATR×5                  │
│ 强制止盈       │ 无（靠均线）          │ 1.5R                   │
└────────────────┴──────────────────────┴────────────────────────┘

作者规则（原文）：
【做多】
  1. 移动止损指标发出买入信号
  2. 关键K线：收盘价位于均线之上的阳线
  3. 止损：关键K线低点下方，或者阶段性低点下方
  4. 止盈：收盘价走出下穿均线的K线
【做空】镜像

关键约束：出场以策略本身（均线穿越）为主，止盈止损与时限只作辅助。

用法：
    python -m backtest.s15_original --interval 5m --days 30
    python -m backtest.s15_original --interval 15m --days 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import binance  # noqa: E402

DATA = ROOT / "backtest" / "data"
DATA.mkdir(parents=True, exist_ok=True)

FEE = 0.0005      # taker 0.05%，双边各一次
SLIP = 0.0003


# ══════════════════════════════════════════════════════════════════
#  Heikin Ashi
# ══════════════════════════════════════════════════════════════════
def heikin_ashi(ks):
    """把原始 OHLC 转成 Heikin Ashi。

    这是策略的核心前提之一 —— 作者明确要求用 HA 图表。HA 会把
    单根的反转噪声抹平，趋势感更强；用原始 K 线跑出来的信号
    与作者原意完全不同。

        HA_close = (O + H + L + C) / 4
        HA_open  = (HA_open[1] + HA_close[1]) / 2     首根用 (O+C)/2
        HA_high  = max(H, HA_open, HA_close)
        HA_low   = min(L, HA_open, HA_close)
    """
    n = len(ks)
    ho = [0.0] * n
    hh = [0.0] * n
    hl = [0.0] * n
    hc = [0.0] * n
    for i, k in enumerate(ks):
        o, h, l, c = float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"])
        hc[i] = (o + h + l + c) / 4.0
        ho[i] = (o + c) / 2.0 if i == 0 else (ho[i - 1] + hc[i - 1]) / 2.0
        hh[i] = max(h, ho[i], hc[i])
        hl[i] = min(l, ho[i], hc[i])
    return ho, hh, hl, hc


# ══════════════════════════════════════════════════════════════════
#  指标（全部在 HA 序列上计算）
# ══════════════════════════════════════════════════════════════════
def _linreg(src, length, offset=0):
    """等价于 Pine 的 ta.linreg(src, length, offset)。"""
    n = len(src)
    out: list = [None] * n
    if length <= 0:
        return out
    x_mean = (length - 1) / 2.0
    denom = sum((x - x_mean) ** 2 for x in range(length))
    for i in range(length - 1, n):
        window = src[i - length + 1:i + 1]
        if any(v is None for v in window):
            continue
        y_mean = sum(window) / length
        num = sum((x - x_mean) * (window[x] - y_mean) for x in range(length))
        slope = num / denom if denom else 0.0
        intercept = y_mean - slope * x_mean
        idx = (length - 1) - offset
        out[i] = intercept + slope * idx
    return out


def _rma(src, length):
    """等价于 Pine 的 ta.rma。length=1 时等于 src 本身。"""
    n = len(src)
    out: list = [None] * n
    if length <= 0:
        return out
    acc = None
    cnt = 0
    for i in range(n):
        v = src[i]
        if v is None:
            continue
        if acc is None:
            cnt += 1
            if cnt >= length:
                acc = sum(x for x in src[:i + 1] if x is not None) / length
                out[i] = acc
        else:
            acc = (acc * (length - 1) + v) / length
            out[i] = acc
    return out


def _adx(ho, hh, hl, hc, length=14):
    """ADX —— 判断趋势强度（>25 趋势明确，<20 震荡）。

    在 HA 序列上算，与策略保持同一视角。ADX 只看趋势有多强，不看方向，
    所以它是"这个行情适不适合趋势策略"最直接的过滤条件。
    """
    n = len(hh)
    tr = [0.0] * n
    pdm = [0.0] * n
    ndm = [0.0] * n
    for i in range(1, n):
        up = hh[i] - hh[i - 1]
        dn = hl[i - 1] - hl[i]
        tr[i] = max(hh[i] - hl[i], abs(hh[i] - hc[i - 1]), abs(hl[i] - hc[i - 1]))
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0

    atr = _rma(tr, length)
    pdi_s = _rma(pdm, length)
    ndi_s = _rma(ndm, length)

    dx = [None] * n
    for i in range(n):
        if not atr[i] or atr[i] <= 0 or pdi_s[i] is None or ndi_s[i] is None:
            continue
        pdi = 100.0 * pdi_s[i] / atr[i]
        ndi = 100.0 * ndi_s[i] / atr[i]
        s = pdi + ndi
        if s > 0:
            dx[i] = 100.0 * abs(pdi - ndi) / s

    # ADX = RMA(dx)，跳过 None
    adx: list = [None] * n
    acc = None
    cnt = 0
    for i in range(n):
        v = dx[i]
        if v is None:
            continue
        if acc is None:
            cnt += 1
            if cnt >= length:
                acc = sum(x for x in dx[:i + 1] if x is not None) / length
                adx[i] = acc
        else:
            acc = (acc * (length - 1) + v) / length
            adx[i] = acc
    return adx


def _chop_score(dirs, i, lookback=50):
    """震荡度：过去 lookback 根内 CE 方向翻转了多少次。

    翻转频繁说明价格在吊灯止损两侧来回穿 —— 典型的横盘。这个指标比
    ADX 更贴近本策略本身：ADX 衡量的是普遍趋势强度，而 ceDir 翻转
    次数直接说明"这套信号在这段行情里会被打脸多少次"。
    """
    lo = max(1, i - lookback + 1)
    flips = 0
    for k in range(lo + 1, i + 1):
        if dirs[k] != dirs[k - 1]:
            flips += 1
    return flips


def build(ks, ce_len=1, ce_mult=2.0, z_len=50):
    """在 HA 序列上算 ZLSMA 与 Chandelier Exit。"""
    ho, hh, hl, hc = heikin_ashi(ks)
    n = len(hc)

    # ZLSMA = linreg + (linreg - linreg(linreg))，在 HA 收盘价上算
    l1 = _linreg(hc, z_len, 0)
    l2 = _linreg([v if v is not None else 0.0 for v in l1], z_len, 0)
    zlsma = [None if (a is None or b is None) else a + (a - b) for a, b in zip(l1, l2)]

    # ATR(ce_len) — 作者参数 ce_len=1，此时等于当根 TR
    tr: list = [None] * n
    tr[0] = hh[0] - hl[0]
    for i in range(1, n):
        tr[i] = max(hh[i] - hl[i], abs(hh[i] - hc[i - 1]), abs(hl[i] - hc[i - 1]))
    atr = _rma(tr, ce_len)

    # Chandelier Exit：极值窗口 + 棘轮 + 方向状态机（完全照 pine 原版）
    long_stop: list = [None] * n
    short_stop: list = [None] * n
    dirs = [1] * n
    for i in range(n):
        if atr[i] is None or i < ce_len:
            continue
        hh_w = max(hc[i - ce_len + 1:i + 1])
        ll_w = min(hc[i - ce_len + 1:i + 1])
        adj = ce_mult * atr[i]
        ls_raw, ss_raw = hh_w - adj, ll_w + adj

        ls_prev = long_stop[i - 1] if long_stop[i - 1] is not None else ls_raw
        ss_prev = short_stop[i - 1] if short_stop[i - 1] is not None else ss_raw
        # 棘轮：多头止损只上移，空头止损只下移
        long_stop[i] = max(ls_raw, ls_prev) if hc[i - 1] > ls_prev else ls_raw
        short_stop[i] = min(ss_raw, ss_prev) if hc[i - 1] < ss_prev else ss_raw

        if hc[i] > ss_prev:
            dirs[i] = 1
        elif hc[i] < ls_prev:
            dirs[i] = -1
        else:
            dirs[i] = dirs[i - 1]

    adx = _adx(ho, hh, hl, hc, 14)

    # CE 方向翻转的前缀和 —— 震荡度过滤要反复查，不能每次都重数
    flip_prefix = [0] * n
    for i in range(1, n):
        flip_prefix[i] = flip_prefix[i - 1] + (1 if dirs[i] != dirs[i - 1] else 0)

    return {"ho": ho, "hh": hh, "hl": hl, "hc": hc,
            "zlsma": zlsma, "atr": atr, "dir": dirs, "adx": adx,
            "flip_prefix": flip_prefix,
            "long_stop": long_stop, "short_stop": short_stop}


# ══════════════════════════════════════════════════════════════════
#  模拟
# ══════════════════════════════════════════════════════════════════
def _close(pos, px, why, i, ts):
    sign = 1 if pos["side"] == "long" else -1
    gross = (px - pos["entry"]) * sign
    # 入场可能是挂单（maker 0.02%），出场永远是市价（taker 0.05%）
    entry_rate = pos.get("entry_fee_rate", FEE)
    exit_rate = FEE
    fees = pos["entry"] * entry_rate + px * exit_rate
    # 滑点只算出场（入场挂单是限价，没有滑点）
    slip = px * SLIP * 0.5
    net = gross - fees - slip
    return {"side": pos["side"], "entry": pos["entry"], "exit": px, "why": why,
            "net": net, "r": net / pos["risk"], "r_gross": gross / pos["risk"],
            "fee_r": (fees + slip) / pos["risk"],
            "risk_pct": pos["risk"] / pos["entry"] * 100 if pos["entry"] else 0.0,
            "atr_pct": pos.get("atr_pct", 0.0),
            "bars": i - pos["entry_i"], "sym": pos.get("sym", ""), "ts": ts}


def simulate_one(sym, ks, ind, allow_long=True, allow_short=True,
                 use_keybar_stop=True, use_swing_stop=True, swing_lookback=20,
                 max_hold_bars=None, aux_atr_stop=None, aux_rr=None,
                 min_adx=0.0, max_flips=None, flip_lookback=50,
                 maker_entry=False):
    """严格按作者规则模拟单币。

    出场优先级（作者规则：策略本身为主，止损时限为辅）：
        ① 收盘穿越 ZLSMA  → 主出场（趋势结束）
        ② 辅助止损：关键K线低点 / 阶段性低点（可叠加）
        ③ 辅助强平：ATR 止损 / R 倍止盈 / 时限（默认关闭）

    swing_lookback: "阶段性低点"的窗口（默认 20 根）

    ── 震荡过滤（核心）──
    本策略是趋势跟踪，横盘时必然被反复打脸。两个过滤条件：
      min_adx    : ADX(14) 低于此值不进场（>25 趋势明确，<20 震荡）
      max_flips  : 过去 flip_lookback 根内 CE 方向翻转次数上限。
                   翻转次数直接等于"这套信号在这段行情里被打脸多少
                   次"，比 ADX 更贴近策略本身。
    """
    n = len(ks)
    zl, dirs = ind["zlsma"], ind["dir"]
    hh, hl, hc, ho = ind["hh"], ind["hl"], ind["hc"], ind["ho"]
    adx = ind.get("adx") or [None] * n

    trades = []
    skipped_chop = 0
    missed = 0
    i = max(1, 60)
    while i < n - 1:
        if zl[i] is None or dirs[i] is None:
            i += 1
            continue

        long_ok = allow_long and dirs[i] == 1 and hc[i] > zl[i] and hc[i] > ho[i]
        short_ok = allow_short and dirs[i] == -1 and hc[i] < zl[i] and hc[i] < ho[i]
        if not (long_ok or short_ok):
            i += 1
            continue

        # ── 震荡过滤 ──
        if min_adx > 0:
            a = adx[i]
            if a is None or a < min_adx:
                skipped_chop += 1
                i += 1
                continue
        if max_flips is not None:
            fp = ind.get("flip_prefix")
            lo = max(0, i - flip_lookback)
            flips = (fp[i] - fp[lo]) if fp else _chop_score(dirs, i, flip_lookback)
            if flips > max_flips:
                skipped_chop += 1
                i += 1
                continue

        side = "long" if long_ok else "short"

        # ── 入场方式 ──
        if maker_entry:
            # 限价挂在信号价（这根 K 线的收盘价），等下一根回来成交
            limit = float(ks[i]["close"])
            nxt = ks[i + 1]
            if side == "long":
                if float(nxt["open"]) <= limit:
                    entry = float(nxt["open"])      # 直接低开，成交更好
                elif float(nxt["low"]) <= limit:
                    entry = limit                   # 回踩到挂单价
                else:
                    missed += 1
                    i += 1
                    continue                        # 没回来，这笔不做
            else:
                if float(nxt["open"]) >= limit:
                    entry = float(nxt["open"])
                elif float(nxt["high"]) >= limit:
                    entry = limit
                else:
                    missed += 1
                    i += 1
                    continue
        else:
            entry = float(ks[i + 1]["open"])        # 市价：下一根开盘
        if entry <= 0:
            i += 1
            continue

        # ── 止损位：关键K线低点 / 阶段性低点，取更远的一个 ──
        stops = []
        if use_keybar_stop:
            stops.append(hl[i] if side == "long" else hh[i])
        if use_swing_stop:
            lo = max(0, i - swing_lookback + 1)
            stops.append(min(hl[lo:i + 1]) if side == "long" else max(hh[lo:i + 1]))
        if stops:
            stop = min(stops) if side == "long" else max(stops)
        else:
            a = ind["atr"][i] or 0.0
            stop = entry - a * 5 if side == "long" else entry + a * 5

        if side == "long" and stop >= entry:
            i += 1
            continue
        if side == "short" and stop <= entry:
            i += 1
            continue

        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue

        tp = None
        if aux_rr:
            tp = entry + risk * aux_rr if side == "long" else entry - risk * aux_rr
            if tp <= 0:
                tp = None

        pos = {"side": side, "entry": entry, "stop": stop, "tp": tp,
               "entry_i": i + 1, "risk": risk, "sym": sym,
               "entry_fee_rate": (0.0002 if maker_entry else FEE),
               "atr_pct": (ind["atr"][i] / hc[i] * 100) if (ind["atr"][i] and hc[i]) else 0.0}

        # ── 逐根推进，判出场 ──
        j = i + 1
        done = False
        while j < n:
            # ① 主出场：收盘穿越 ZLSMA（用前一根已收盘的均线值，防重绘）
            zp = zl[j - 1]
            if zp is not None:
                if side == "long" and hc[j] < zp:
                    trades.append(_close(pos, float(ks[j]["close"]), "均线止盈", j, ks[j]["ts"]))
                    done = True
                    break
                if side == "short" and hc[j] > zp:
                    trades.append(_close(pos, float(ks[j]["close"]), "均线止盈", j, ks[j]["ts"]))
                    done = True
                    break

            # ② 辅助止损：关键K线低/高点
            if side == "long" and float(ks[j]["low"]) <= stop:
                trades.append(_close(pos, stop, "关键K线止损", j, ks[j]["ts"]))
                done = True
                break
            if side == "short" and float(ks[j]["high"]) >= stop:
                trades.append(_close(pos, stop, "关键K线止损", j, ks[j]["ts"]))
                done = True
                break

            # ③ 辅助：R 倍止盈 / ATR 止损（默认关闭）
            if pos["tp"]:
                if side == "long" and float(ks[j]["high"]) >= pos["tp"]:
                    trades.append(_close(pos, pos["tp"], "R倍止盈", j, ks[j]["ts"]))
                    done = True
                    break
                if side == "short" and float(ks[j]["low"]) <= pos["tp"]:
                    trades.append(_close(pos, pos["tp"], "R倍止盈", j, ks[j]["ts"]))
                    done = True
                    break

            # ④ 辅助：时限
            if max_hold_bars and j - pos["entry_i"] >= max_hold_bars:
                trades.append(_close(pos, float(ks[j]["close"]), "超时", j, ks[j]["ts"]))
                done = True
                break

            j += 1

        if not done:
            # 数据末尾仍未平，按最后收盘结算
            trades.append(_close(pos, float(ks[-1]["close"]), "数据结束", n - 1, ks[-1]["ts"]))

        i = j + 1
    return trades, skipped_chop, missed


def stats(trades, label):
    if not trades:
        return {"label": label, "n": 0, "win": 0.0, "avg_r": 0.0, "total_r": 0.0,
                "gross_r": 0.0, "fee_r": 0.0, "pf": 0.0, "maxdd_r": 0.0,
                "why": {}, "long_win": 0.0, "short_win": 0.0}
    n = len(trades)
    rs = [t["r"] for t in trades]
    wins = [t for t in trades if t["r"] > 0]
    gw = sum(t["r"] for t in wins)
    gl = abs(sum(t["r"] for t in trades if t["r"] <= 0))
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    ls = [t for t in trades if t["side"] == "long"]
    ss = [t for t in trades if t["side"] == "short"]
    from collections import Counter
    return {
        "label": label, "n": n,
        "win": len(wins) / n * 100,
        "avg_r": sum(rs) / n,
        "total_r": sum(rs),
        "gross_r": sum(t["r_gross"] for t in trades),
        "fee_r": sum(t["fee_r"] for t in trades),
        "pf": (gw / gl) if gl > 0 else 99.9,
        "maxdd_r": mdd,
        "why": dict(Counter(t["why"] for t in trades)),
        "long_win": (sum(1 for t in ls if t["r"] > 0) / len(ls) * 100) if ls else 0.0,
        "short_win": (sum(1 for t in ss if t["r"] > 0) / len(ss) * 100) if ss else 0.0,
    }


# ══════════════════════════════════════════════════════════════════
#  取数 & 筛标的
# ══════════════════════════════════════════════════════════════════
def _cache_path(sym, iv, days):
    return DATA / f"orig_{sym}_{iv}_{days}.json"


async def fetch_klines(sym, interval, days, nocache=False):
    """拉历史 K 线并缓存。

    不用 backend.binance.klines 是因为它不支持 endTime，没法往前翻页
    取更早的历史。这里直接走同一个 _get 通道，复用连接池。
    """
    p = _cache_path(sym, interval, days)
    if p.exists() and not nocache:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    need = days * {"5m": 288, "15m": 96, "1h": 24}.get(interval, 96)
    raw = []
    end = int(time.time() * 1000)
    while len(raw) < need:
        batch = await binance._get(  # noqa: SLF001
            binance.FUT, "/fapi/v1/klines",
            {"symbol": sym, "interval": interval,
             "limit": min(1500, need - len(raw)), "endTime": end})
        if not batch:
            break
        raw = list(batch) + raw
        end = int(batch[0][0]) - 1
        if len(batch) < 1500:
            break
    ks = []
    for b in raw:
        try:
            ks.append({"ts": int(b[0]) // 1000,
                       "open": float(b[1]), "high": float(b[2]),
                       "low": float(b[3]), "close": float(b[4]),
                       "vol": float(b[5])})
        except (TypeError, ValueError, IndexError):
            continue
    ks.sort(key=lambda x: x["ts"])
    # 去重（翻页边界可能重复）
    seen = set()
    uniq = []
    for k in ks:
        if k["ts"] in seen:
            continue
        seen.add(k["ts"])
        uniq.append(k)
    p.write_text(json.dumps(uniq), encoding="utf-8")
    return uniq


async def pick_symbols(min_quote_vol=10_000_000, min_atr_pct=1.5,
                       interval="15m", top_n=3, scan_n=25):
    """筛标的：24h 成交额 > 1000 万 且 ATR% > 1.5。

    必须排除股票/ETF 代币（SOXL、KORU、SOXS 这类）。它们有交易时段限制、
    开盘跳空，还有杠杆损耗，与加密永续的日内特性完全不同 —— 混进来会
    把回测结果带偏。用 exchangeInfo 的 underlyingType 判断，只留 COIN。
    """
    # 先排除非加密标的
    coin_ok = set()
    try:
        info = await binance.exchange_info()
        for c in (info.get("symbols") or []):
            if c.get("status") != "TRADING":
                continue
            if c.get("contractType") not in ("PERPETUAL", "CURRENT_QUARTER", "NEXT_QUARTER"):
                continue
            ut = c.get("underlyingType")
            if ut and ut != "COIN":
                continue          # 股票 / ETF / 指数类
            coin_ok.add(c.get("symbol"))
        print(f"  交易所加密永续合约: {len(coin_ok)} 个")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ exchangeInfo 取不到（{exc}），改用后缀黑名单")
        coin_ok = None

    tk = await binance.ticker_24h_all()
    rows = []
    for t in tk:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if coin_ok is not None and s not in coin_ok:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if qv < min_quote_vol:
            continue
        rows.append((qv, s))
    rows.sort(reverse=True)
    print(f"  24h 成交额 > {min_quote_vol/1e6:.1f}M 的加密合约: {len(rows)} 个")
    print(f"  按成交额取前 {scan_n} 个算 ATR% ...")

    picked = []
    scanned = 0
    for qv, s in rows[:scan_n]:
        try:
            ks = await fetch_klines(s, interval, 3)
        except Exception:  # noqa: BLE001
            continue
        if len(ks) < 100:
            continue
        scanned += 1
        ind = build(ks, 1, 2.0, 50)
        atrs = [a for a in ind["atr"][-60:] if a]
        if not atrs:
            continue
        avg_atr = sum(atrs) / len(atrs)
        px = ks[-1]["close"]
        atr_pct = avg_atr / px * 100 if px else 0
        flag = "✓" if atr_pct >= min_atr_pct else " "
        print(f"    {flag} {s:16s} 成交额 {qv/1e6:8.1f}M  ATR {atr_pct:5.2f}%")
        if atr_pct >= min_atr_pct:
            picked.append({"symbol": s, "quote_vol": qv, "atr_pct": atr_pct})
        if len(picked) >= top_n:
            break
    if not picked:
        print(f"  ✗ 前 {scanned} 个里没有 ATR% ≥ {min_atr_pct} 的，需要放宽门槛")
    return picked


# ══════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════
async def run(interval, days, ce_len, ce_mult, z_len, symbols=None,
              min_vol=10_000_000, min_atr=1.5, top_n=3, scan_n=25,
              min_adx=0.0, max_flips=None, flip_lookback=50, maker_entry=False):
    print(f"\n{'='*74}")
    print(f"S15 作者原版回测  |  {interval}  |  {days} 天")
    print(f"参数: HA图表 + CE(ATR{ce_len}×{ce_mult}) + ZLSMA({z_len})")
    print(f"筛标的: 成交额>{min_vol/1e6:.0f}M  ATR%>{min_atr}")
    filt = []
    if min_adx > 0:
        filt.append(f"ADX≥{min_adx}")
    if max_flips is not None:
        filt.append(f"{flip_lookback}根内CE翻转≤{max_flips}")
    print(f"震荡过滤: {' + '.join(filt) if filt else '无（全部信号都做）'}")
    print(f"入场方式: {'限价挂单 maker 0.02%（等回踩）' if maker_entry else '市价 taker 0.05%'}")
    print(f"{'='*74}\n")

    if symbols:
        picked = [{"symbol": s, "quote_vol": 0, "atr_pct": 0} for s in symbols]
    else:
        print("【筛标的】")
        picked = await pick_symbols(min_vol, min_atr, interval, top_n, scan_n)
        if not picked:
            print("  ✗ 没有符合条件的标的，请放宽门槛")
            return []

    print(f"\n【回测】{len(picked)} 个标的")
    rows = []
    all_tr = []
    for p in picked:
        sym = p["symbol"]
        ks = await fetch_klines(sym, interval, days)
        if len(ks) < 200:
            print(f"  {sym:16s} 数据不足 {len(ks)} 根，跳过")
            continue
        ind = build(ks, ce_len, ce_mult, z_len)
        tr, skipped, missed = simulate_one(sym, ks, ind, min_adx=min_adx,
                                           max_flips=max_flips,
                                           flip_lookback=flip_lookback,
                                           maker_entry=maker_entry)
        all_tr.extend(tr)
        r = stats(tr, sym)
        rows.append(r)
        miss_pct = missed / (len(tr) + missed) * 100 if (len(tr) + missed) else 0
        print(f"  {sym:16s} {len(ks):5d}根  {r['n']:3d}笔  胜{r['win']:5.1f}%  "
              f"净{r['total_r']:8.2f}R  漏单{miss_pct:4.1f}%  {r['why']}")

    merged = stats(all_tr, "合计")
    print(f"\n{'='*74}")
    print(f"{'标的':<17}{'笔数':>6}{'胜率':>8}{'净总R':>10}{'均R':>9}"
          f"{'盈亏比':>8}{'回撤R':>9}")
    print("-" * 68)
    for r in rows + [merged]:
        print(f"{r['label']:<17}{r['n']:>6}{r['win']:>7.1f}%{r['total_r']:>10.2f}"
              f"{r['avg_r']:>9.3f}{r['pf']:>8.2f}{r['maxdd_r']:>9.2f}")
    print()
    print(f"  合计出场原因: {merged['why']}")
    print(f"  多头胜率 {merged['long_win']:.1f}%   空头胜率 {merged['short_win']:.1f}%")
    print(f"  手续费吃掉 {merged['fee_r']:.1f}R（毛 {merged['gross_r']:.1f}R）")

    out = ROOT / "backtest" / f"orig_{interval}_{int(time.time())}.json"
    out.write_text(json.dumps({"params": {"interval": interval, "days": days,
                                          "ce_len": ce_len, "ce_mult": ce_mult,
                                          "z_len": z_len,
                                          "picked": picked},
                               "summary": rows + [merged]},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已存: {out}")
    return rows + [merged]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="15m", choices=["5m", "15m", "1h"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--ce-len", type=int, default=1)
    ap.add_argument("--ce-mult", type=float, default=2.0)
    ap.add_argument("--z-len", type=int, default=50)
    ap.add_argument("--symbols", default="", help="逗号分隔，跳过自动筛选")
    ap.add_argument("--min-vol", type=float, default=10_000_000)
    ap.add_argument("--min-atr", type=float, default=1.5)
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--scan-n", type=int, default=25)
    ap.add_argument("--min-adx", type=float, default=0.0, help="ADX 下限，0=不过滤")
    ap.add_argument("--max-flips", type=int, default=None, help="CE 翻转次数上限")
    ap.add_argument("--flip-lookback", type=int, default=50)
    ap.add_argument("--maker-entry", action="store_true",
                    help="限价挂单入场（maker 0.02%），等回踩才成交")
    a = ap.parse_args()
    syms = [s.strip() for s in a.symbols.split(",") if s.strip()] or None
    asyncio.run(run(a.interval, a.days, a.ce_len, a.ce_mult, a.z_len,
                    syms, a.min_vol, a.min_atr, a.top_n, a.scan_n,
                    a.min_adx, a.max_flips, a.flip_lookback, a.maker_entry))


if __name__ == "__main__":
    main()
