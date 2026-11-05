"""监控池（三层池的第 2 层）—— 从候选池 100 只里筛出可交易的 30 只。

三道门（波动小不是缺点，反而让止损更窄、仓位自动放大）：
  1. 24h 成交额   < 1000 万 U  → 流动性差，滑点吃人
  2. 插针比       > 4.0        → 单根 K 线振幅超过 4 倍 ATR，止损容易被扫
  3. ATR%         < 0.6%       → 死水币，止损窄到没有意义

刷新：每 1 小时（与 1h K 线粒度对齐，不重复计算）
排序：三道门全过的币，按 24h 成交额降序取前 MONITOR_SIZE 个进监控池
退出：持仓中的币不受监控池进出影响（池子只管能不能开新仓）
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import binance

log = logging.getLogger(__name__)

MIN_QUOTE_VOL = 1e7   # 24h 成交额下限：1000 万 U
MAX_SPIKE = 4.0       # 单根最大振幅 / ATR 上限（用 1h K线算，见下方说明）
# 2026-11-05 由 3.5 放宽到 4.0：插针比 = 最大单根振幅 / ATR，对低波动大市值币系统性偏高
#   （BTC ATR 仅 0.64%，分母小 → 比值天然高，实测 3.70 被误判；ETH 3.48 擦边）。
#   放宽到 4.0 后 BTC 放行，TRX(8.53) 等真正易插针的币仍被拦。
MIN_ATR_PCT = 0.6     # ATR 占价格比例下限：低于此值说明是"死水"币，止损过窄（1h 口径）
REFRESH_SEC = 3600    # 1 小时刷新（2026-11-05 由 5 分钟改，数据源是 1h K 线，对齐粒度）
MONITOR_SIZE = 30     # 监控池规模：三道门全过的币按成交额降序取前 30
FETCH_CONCURRENCY = 8 # 并发拉 K 线的协程数（串行 100 币要 80 秒，并发约 10 秒）

# 2026-11-05 算法变更（原用 15m K线）：
#   15m K线噪音太大 —— 96 根里每根都可能是十字星，29 个币的 max振幅/ATR 全落在 4~8.4，
#   导致 28/30 个币被判"易插针"，监控池实际只剩 INJUSDT 一个可用。
#   改用 1h K线（96 根 = 4 天窗口）后，噪音被平滑，插针值分布 2.08~8.33，区分度正常。
#   MIN_ATR_PCT 因口径从 15m 换到 1h（ATR 约大 2 倍），同步由 0.3 调为 0.6。

_cache: dict[str, dict] = {}
_monitor: list[str] = []   # 通过三道门且成交额前 MONITOR_SIZE 的币
_ts: float = 0.0


def configure(cfg: dict | None = None) -> None:
    """从 strategies.yaml 的 pools.monitor 段热更新阈值（缺项保留原值）。"""
    global MIN_QUOTE_VOL, MAX_SPIKE, MIN_ATR_PCT, REFRESH_SEC, MONITOR_SIZE
    cfg = cfg or {}
    if "min_quote_vol" in cfg:
        MIN_QUOTE_VOL = float(cfg["min_quote_vol"])
    if "max_spike" in cfg:
        MAX_SPIKE = float(cfg["max_spike"])
    if "min_atr_pct" in cfg:
        MIN_ATR_PCT = float(cfg["min_atr_pct"])
    if "refresh_sec" in cfg:
        REFRESH_SEC = int(cfg["refresh_sec"])
    if "size" in cfg:
        MONITOR_SIZE = int(cfg["size"])


def _atr(bars, n: int = 22):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n if len(trs) >= n else None


async def _fetch(sym: str, retries: int = 2):
    for attempt in range(retries):
        try:
            bars = await binance.klines(sym, "1h", 100)
            if len(bars) < 96:
                return None
            px = bars[-1]["close"]
            a = _atr(bars)
            if not a or not px:
                return None
            # 24 小时成交额 = 最近 24 根 1h K线
            qv = sum(x["volume"] * x["close"] for x in bars[-24:])
            # 插针 = 4 天内单根最大振幅 ÷ ATR（96 根 1h = 4 天）
            spike = max(x["high"] - x["low"] for x in bars[-96:]) / a
            atr_pct = a / px * 100.0
            why = []
            if qv < MIN_QUOTE_VOL:
                why.append("量小")
            if spike > MAX_SPIKE:
                why.append("易插针")
            if atr_pct < MIN_ATR_PCT:
                why.append("死水")
            return {"qv": qv, "atr_pct": atr_pct, "spike": spike,
                    "ok": not why, "why": ",".join(why), "ts": time.time()}
        except Exception as exc:  # noqa: BLE001
            if attempt + 1 < retries:
                await asyncio.sleep(1.0)
                continue
            log.debug("quality fetch %s failed: %s", sym, exc)
            return None
    return None


async def refresh(universe) -> int:
    """重新计算整个监控池的质地。返回成功条数。

    结果采用「先算后换」的原子替换：整个采集过程（100 币并发 8 路 ≈ 10~15 秒）结束后
    才一次性替换 _cache 和 _monitor。若边采边写，刷新窗口期内 _monitor 可能为空，
    is_tradable() 里 `if _monitor and ...` 会走不到席位判断，或者更糟 —— 读到半成品名单。
    """
    global _ts, _cache, _monitor
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _one(sym):
        async with sem:
            r = await _fetch(sym)
            await asyncio.sleep(0.05)   # 轻微错峰，避免瞬时打满权重
            return sym, r

    results = await asyncio.gather(*[_one(s) for s in universe])
    out: dict[str, dict] = {sym: r for sym, r in results if r}
    if not out:
        log.warning("quality refresh: 一个币都没采到，保留上一轮结果")
        return 0
    # 三道门全过的按成交额降序，取前 MONITOR_SIZE 进监控池
    passed = sorted(((sym, v["qv"]) for sym, v in out.items() if v["ok"]),
                    key=lambda x: -x[1])
    monitor = [s for s, _ in passed[:MONITOR_SIZE]]
    _cache, _monitor, _ts = out, monitor, time.time()   # 原子替换
    log.info("quality refreshed: %d symbols, pass %d, monitor %d",
             len(out), len(passed), len(monitor))
    return len(out)


def get(symbol: str):
    """取单币质量。未采集到时返回 None（此时放行，不因数据缺失而停单）。"""
    return _cache.get(symbol)


def monitor_list() -> list[str]:
    """当前监控池（30 个）。策略扫描时应以它为准，而不是全量候选池 ——
    扫了也白扫：执行层的 is_tradable() 会把非监控池的信号全拦掉。"""
    return list(_monitor)


def is_tradable(symbol: str):
    """(是否放行, 原因)。数据缺失时放行，避免因刷新失败误停整个系统。"""
    q = _cache.get(symbol)
    if not q:
        return True, ""
    if not q["ok"]:
        return False, q["why"]
    # 过了三道门还要抢到监控池席位（成交额前 30）
    if _monitor and symbol not in _monitor:
        return False, "未进监控池"
    return True, ""


def snapshot() -> dict:
    return {"ts": _ts, "min_quote_vol": MIN_QUOTE_VOL,
            "max_spike": MAX_SPIKE, "min_atr_pct": MIN_ATR_PCT,
            "refresh_sec": REFRESH_SEC, "data": _cache,
            "monitor": list(_monitor), "monitor_size": MONITOR_SIZE,
            "monitor_n": len(_monitor)}


async def loop(universe_fn, cfg_fn=None):
    """后台循环，随引擎一起启动。

    universe_fn: () -> 候选池币种列表（约 100 只）
    cfg_fn:      () -> pools.monitor 配置字典（可空）
    """
    while True:
        try:
            if cfg_fn is not None:
                configure(cfg_fn())
            await refresh(universe_fn())
        except Exception as exc:  # noqa: BLE001
            log.warning("quality loop failed: %s", exc)
        await asyncio.sleep(REFRESH_SEC)
