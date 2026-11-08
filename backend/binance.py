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

from . import config, netwatch

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


async def _reconnect():
    """丢掉当前连接池，下次请求自动新建。

    为什么必须整个丢掉：代理瞬断时池里的 keep-alive 连接会全变成死连接，
    httpx 不感知对端已经断开，会继续复用 → 一直 PoolTimeout。而 httpx 的
    PoolTimeout 是「拿不到池里的槽位」，不是网络超时，所以重试同一个池
    永远失败。

    2026-11-08 事故：代理 18:58 抖动 → 服务卡死 27 分钟 → 代理 19:00 就
    恢复了，但服务一直不自愈，只能人工重启。netwatch 判定失败后每隔
    RECONNECT_COOL_SEC 秒发一次标记，这里接住重建，下一个请求就能通。
    """
    global _client
    old = _client
    _client = None
    if old is not None and not old.is_closed:
        try:
            await old.aclose()
        except Exception:  # noqa: BLE001
            pass


async def _get(base, path, params=None, timeout=15):
    """公开行情 GET —— 只读，失败自动重试 3 次（断网/代理挂/封 IP 时）。

    重试后仍失败 → netwatch 记一次故障事件；60 秒内累计 3 次 → 判定网络瘫痪，
    页面弹窗 + 飞书推送（2026-11-08 浩哥定）。
    异常仍原样抛出，调用方照旧按「数据拿不到」跳过本轮。
    """
    if netwatch.take_reconnect():
        await _reconnect()

    async def _once():
        client = _get_client()
        resp = await client.get(base + path, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    return await netwatch.with_retry(_once, f"GET {path}")


async def _signed(method, base, path, params=None, timeout=15):
    """签名私有接口。

    ★★ 写操作（POST / DELETE）绝不自动重试 ★★
    原因：place_market_order 没有 newClientOrderId 幂等键。若下单请求超时但
    币安其实已经成交，重试就会**重复开仓**——比拿不到数据危险一万倍。
    所以只有 GET（查账户/查持仓/查模式）走 3 次重试，写操作失败直接抛，
    由上层记录并放弃本轮，人工确认后在币安后台核对。

    写操作失败仍然会上报 netwatch（用于判断是不是网络瘫痪），只是不重试。
    """

    if netwatch.take_reconnect():
        await _reconnect()

    def _build():
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = 10000
        qs = urllib.parse.urlencode(p)
        sig = hmac.new(config.BINANCE_SECRET_KEY.encode(), qs.encode(),
                       hashlib.sha256).hexdigest()
        return f"{base}{path}?{qs}&signature={sig}"

    async def _once():
        url = _build()          # 每次重试都重新签名（timestamp 会过期）
        client = _get_client()
        resp = await client.request(method, url,
                                    headers={"X-MBX-APIKEY": config.BINANCE_API_KEY},
                                    timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    if method.upper() == "GET":
        return await netwatch.with_retry(_once, f"{method} {path}")

    # ── 写操作：只试一次 ──
    try:
        r = await _once()
        netwatch.report_success()
        return r
    except Exception as exc:  # noqa: BLE001
        netwatch.report_failure(f"{method} {path}（写操作，未重试）", exc)
        raise


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


async def place_market_order(symbol, side, qty, reduce_only=False, position_side=None):
    """市价下单。

    position_side：双向持仓（hedge）模式必传 LONG / SHORT，单向模式传 None。
    hedge 模式下币安不接受 reduceOnly（会报错），平仓靠「positionSide + 反向 side」
    定向，所以有 position_side 时不再传 reduceOnly。
    """
    params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}
    if position_side:
        params["positionSide"] = position_side
    elif reduce_only:
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
