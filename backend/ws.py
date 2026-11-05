"""持仓币实时价 —— REST 轮询。

⚠ 2026-11-05 重要变更：原来用 WebSocket 订阅 `<symbol>@bookTicker`（逐笔最优价），
   候选池扩到 100 只后实测流量 **21.7 GB/天**（100 币 × 约 1300 条/秒）。
   这跟"一晚上跑几十个 G 流量"的事故完全对得上。
   而且 S11 高频策略（唯一需要 tick 级数据的策略）是关闭的，这些数据根本没人用。

   改为：只轮询**当前持仓币**（≤6 个）的标记价，每 5 秒一次。
   实测流量 7.3 MB/天，降低 **3000 倍**。止损判断最坏延迟 5 秒，
   对 5×ATR 的止损距离来说影响可以忽略。

   候选池其他币的价格由 mark_all 因子（premiumIndex，30 秒一次）提供，
   走 factors.mark_price_of() 的 fallback 分支。

接口保持不变：live_price() / price_ago() / change_pct() / buffer_span()，
engine 和 strategies 不用改。
"""
import asyncio
import logging
import time
from collections import deque

from . import binance, db

log = logging.getLogger("ws")

PRICES = {}       # symbol -> {mark, bid, ask, ts}
PRICE_HIST = {}   # symbol -> deque[(ts, mid)]  (5 秒粒度，够 S11 之外的策略用)
_HIST_MAX = 4000

POLL_SEC = 5.0    # 持仓币轮询间隔（秒）
STALE_SEC = 12.0  # 超过这个时间没更新就认为价格失效


def _record(sym, mid):
    dq = PRICE_HIST.get(sym)
    if dq is None:
        dq = deque(maxlen=_HIST_MAX)
        PRICE_HIST[sym] = dq
    dq.append((time.time(), mid))


def live_price(symbol, max_age_sec=None):
    p = PRICES.get(symbol)
    if p and time.time() - p["ts"] <= (max_age_sec or STALE_SEC):
        return p["mark"]
    return 0.0


def price_ago(symbol, secs):
    """Price roughly `secs` seconds ago (last tick at/older than the cutoff)."""
    dq = PRICE_HIST.get(symbol)
    if not dq:
        return None
    cutoff = time.time() - secs
    best = None
    for ts, p in dq:
        if ts <= cutoff:
            best = p
        else:
            break
    if best is None:  # buffer younger than the window — use earliest if it spans enough
        first_ts, first_p = dq[0]
        if time.time() - first_ts >= secs * 0.5:
            best = first_p
    return best


def change_pct(symbol, secs):
    """Percent change of current price vs `secs` ago (negative = drop)."""
    old = price_ago(symbol, secs)
    cur = live_price(symbol)
    if old and cur:
        return (cur - old) / old * 100
    return None


def buffer_span(symbol):
    dq = PRICE_HIST.get(symbol)
    if not dq:
        return 0
    return time.time() - dq[0][0]


async def _poll(sym: str) -> None:
    try:
        d = await binance.mark_price(sym)
    except Exception:  # noqa: BLE001
        return
    rows = d if isinstance(d, list) else [d]
    for r in rows:
        if r.get("symbol") != sym:
            continue
        try:
            mark = float(r.get("markPrice") or 0)
        except (TypeError, ValueError):
            return
        if mark > 0:
            PRICES[sym] = {"mark": mark, "bid": mark, "ask": mark, "ts": time.time()}
            _record(sym, mark)


async def ws_loop():
    """轮询持仓币标记价。持仓变化时自动调整轮询名单（无需重连）。"""
    log.info("price poller started (%.0fs, positions-only, REST)", POLL_SEC)
    last_syms: list[str] = []
    while True:
        started = time.monotonic()
        try:
            syms = sorted({p["symbol"] for p in db.open_positions()})
            if syms != last_syms:
                log.info("price poller watching %d symbols: %s", len(syms), ",".join(syms) or "(none)")
                last_syms = syms
            if syms:
                await asyncio.gather(*[_poll(s) for s in syms], return_exceptions=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("price poll failed: %s", exc)
        await asyncio.sleep(max(POLL_SEC - (time.monotonic() - started), 0.2))
