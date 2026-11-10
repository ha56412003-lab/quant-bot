# -*- coding: utf-8 -*-
"""关键K线（大阳线 / 大阴线）定义 · 生效性验证

验证四件事：
  ① 定义在 `backend/strategies.py::_is_key_bar`，三条 AND
  ② 全部用**原始 K 线**（open/high/low/close），不是 HA K 线
     —— 用同一条数据分别喂原始 K 线和 HA K 线，判定结果必然不同，差异即证据
  ③ 拿一个币一天的真实数据，打印通过 / 不通过的逐根明细
  ④ 通过率与文档记载一致（mult=0.7 → 18.83%，三条全满足 9.91%）

用法：
  .venv/Scripts/python.exe scripts/verify_keybar.py [SYM] [DATE]
  例：verify_keybar.py BTCUSDT 2026-11-09
"""
import asyncio
import importlib.util
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.strategies import _is_key_bar  # noqa: E402

CST = timezone(timedelta(hours=8))
DATA = os.path.join(ROOT, "backtest", "data80coin")


def load(sym):
    fp = os.path.join(DATA, f"{sym}_15m_60d.json")
    if not os.path.exists(fp):
        raise SystemExit(f"没有数据文件：{fp}")
    with io.open(fp, encoding="utf-8") as f:
        raw = json.load(f)
    ks = raw if isinstance(raw, list) else raw.get("ks") or raw.get("data")
    out = []
    for k in ks:
        out.append({
            "ts": int(k.get("ts") or k.get("time") or 0),
            "open": float(k["open"]), "high": float(k["high"]),
            "low": float(k["low"]), "close": float(k["close"]),
        })
    return out


def to_ha(ks):
    """把原始 K 线转成 Heikin-Ashi（用来证明判定不吃 HA）"""
    ha, po, pc = [], None, None
    for k in ks:
        c = (k["open"] + k["high"] + k["low"] + k["close"]) / 4.0
        o = (po + pc) / 2.0 if po is not None else (k["open"] + k["close"]) / 2.0
        ha.append({"ts": k["ts"], "open": o, "high": max(k["high"], o, c),
                   "low": min(k["low"], o, c), "close": c})
        po, pc = o, c
    return ha


def atr20(ks, n=20):
    a = [None] * len(ks)
    trs = []
    for i, k in enumerate(ks):
        if i == 0:
            trs.append(k["high"] - k["low"])
        else:
            pc = ks[i - 1]["close"]
            trs.append(max(k["high"] - k["low"], abs(k["high"] - pc), abs(pc - k["low"])))
        a[i] = sum(trs[-n:]) / len(trs[-n:]) if len(trs) >= 2 else None
    return a


def fmt(t):
    return datetime.fromtimestamp(t, CST).strftime("%Y-%m-%d %H:%M")


def main():
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    day = sys.argv[2] if len(sys.argv) > 2 else None
    ks = load(sym)
    ha = to_ha(ks)
    atr_o, atr_h = atr20(ks), atr20(ha)

    print("=" * 82)
    print(f"关键K线定义验证 · {sym} · 共 {len(ks)} 根 15m")
    print("=" * 82)
    print(f"  判定函数：backend/strategies.py:343  def _is_key_bar(ks, i, atr_n, want, mult, closepos, bodyfrac)")
    print(f"  三条 AND：① 实体 > mult×ATR(20)  ② (收−低)/(高−低) > closepos  ③ 实体/(高−低) > bodyfrac")
    print(f"  参数取值：mult=0.7  closepos=0.66  bodyfrac=0.5")

    # ── 全样本通过率（对文档记载）
    print("\n" + "-" * 82)
    print("① 全样本通过率（逐根判定，mult=0.7）")
    print("-" * 82)
    n = 0
    c1 = c2 = c3 = 0
    bodies = []
    for i in range(25, len(ks)):
        a = atr_o[i]
        if not a or a <= 0:
            continue
        n += 1
        o, h, l, c = ks[i]["open"], ks[i]["high"], ks[i]["low"], ks[i]["close"]
        rng = h - l
        body = abs(c - o)
        if rng <= 0:
            continue
        f1 = body > 0.7 * a
        f2 = ((c - l) / rng > 0.66) if c >= o else ((h - c) / rng > 0.66)
        f3 = body / rng > 0.5
        c1 += f1
        c2 += f2
        c3 += f3
        if f1 and f2 and f3:
            bodies.append(body / rng)
    print(f"  样本 {n} 根：① 力度 {c1/n*100:.2f}% ｜ ② 掌控力 {c2/n*100:.2f}% ｜ ③ 决断力 {c3/n*100:.2f}%")
    print(f"  三条全满足 {len(bodies)/n*100:.2f}%   （文档记载 0.7× → 18.83%，三条全满足 9.91%）")

    # ── 原始 K 线 vs HA K 线
    print("\n" + "-" * 82)
    print("② 原始 K 线 vs HA K 线（证明判定吃的是原始 K 线）")
    print("-" * 82)
    raw_hits = ha_hits = both = 0
    for i in range(25, len(ks)):
        a = atr_o[i]
        if not a or a <= 0:
            continue
        r1 = _is_key_bar(ks, i, atr_o, "long", 0.7, 0.66, 0.5) or \
            _is_key_bar(ks, i, atr_o, "short", 0.7, 0.66, 0.5)
        r2 = _is_key_bar(ha, i, atr_h, "long", 0.7, 0.66, 0.5) or \
            _is_key_bar(ha, i, atr_h, "short", 0.7, 0.66, 0.5)
        raw_hits += r1
        ha_hits += r2
        both += (r1 and r2)
    print(f"  喂原始 K 线：{raw_hits} 根通过")
    print(f"  喂 HA  K 线：{ha_hits} 根通过")
    print(f"  两者同时通过：{both} 根 → 只有 {both/max(raw_hits,1)*100:.1f}% 重合")
    print(f"  → 若真的吃 HA，代码里传的就是 ha 数组；实际调用处传的是 ks，结论见 ③ 的逐根数值")

    # ── 一天样例
    if day:
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=CST)
    else:
        # 默认取最近一个完整的中国自然日
        last = datetime.fromtimestamp(ks[-1]["ts"], CST)
        d0 = (last.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
    d1 = d0 + timedelta(days=1)
    idxs = [i for i, k in enumerate(ks) if d0.timestamp() <= k["ts"] < d1.timestamp()]
    print("\n" + "-" * 82)
    print(f"③ 逐根明细 · {sym} · {d0.strftime('%Y-%m-%d')}（中国时间，共 {len(idxs)} 根）")
    print("-" * 82)
    print(f"  {'时间':<12}{'方向':<5}{'开':>10}{'高':>10}{'低':>10}{'收':>10}"
          f"{'实体/ATR':>10}{'收盘位置':>10}{'实体占比':>9}  判定")
    hits_long = hits_short = 0
    for i in idxs:
        a = atr_o[i]
        if not a or a <= 0:
            continue
        o, h, l, c = ks[i]["open"], ks[i]["high"], ks[i]["low"], ks[i]["close"]
        rng = h - l
        if rng <= 0:
            continue
        body = abs(c - o)
        bull = c >= o
        pos = ((c - l) / rng) if bull else ((h - c) / rng)
        kl = _is_key_bar(ks, i, atr_o, "long", 0.7, 0.66, 0.5)
        ks_ = _is_key_bar(ks, i, atr_o, "short", 0.7, 0.66, 0.5)
        hits_long += kl
        hits_short += ks_
        tag = "★ 大阳线" if kl else ("★ 大阴线" if ks_ else "")
        if tag or body / a > 0.5:   # 只打关键K线和接近的，避免刷屏
            print(f"  {fmt(ks[i]['ts']):<13}{('多' if bull else '空'):<6}"
                  f"{o:>10.2f}{h:>10.2f}{l:>10.2f}{c:>10.2f}"
                  f"{body/a:>10.3f}{pos:>10.3f}{body/rng:>9.3f}  {tag}")
    print(f"\n  当日：大阳线 {hits_long} 根，大阴线 {hits_short} 根（阈值 实体>0.7×ATR20 / 收盘位置>0.66 / 实体占比>0.5）")

    # ── 调用处证据
    print("\n" + "-" * 82)
    print("④ 调用处传的到底是什么（原始 K 线 vs HA）")
    print("-" * 82)
    src = io.open(os.path.join(ROOT, "backend", "strategies.py"), encoding="utf-8").read().splitlines()
    for ln in (626, 668, 956):
        print(f"  strategies.py:{ln}  {src[ln-1].strip()}")
    print("\n  → 第一参数都是 ks（原始 K 线数组）；HA 数组在代码里叫 ha / ha_full，从不传给 _is_key_bar")
    print("=" * 82)


if __name__ == "__main__":
    main()
