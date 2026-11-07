"""Binance REST client: public futures/spot market data + signed trading.

Uses the official REST API directly (same surface the Clawby binance-cli
connector wraps); keys stay local, nothing routes through Clawby.
"""
import hashlib
import hmac
import logging
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import math
import time
import urllib.parse

import asyncio

import httpx

from . import config

log = logging.getLogger("binance")

SPOT = "https://api.binance.com"
FUT = "https://fapi.binance.com"

# ── 复用的 HTTP 连接池 ──────────────────────────────────────────────
# 旧实现每个请求 `async with httpx.AsyncClient()` 新建并关闭连接，
# 每次都要重做 TCP + TLS 握手（约 3.5KB 上下行）。ws 轮询 5s×8 币 +
# mark_all + quality + pool 合计 10 万请求/天，光握手就比响应体本身
# 大 40 倍。这里做成模块级单例，天然复用 keep-alive 连接。
# httpx.AsyncClient 绑定事件循环：脚本(新建 loop)与服务(uvicorn loop)
# 交替使用时检测 loop 变化并重建，避免 "attached to a different loop"。
_client = None
_client_loop = None


def _get_client(timeout=15):
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is None or _client.is_closed or _client_loop is not loop:
        try:
            limits = httpx.Limits(max_connections=24, max_keepalive_connections=12,
                                  keepalive_expiry=90)
        except TypeError:          # 老版本 httpx 无 keepalive_expiry
            limits = httpx.Limits(max_connections=24, max_keepalive_connections=12)
        _client = httpx.AsyncClient(
            timeout=timeout, limits=limits,
            headers={"User-Agent": "quant-bot/1.0"})
        _client_loop = loop
    return _client


async def aclose():
    """关闭连接池（lifespan 结束时调用）。"""
    global _client
    if _client is not None and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001
            pass
    _client = None


async def _get(base, path, params=None, timeout=15):
    client = _get_client()
    resp = await client.get(base + path, params=params or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


async def _signed(method, base, path, params=None, timeout=15):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 10000
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(config.BINANCE_SECRET_KEY.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{qs}&signature={sig}"
    headers = {"X-MBX-APIKEY": config.BINANCE_API_KEY}
    client = _get_client()
    resp = await client.request(method, url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# -- public market data (futures) -------------------------------------------

async def klines(symbol, interval="1m", limit=200):
    """Futures klines -> list of dicts (ts, open, high, low, close, volume).

    返回前做健全性校验：任一根出现非有限值 / 价格<=0 / high<low，
    整轮丢弃返回 []（而不是丢坏根）。理由是丢根会让后续索引错位、
    K 线时间不连续，ZLSMA/ATR 等窗口指标会静默算错；整轮丢弃后
    策略按"数据不足"跳过，是最安全的行为。

    真实风险场景：新上币、低流动性合约偶发 0 价或脏 tick。若放进去，
    一根 close=0 就能把 50 根 ZLSMA 拉歪，NaN 会让 stop_price 变 NaN
    → `price <= stop_price` 恒为 False → 止损永远不触发；同时
    risk.position_qty 会把 NaN 写进 positions，paper_balance 变 NaN，
    `equity > 0` 恒 False → 日内熔断整个失效。
    """
    raw = await _get(FUT, "/fapi/v1/klines",
                     {"symbol": symbol, "interval": interval, "limit": limit})
    out = []
    for k in raw:
        try:
            ts = int(k[0] // 1000)
            o, h, l, c, v = (float(k[1]), float(k[2]), float(k[3]),
                             float(k[4]), float(k[5]))
        except (TypeError, ValueError, IndexError):
            log.warning("klines %s %s: 无法解析的 K 线，整轮丢弃", symbol, interval)
            return []
        if not all(math.isfinite(x) for x in (o, h, l, c, v)):
            log.warning("klines %s %s: 非有限值 %s，整轮丢弃", symbol, interval, k[:6])
            return []
        if min(o, h, l, c) <= 0 or h < l:
            log.warning("klines %s %s: 价格非法 ohlc=%s，整轮丢弃",
                        symbol, interval, (o, h, l, c))
            return []
        out.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
    return out


async def mark_price(symbol=None):
    params = {"symbol": symbol} if symbol else {}
    return await _get(FUT, "/fapi/v1/premiumIndex", params)


async def depth(symbol, limit=50):
    return await _get(FUT, "/fapi/v1/depth", {"symbol": symbol, "limit": limit})


async def open_interest(symbol):
    return await _get(FUT, "/fapi/v1/openInterest", {"symbol": symbol})


async def open_interest_hist(symbol, period="1h", limit=48):
    return await _get(FUT, "/futures/data/openInterestHist",
                      {"symbol": symbol, "period": period, "limit": limit})


async def long_short_global(symbol, period="1h", limit=48):
    return await _get(FUT, "/futures/data/globalLongShortAccountRatio",
                      {"symbol": symbol, "period": period, "limit": limit})


async def long_short_top_positions(symbol, period="1h", limit=168):
    return await _get(FUT, "/futures/data/topLongShortPositionRatio",
                      {"symbol": symbol, "period": period, "limit": limit})


async def taker_ratio(symbol, period="5m", limit=48):
    return await _get(FUT, "/futures/data/takerlongshortRatio",
                      {"symbol": symbol, "period": period, "limit": limit})


async def funding_history(symbol, limit=30):
    return await _get(FUT, "/fapi/v1/fundingRate", {"symbol": symbol, "limit": limit})


async def ticker_24h_all():
    return await _get(FUT, "/fapi/v1/ticker/24hr")


# -- signed (trading + account, futures) ------------------------------------

async def account():
    return await _signed("GET", FUT, "/fapi/v2/account")


async def place_market_order(symbol, side, qty, reduce_only=False):
    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}
    if reduce_only:
        params["reduceOnly"] = "true"
    return await _signed("POST", FUT, "/fapi/v1/order", params)


async def set_leverage(symbol, leverage):
    """Best-effort set initial leverage before opening (live only)."""
    try:
        return await _signed("POST", FUT, "/fapi/v1/leverage",
                             {"symbol": symbol, "leverage": int(leverage)})
    except Exception as exc:  # noqa: BLE001
        log.warning("set_leverage %s x%s failed: %s", symbol, leverage, exc)
        return None


async def set_margin_type(symbol, margin_type="ISOLATED"):
    """把该币种保证金模式设成指定值 —— 接实盘的生死线（浩哥 2026-11-07 定）。

    币安跟 OKX/Bitget 不一样：保证金模式不是下单时带，而是 symbol 级单独设，
    设一次就记住。所以只需在「即将开仓的那个币」上设，不需要预设整个监控池。

    返回：
      "changed"  本次改成功
      "already"  本来就是目标模式（币安 -4046，不是错误）

    其他任何异常都往外抛，由调用方决定是否中止开仓。
    注意 -4047 = 该币还有挂单不让改；-4061 = 持仓方向不匹配。
    """
    try:
        await _signed("POST", FUT, "/fapi/v1/marginType",
                      {"symbol": symbol, "marginType": margin_type})
        return "changed"
    except Exception as exc:  # noqa: BLE001
        # httpx 的 raise_for_status() 异常文本不含 response body，
        # 币安的错误码在 body 里，必须自己掏出来看，否则 -4046 认不出来。
        body = ""
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                body = resp.text or ""
            except Exception:  # noqa: BLE001
                body = ""
        msg = f"{exc} {body}"
        if "-4046" in msg or "No need to change" in msg:
            return "already"
        raise


async def position_risk(symbol=None):
    """仓位风险查询：含 positionSide / marginType / leverage / liquidationPrice。

    这是逐仓验收最硬的依据 —— 改没改成功直接读 marginType 字段，
    不用看网页、不用靠"我觉得应该对了"。
    """
    params = {"symbol": symbol} if symbol else None
    return await _signed("GET", FUT, "/fapi/v2/positionRisk", params)


async def margin_of(symbol):
    """查某币当前实际保证金模式：'isolated' / 'crossed' / None（查不到）。"""
    rows = await position_risk(symbol)
    if isinstance(rows, dict):
        rows = [rows]
    for r in rows or []:
        if r.get("symbol") == symbol:
            v = str(r.get("marginType") or "").lower()
            return v or None
    return None


async def position_mode():
    """账户持仓模式：True=双向(hedge，下单必须带 positionSide)，False=单向。"""
    r = await _signed("GET", FUT, "/fapi/v1/positionSide/dual")
    return bool(r.get("dualSidePosition"))


async def multi_assets_mode():
    """账户多资产模式：True=多资产（会绕开逐仓隔离，接实盘前必须关成 False）。"""
    r = await _signed("GET", FUT, "/fapi/v1/multiAssetsMargin")
    return bool(r.get("multiAssetsMargin"))


async def exchange_info():
    return await _get(FUT, "/fapi/v1/exchangeInfo")


_qty_steps = {}


async def qty_step(symbol):
    """LOT_SIZE stepSize for rounding order quantities."""
    if not _qty_steps:
        info = await exchange_info()
        for s in info.get("symbols", []):
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    _qty_steps[s["symbol"]] = float(f["stepSize"])
    return _qty_steps.get(symbol, 0.001)


def round_step(qty, step):
    """按交易所的 stepSize 向下取整。

    ⚠ 2026-11-06 修：原实现用 len(str(step).split(".")[-1]) 推小数位。
    但交易所返回的 stepSize 可能是科学计数法字符串 —— str(1e-05) = "1e-05"，
    里面根本没有 "."，于是 precision=0 → round(qty, 0) 直接取整。
    实测 round_step(0.5, 1e-05) → 0.0（下单量变 0 被拒），
    round_step(12345.6, 1e-05) → 12346.0（名义金额错 N 倍）。
    改用 Decimal 做精确的整除，不依赖任何字符串格式推断。
    """
    if step <= 0:
        return qty
    try:
        d_step = Decimal(str(step))
        d_qty = Decimal(str(qty))
        if d_step <= 0:
            return qty
        n = (d_qty / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step
        return float(n.normalize())
    except (InvalidOperation, ValueError, TypeError):
        # 兜底：退回原来的整数运算（至少不会抛异常打断下单）
        return (qty // step) * step
