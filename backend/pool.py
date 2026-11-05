"""每日候选池（三层池的第 1 层）—— 每天凌晨从全市场筛 100 只。

组成：
  1. 白名单（配置里的自选合约）—— 优先级最高，无条件占位
  2. 规则筛入 —— 按 24h 成交额降序扫描，过阀门后补满剩余名额

规则筛入的四道门：
  ① underlyingType = COIN      真加密货币（排除 TSLA/NVDA 等股票代币）
  ② quoteAsset = USDT          USDT 本位（排除 BTCUSDC 等 USDC 本位）
  ③ contractType = PERPETUAL  永续（排除季度交割）
  ④ status = TRADING          在交易（排除已下架）
  ⑤ 30 天振幅 ∈ [10%, 300%]   价格波动阀门（见下方说明）

振幅阀门的取值依据（2026-11-05 实测 200 个币的 30 天日线）：
  中位振幅 62%，P10=28.9%，P90=357%
  下限 10%：剔除 PAXG(8.3%)/XAUT(8.7%)/USDC(0.3%) 这类非加密资产和稳定币，
            同时保住 BTC(13.0%)、BNB(15.1%) —— 这两个是前 100 名里振幅最低的真币
  上限 300%：剔除 AKE(1745%,30天-90.6%)、Q(1037%,日波动54%)、AIN(1061%,+669%)、
            JCT(599%,+355%) 这类妖币/崩盘币，它们的止损会被一根 K 线扫掉
  实测 [10%,300%] 在前 100 名内通过 88 个、前 200 名内通过 183 个，凑满 100 无压力。

流量：exchangeInfo 1145KB + ticker 291KB + 约 250 个日线请求 × 4KB ≈ 2.5 MB/天
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta

from . import binance

log = logging.getLogger(__name__)

# ── 参数（可在 strategies.yaml 的 pools.candidate 段覆盖）──
CANDIDATE_SIZE = 100        # 候选池目标规模
MIN_AMP_PCT = 10.0          # 30 天振幅下限（%）
MAX_AMP_PCT = 300.0         # 30 天振幅上限（%）
VOL_DAYS = 31               # 取 31 根日线算 30 天波动
REFRESH_HOUR = 3            # 每天凌晨 3 点重建（流动性最低，成交额数据已完整）
MAX_SCAN = 400              # 最多扫描多少个币（防止极端情况一直扫）
CONCURRENCY = 8             # 日线请求并发数

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_PATH = os.path.join(_DIR, "candidates.json")

_state: dict = {"symbols": [], "built_ts": 0.0, "meta": {}}


def _amp_pct(bars) -> float | None:
    """30 天振幅 = (最高 - 最低) / 最低 × 100"""
    if len(bars) < 20:
        return None
    hi = max(b["high"] for b in bars)
    lo = min(b["low"] for b in bars)
    if lo <= 0:
        return None
    return (hi - lo) / lo * 100.0


async def _passes_amp(sym: str, sem: asyncio.Semaphore):
    """拉日线判振幅，返回 (sym, amp, 是否通过, 原因)"""
    async with sem:
        try:
            bars = await binance.klines(sym, "1d", VOL_DAYS)
        except Exception:  # noqa: BLE001
            return sym, None, False, "日线失败"
        finally:
            await asyncio.sleep(0.05)
    amp = _amp_pct(bars)
    if amp is None:
        return sym, None, False, "数据不足"
    if amp < MIN_AMP_PCT:
        return sym, amp, False, f"太死({amp:.1f}%)"
    if amp > MAX_AMP_PCT:
        return sym, amp, False, f"太疯({amp:.1f}%)"
    return sym, amp, True, ""


async def build(whitelist: list[str] | None = None) -> dict:
    """重建候选池并落盘。返回 {symbols, meta}。"""
    global _state
    whitelist = [s.strip().upper() for s in (whitelist or []) if s.strip()]
    t0 = time.time()

    info = await binance.exchange_info()
    syms_info = info.get("symbols") or []
    tick = await binance.ticker_24h_all()
    qv = {d["symbol"]: float(d.get("quoteVolume") or 0) for d in tick}

    # 三道基础门：真币 + USDT 本位 + 永续 + 在交易
    tradable: list[str] = []
    for s in syms_info:
        if (s.get("underlyingType") == "COIN"
                and s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"):
            tradable.append(s["symbol"])

    log.info("候选池: 全市场真币 USDT 永续 %d 个", len(tradable))

    # 白名单优先，无条件占位（但也必须在 tradable 里，否则下不了单）
    tradable_set = set(tradable)
    picked: list[str] = []
    wl_used: list[str] = []
    wl_bad: list[str] = []
    for s in whitelist:
        if s in tradable_set and s not in picked:
            picked.append(s)
            wl_used.append(s)
        elif s not in tradable_set:
            wl_bad.append(s)
    if wl_bad:
        log.warning("候选池: 白名单里这些币在币安不可交易，已跳过: %s", wl_bad)

    # 规则筛入：按成交额降序扫描
    ranked = sorted(tradable, key=lambda s: -qv.get(s, 0))
    need = max(0, CANDIDATE_SIZE - len(picked))
    sem = asyncio.Semaphore(CONCURRENCY)
    scanned = 0
    rejected: list[tuple[str, str]] = []
    amps: dict[str, float] = {}

    idx = 0
    while need > 0 and idx < min(len(ranked), MAX_SCAN):
        batch = [s for s in ranked[idx: idx + CONCURRENCY * 2] if s not in picked]
        idx += CONCURRENCY * 2
        if not batch:
            continue
        results = await asyncio.gather(*[_passes_amp(s, sem) for s in batch])
        for sym, amp, ok, why in results:
            scanned += 1
            if ok and amp is not None:
                if sym not in picked:
                    picked.append(sym)
                    amps[sym] = amp
                    need -= 1
                    if need <= 0:
                        break
            elif not ok:
                rejected.append((sym, why))

    # 把已通过但没轮上的也记下振幅（用于展示）
    el = time.time() - t0
    meta = {
        "built_ts": time.time(),
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(el, 1),
        "total_tradable": len(tradable),
        "scanned": scanned,
        "whitelist_used": wl_used,
        "whitelist_missing": wl_bad,
        "rejected_n": len(rejected),
        "rejected_sample": rejected[:30],
        "size": len(picked),
        "min_amp_pct": MIN_AMP_PCT,
        "max_amp_pct": MAX_AMP_PCT,
        "vol_days": VOL_DAYS - 1,
        "amps": amps,
    }

    os.makedirs(_DIR, exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"symbols": picked, "meta": meta}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _PATH)

    _state = {"symbols": list(picked), "built_ts": meta["built_ts"], "meta": meta}
    log.info("候选池重建完成: %d 只（白名单 %d，扫描 %d，耗时 %.0fs）",
             len(picked), len(wl_used), scanned, el)
    return _state


def load() -> list[str]:
    """从磁盘读候选池（启动时调用）。文件不存在返回空。"""
    global _state
    try:
        with open(_PATH, encoding="utf-8") as f:
            d = json.load(f)
        _state = {"symbols": d.get("symbols") or [],
                  "built_ts": (d.get("meta") or {}).get("built_ts", 0.0),
                  "meta": d.get("meta") or {}}
        log.info("候选池: 从磁盘加载 %d 只（构建于 %s）",
                 len(_state["symbols"]), _state["meta"].get("built_at", "?"))
    except FileNotFoundError:
        log.warning("候选池: %s 不存在，将使用配置里的 universe 兜底", _PATH)
        _state = {"symbols": [], "built_ts": 0.0, "meta": {}}
    except Exception as exc:  # noqa: BLE001
        log.warning("候选池加载失败: %s", exc)
        _state = {"symbols": [], "built_ts": 0.0, "meta": {}}
    return _state["symbols"]


def symbols() -> list[str]:
    return list(_state["symbols"])


def snapshot() -> dict:
    return {"symbols": list(_state["symbols"]),
            "built_ts": _state["built_ts"],
            "meta": _state["meta"],
            "size": CANDIDATE_SIZE,
            "refresh_hour": REFRESH_HOUR}


def _secs_until_next_run(hour: int) -> float:
    """距离下一个 hour:00 还有多少秒。"""
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(60.0, (target - now).total_seconds())


async def loop(whitelist_fn, hour: int = REFRESH_HOUR):
    """每天凌晨定时重建。启动时若存量超过 20 小时也会重建。"""
    from . import config as _config

    async def _rebuild():
        st = await build(whitelist_fn())
        # 重建后立刻让 config.UNIVERSE 反映新名单，不等引擎下一 tick
        try:
            _config.load_strategies()
        except Exception:  # noqa: BLE001
            pass
        return st

    await asyncio.sleep(5)  # 等引擎就绪
    if time.time() - _state["built_ts"] > 20 * 3600:
        try:
            await _rebuild()
        except Exception as exc:  # noqa: BLE001
            log.warning("候选池首次重建失败: %s", exc)
    while True:
        wait = _secs_until_next_run(hour)
        log.info("候选池: 下次重建在 %.1f 小时后", wait / 3600)
        await asyncio.sleep(wait)
        try:
            await _rebuild()
        except Exception as exc:  # noqa: BLE001
            log.warning("候选池定时重建失败: %s", exc)
