"""持仓强弱雷达 —— 只提示，不干预交易。

设计原则（重要，别改坏）：
  1. 本模块不 import strategies / engine 的任何判定逻辑，也不写 positions 表。
  2. 它只读持仓、采因子、算分、写 radar_history，前端展示。
  3. 交易系统照常跑，雷达喊什么都不影响它平不平仓。

回答的问题只有一个：**这个持仓的币，现在还有没有劲？**
给人工一个参考，用来决定要不要手动平一半 / 全平。

为什么不看 K 线形态：
  实测 731 笔「破均线+回撤」出场，用 K 线的形态特征（回撤幅度、放量缩量、
  ATR 收缩、破均线根数）去预测「出场后还有没有第二波」，区分度最好只有 50%
  （基准 32%）。K 线是结果，订单流才是原因，所以这里全部用订单流因子。

五个维度（权重按「离原因有多近」排）：
  CVD 背离 35  价格和主动资金是否同向 —— 背离是最强的离场信号
  主动买卖比 25  最近 30 分钟是买盘还是卖盘主动
  OI 变化   20  这波涨是「新钱进场」还是「空头回补」
  大户多空比 12  大户在加仓还是撤
  清算     8   自己人（同向持仓）被爆了多少
"""
import asyncio
import json
import logging
import sqlite3
import statistics as st
import time

from . import binance, clawby, config, db, factors

log = logging.getLogger("radar")

# 采集节奏：5m 级的因子每分钟采，1h 级的 5 分钟采一次就够（变化慢，省流量）
SCAN_SEC = 60

_conn = None
_last_slow = {}          # symbol -> ts
_running = False


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=10.0)
        _conn.row_factory = sqlite3.Row
        for p in ("PRAGMA journal_mode=WAL", "PRAGMA busy_timeout=8000"):
            try:
                _conn.execute(p)
            except sqlite3.Error:
                pass
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS radar_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT,
            score REAL,          -- -100(弱) ~ +100(强)
            s_cvd REAL, s_taker REAL, s_oi REAL, s_book REAL, s_liq REAL,
            r_now REAL,          -- 打分时的浮盈（R 倍数）
            verdict TEXT,        -- 强势/偏强/中性/转弱/弱势
            advice TEXT,         -- 给人工的建议
            detail TEXT          -- JSON：各因子的原始值，便于事后复盘
        );
        CREATE INDEX IF NOT EXISTS ix_radar_sym_ts ON radar_history(symbol, ts);
        """)
        _conn.commit()
    return _conn


# ── 各维度打分：一律返回 -1.0(最不利) ~ +1.0(最有利) ──────────────────────
#
# 设计铁律：不许用滞后指标。
# 传统技术指标（MA / MACD / RSI）本质是对过去价格的平滑，天生滞后一个周期，
# 等它转向，行情已经走完了。这里全部改用「成交层」的即时数据：
#   谁在主动成交（taker）、主动资金净流向（CVD）、新钱有没有进来（OI）、
#   自己人有没有被打爆（清算）、盘口挂单往哪边压（orderbook）。
# 最长滞后 5 分钟（一根 5m K 线），清算和盘口是实时。

def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def score_taker(series, side):
    """主动买卖比 —— 谁在主动成交。最近 3 根 5m（15 分钟）。

    买盘主动（>1）对多头有利；卖盘主动（<1）对空头有利。
    滞后：5 分钟。
    """
    if not series or len(series) < 3:
        return None, {}
    recent = series[-3:]
    avg = st.mean(recent)
    raw = (avg - 1) if side == "long" else (1 - avg)
    return _clamp(raw / 0.12), {"taker_15m": round(avg, 3),
                                "taker_last": round(series[-1], 3)}


def score_cvd(cvd_series, px_series, side):
    """CVD 短周期斜率 + 价格是否背离。最近 6 根 5m（30 分钟）。

    多头：价涨 CVD 也涨 = 真有人接（+）
          价涨 CVD 跌 = 虚涨，随时回落（-）  ← 最强的离场信号
          价跌 CVD 涨 = 砸下去有人吸（可能是洗盘）
    滞后：5 分钟。
    """
    if not cvd_series or not px_series:
        return None, {}
    n = min(len(cvd_series), len(px_series), 7)
    if n < 3:
        return None, {}
    cvd_chg = cvd_series[-1] - cvd_series[-n]
    px_chg = (px_series[-1] / px_series[-n] - 1) * 100 if px_series[-n] else 0
    cvd_up, px_up = cvd_chg > 0, px_chg > 0
    if side == "long":
        s = {(True, True): 1.0, (True, False): 0.6,
             (False, True): -1.0, (False, False): -0.6}[(cvd_up, px_up)]
    else:
        s = {(True, True): -1.0, (True, False): -0.6,
             (False, True): 1.0, (False, False): 0.6}[(cvd_up, px_up)]
    return s, {"cvd_chg": round(cvd_chg, 1), "px_chg_pct": round(px_chg, 3),
               "cvd_up": cvd_up, "px_up": px_up}


def score_oi(series, px_series, side):
    """OI 变化 —— 这波涨是「新钱进场」还是「空头回补」。最近 2 根 5m。

    多头 + 价涨 + OI 涨 = 新资金推着走（最强）
    多头 + 价涨 + OI 跌 = 空头平仓推上去的，油尽灯枯
    多头 + 价跌 + OI 涨 = 空头在加仓（危险）
    多头 + 价跌 + OI 跌 = 多头止损离场，跌势接近尾声（不算太坏）
    滞后：5 分钟。
    """
    if not series or len(series) < 3 or not px_series or len(px_series) < 3:
        return None, {}
    oi_chg = (series[-1] / series[-3] - 1) * 100 if series[-3] else 0
    px_chg = (px_series[-1] / px_series[-3] - 1) * 100 if px_series[-3] else 0
    up, oi_up = px_chg > 0, oi_chg > 0
    if side == "long":
        s = {(True, True): 1.0, (True, False): 0.3,
             (False, True): -1.0, (False, False): -0.3}[(up, oi_up)]
    else:
        s = {(False, True): 1.0, (False, False): 0.3,
             (True, True): -1.0, (True, False): -0.3}[(up, oi_up)]
    return s, {"oi_chg_10m_pct": round(oi_chg, 3), "px_chg_10m_pct": round(px_chg, 3)}


def score_liq(liq, side):
    """清算 —— 自己人正在被打爆多少。

    用聚合清算的「相对历史均值的倍数」比：多头清算倍数远高于空头，
    说明多头正在被强平踩踏，多头持仓基本到头了。
    """
    if not liq:
        return None, {}
    l_usd = liq.get("long_1h") or 0
    s_usd = liq.get("short_1h") or 0
    # 总量太小 -> 没信号。否则几十美元的清算都能算出一个极端比例，
    # 变成纯噪音往总分里灌（实测能把 15 分权重整个打满）。
    if (l_usd + s_usd) < 200_000:
        return 0.0, {"liq_total": round(l_usd + s_usd), "note": "量太小忽略"}
    lm = liq.get("long_mult") or 0
    sm = liq.get("short_mult") or 0
    if lm + sm <= 0:
        return 0.0, {"liq_total": round(l_usd + s_usd)}
    ratio = lm / (lm + sm)
    s = (1 - 2 * ratio) if side == "long" else (2 * ratio - 1)
    return _clamp(s), {"long_mult": round(lm, 2), "short_mult": round(sm, 2),
                       "long_ratio": round(ratio, 2)}


def score_book(bids, asks, side):
    """盘口挂单失衡 —— 买卖挂单往哪边压。实时。

    bids/asks > 1 说明下方接盘厚；对多头有利。
    注意：挂单可以撤，只能当辅证，所以权重给得低。
    """
    if not bids or not asks:
        return None, {}
    r = bids / asks
    raw = (r - 1) if side == "long" else (1 - r)
    return _clamp(raw / 0.35), {"bid_ask_ratio": round(r, 3)}


# ── 汇总：0 ~ 100 分 ─────────────────────────────────────────────────────
#
#   80 ~ 100  走势很强，不用管
#   60 ~ 80   走势放缓，留意
#   40 ~ 60   可观望，或部分斩仓
#   <  40     力竭，可能反转，可以考虑手动离场
#
WEIGHTS = (("taker", 25), ("cvd", 25), ("oi", 20), ("liq", 15), ("book", 15))


def verdict_of(score, r_now):
    """把分数翻译成人话 + 建议动作。

    浮亏时不给「离场」建议 —— 那是量化系统止损该管的事，雷达不越界。
    """
    if score >= 80:
        return "走势很强", "势还在，不用动"
    if score >= 60:
        return "走势放缓", "开始放缓，盯着点"
    if score >= 40:
        if r_now > 0.3:
            return "走势转弱", "可以考虑平一半锁定一部分"
        return "中性偏弱", "观望，等下一轮确认"
    if r_now > 0:
        return "走势力竭", "建议手动平掉（至少平一半）"
    return "走势力竭", "力竭但没浮盈，交给系统止损"


async def sample(symbol, side, r_now=0.0, slow=True):
    """采一轮因子并打分，写 radar_history。

    全部使用 5 分钟级或实时数据（最长滞后 5 分钟）。
    slow 参数保留但已不用 —— 现在五个因子都能 1 分钟拿一次，没必要分级。
    """
    detail, parts = {}, {}

    # 价格（5m，24 根 = 2 小时）
    px = []
    try:
        ks = await binance.klines(symbol, "5m", 24)
        px = [float(k["close"]) for k in ks] if ks else []
    except Exception as exc:  # noqa: BLE001
        log.warning("radar klines %s: %s", symbol, exc)

    # 主动买卖比（5m）
    try:
        t = await binance.taker_ratio(symbol, "5m", 12)
        ser = [float(x["buySellRatio"]) for x in t] if t else []
        if ser:
            parts["taker"], detail["taker"] = score_taker(ser, side)
    except Exception as exc:  # noqa: BLE001
        log.warning("radar taker %s: %s", symbol, exc)

    # CVD（5m，主动资金净流向）
    try:
        c = await clawby.relay_safe("futures_cvd_history",
                                    {"exchange": "Binance", "symbol": symbol,
                                     "interval": "5m", "limit": 12})
        if isinstance(c, list) and c:
            ser = [float(x.get("cum_vol_delta") or 0) for x in c]
            parts["cvd"], detail["cvd"] = score_cvd(ser, px, side)
    except Exception as exc:  # noqa: BLE001
        log.warning("radar cvd %s: %s", symbol, exc)

    # OI（5m）
    try:
        o = await binance.open_interest_hist(symbol, "5m", 12)
        ser = [float(x["sumOpenInterest"]) for x in o] if o else []
        if ser:
            parts["oi"], detail["oi"] = score_oi(ser, px, side)
    except Exception as exc:  # noqa: BLE001
        log.warning("radar oi %s: %s", symbol, exc)

    # 清算（三所聚合，1h 桶）
    try:
        liq = await factors.f_liq_agg(symbol)
        if liq:
            parts["liq"], detail["liq"] = score_liq(liq, side)
    except Exception as exc:  # noqa: BLE001
        log.warning("radar liq %s: %s", symbol, exc)

    # 盘口挂单失衡
    try:
        ob = await factors.f_ob_imbalance(symbol)
        if ob:
            parts["book"], detail["book"] = score_book(
                ob.get("bids_usd"), ob.get("asks_usd"), side)
    except Exception as exc:  # noqa: BLE001
        log.warning("radar book %s: %s", symbol, exc)

    # 缺维度就按剩余权重重分配（不是简单补 0，否则缺数据会误判成中性）
    got = [(k, w) for k, w in WEIGHTS if parts.get(k) is not None]
    if not got:
        return None
    wsum = sum(w for _, w in got)
    raw = sum(parts[k] * w for k, w in got) / wsum          # -1 ~ +1
    score = round(50 + 50 * raw, 1)                          # 0 ~ 100
    v, advice = verdict_of(score, r_now)

    row = {
        "ts": int(time.time()), "symbol": symbol, "side": side,
        "score": score,
        "s_cvd": parts.get("cvd"), "s_taker": parts.get("taker"),
        "s_oi": parts.get("oi"), "s_book": parts.get("book"), "s_liq": parts.get("liq"),
        "r_now": round(r_now, 3), "verdict": v, "advice": advice,
        "detail": json.dumps(detail, ensure_ascii=False),
    }
    c = conn()
    with c:
        c.execute("""INSERT INTO radar_history
            (ts, symbol, side, score, s_cvd, s_taker, s_oi, s_book, s_liq, r_now, verdict, advice, detail)
            VALUES (:ts,:symbol,:side,:score,:s_cvd,:s_taker,:s_oi,:s_book,:s_liq,:r_now,:verdict,:advice,:detail)""",
                  row)
    return row


async def scan_once():
    """扫一遍当前持仓。空仓直接返回（不扫全市场，省流量）。"""
    try:
        poss = db.open_positions()
    except Exception:  # noqa: BLE001
        return []
    out = []
    now = time.time()
    for p in poss:
        sym = p["symbol"]
        side = p["side"]
        entry = float(p.get("entry_price") or 0)
        mark = float(p.get("mark_price") or 0) or entry
        stop = float(p.get("stop_price") or 0)
        risk = abs(entry - stop) or entry * 0.01
        r_now = (mark - entry) / risk * (1 if side == "long" else -1) if risk else 0
        slow = now - _last_slow.get(sym, 0) >= SLOW_SEC
        try:
            r = await sample(sym, side, r_now, slow=slow)
            if r:
                out.append(r)
                if slow:
                    _last_slow[sym] = now
        except Exception as exc:  # noqa: BLE001
            log.warning("radar scan %s: %s", sym, exc)
    return out


async def loop():
    """常驻循环：每分钟扫一次持仓。由 main.py 在启动时 create_task。"""
    global _running
    if _running:
        return
    _running = True
    log.info("radar loop started (%ss)", SCAN_SEC)
    while True:
        try:
            await scan_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("radar loop: %s", exc)
        await asyncio.sleep(SCAN_SEC)


def latest(symbol=None, limit=1):
    """给 API 用：每币最近一条分数。"""
    c = conn()
    if symbol:
        return [dict(r) for r in c.execute(
            "SELECT * FROM radar_history WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, limit))]
    rows = c.execute("""SELECT r.* FROM radar_history r
        JOIN (SELECT symbol, MAX(ts) mt FROM radar_history GROUP BY symbol) m
          ON r.symbol=m.symbol AND r.ts=m.mt ORDER BY r.ts DESC""").fetchall()
    return [dict(r) for r in rows]


def history(symbol, limit=180):
    c = conn()
    return [dict(r) for r in c.execute(
        "SELECT ts, score, r_now, verdict FROM radar_history WHERE symbol=? ORDER BY ts DESC LIMIT ?",
        (symbol, limit))]
