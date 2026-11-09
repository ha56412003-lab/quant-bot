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
# 单个数据源的最长等待（秒）。
# clawby 中继默认 30 秒超时：启动初期因子全量刷新把它打到 429 时，
# 每个 clawby 因子都要等满 30 秒 → 6 币 × 3 个 clawby 因子 = 9 分钟，
# 界面就一直「空仓·雷达待命」。雷达宁可少一个因子也必须整轮扫完。
RADAR_SRC_TIMEOUT = 10

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
            score REAL,          -- 0(最弱) ~ 100(最强)
            r_now REAL,          -- 打分时的浮盈（R 倍数）
            verdict TEXT,        -- 强势/偏强/中性/转弱/弱势
            advice TEXT,         -- 给人工的建议
            detail TEXT          -- JSON：各因子的原始值，便于事后复盘
        );
        CREATE INDEX IF NOT EXISTS ix_radar_sym_ts ON radar_history(symbol, ts);

        -- 警报记录：什么时候叫过、叫的什么、什么档位
        -- （2026-11-09 清理：删掉 from_level / r_at_alert / eval_1h_r / eval_4h_r / eval_ts
        --   这 5 个字段从来没有被任何代码读过；eval_4h_r 更是全 NULL，功能从未跑通。
        --   历史数据已导出 docs/archive/data/radar_alerts_20261109.csv）
        CREATE TABLE IF NOT EXISTS radar_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT,
            level INTEGER,
            score REAL,
            action TEXT,
            advice TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_alert_sym_ts ON radar_alerts(symbol, ts);
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

    建议动作**统一由 _advice_action 决定** —— 早先这里自己写了一套阈值
    （浮盈 >0 就喊"建议平掉"），跟警报逻辑（浮盈 ≥0.3R 才喊）各说一套，
    出现「页面建议平掉、警报说交给系统」的自相矛盾。
    浮亏时不给离场建议 —— 那是量化系统止损该管的事，雷达不越界。
    """
    if score >= 80:
        return "走势很强", "势还在，不用动"
    if score >= 60:
        return "走势放缓", "开始放缓，盯着点"
    action, advice = _advice_action(score, r_now)
    if score >= 40:
        return ("走势转弱" if action == "half" else "中性偏弱"), advice
    return "走势力竭", advice


def reason_of(detail):
    """把各分项原始数字翻成一句人话 —— 挑最拖后腿的那一项说。

    浩哥要求：每行末尾括一句「凭什么这么判」，别只给个分数让人猜。
    只解释"为什么变弱"，走强的那一侧没有解释的必要（很强的行不带依据）。
    """
    cands = []          # (严重度 0~3, 文本)

    t = detail.get("taker") or {}
    v = t.get("taker_15m")
    if v is not None:
        if v < 0.55:
            cands.append((3, f"15分钟主动买卖比 {v}，卖盘是买盘两倍"))
        elif v < 0.80:
            cands.append((2, f"15分钟主动买卖比 {v}，卖盘明显占优"))
        elif v < 1.00:
            cands.append((1, f"15分钟主动买卖比 {v}，卖盘略占优"))

    c = detail.get("cvd") or {}
    if "cvd_up" in c:
        cu, pu = c.get("cvd_up"), c.get("px_up")
        if pu and not cu:
            cands.append((3, "价格在涨但 CVD 在跌，虚涨没有真资金"))
        elif (not pu) and cu:
            cands.append((1, "价格在跌但 CVD 在涨，有人在低位吸"))

    o = detail.get("oi") or {}
    oi, px = o.get("oi_chg_10m_pct"), o.get("px_chg_10m_pct")
    if oi is not None and px is not None:
        if px > 0 and oi < -0.05:
            cands.append((2, f"价涨 {px:.2f}% 但 OI 减 {abs(oi):.2f}%，靠空头回补推的"))
        elif px < 0 and oi > 0.05:
            cands.append((2, f"价跌 {abs(px):.2f}% 但 OI 增 {oi:.2f}%，对手在加仓"))

    l = detail.get("liq") or {}
    if l.get("long_ratio") is not None and (l.get("liq_total") or 0) >= 200_000:
        r = l["long_ratio"]
        if r > 0.70:
            cands.append((3, f"清算里多头占 {r:.0%}，多头正在被打爆"))
        elif r < 0.30:
            cands.append((1, f"清算里空头占 {1 - r:.0%}，空头在挨打"))

    b = detail.get("book") or {}
    br = b.get("bid_ask_ratio")
    if br is not None and br < 0.60:
        cands.append((1, f"盘口买盘只有卖盘的 {br:.2f}"))

    if not cands:
        return ""
    cands.sort(key=lambda x: -x[0])
    return cands[0][1]


async def sample(symbol, side, r_now=0.0, slow=True):
    """采一轮因子并打分，写 radar_history。

    全部使用 5 分钟级或实时数据（最长滞后 5 分钟）。
    slow 参数保留但已不用 —— 现在五个因子都能 1 分钟拿一次，没必要分级。
    """
    detail, parts = {}, {}

    # -- 五个因子并行取数 ----------------------------------------------------
    # 三个 clawby 因子（CVD / 清算 / 盘口）**读 factors 缓存，不自己调 clawby**。
    # 原因：clawby.relay() 是一把全局锁 + 3.2 秒硬间隔（免费版 20 req/min），
    # 雷达 6 币 × 3 个 clawby 因子 = 18 次排队要等近 1 分钟，还要排在因子
    # 刷新后面 → 整轮永远跑不完，界面一直「空仓·雷达待命」（2026-11-09）。
    # 代价：CVD 粒度从 5m 退到 1h（factors 里 cvd 就是 1h × 48 根），灵敏度
    # 下降但方向判断依然有效；清算/盘口本来就是 1h 桶，无损失。
    async def _raw_klines(iv, limit):
        try:
            ks = await binance.klines(symbol, iv, limit, isolated=True)
            return [float(k["close"]) for k in ks] if ks else []
        except Exception as exc:  # noqa: BLE001
            log.warning("radar klines %s/%s: %s", symbol, iv, exc)
            return []

    async def _raw_taker():
        try:
            t = await binance.taker_ratio(symbol, "5m", 12, isolated=True)
            return [float(x["buySellRatio"]) for x in t] if t else []
        except Exception as exc:  # noqa: BLE001
            log.warning("radar taker %s: %s", symbol, exc)
            return []

    async def _raw_oi():
        try:
            o = await binance.open_interest_hist(symbol, "5m", 12, isolated=True)
            return [float(x["sumOpenInterest"]) for x in o] if o else []
        except Exception as exc:  # noqa: BLE001
            log.warning("radar oi %s: %s", symbol, exc)
            return []

    def _cached(name, max_age):
        try:
            v = db.get_factor(name, symbol, max_age_sec=max_age)
            return v if isinstance(v, dict) else None
        except Exception as exc:  # noqa: BLE001
            log.warning("radar cache %s/%s: %s", name, symbol, exc)
            return None

    px5, px1h, ser_taker, ser_oi = await asyncio.gather(
        _raw_klines("5m", 24), _raw_klines("1h", 48), _raw_taker(), _raw_oi())
    # 缓存读取是同步的，放在 gather 之后（微秒级，不占 io）
    cvd_cache = _cached("cvd", 1800)          # 900s 刷新，放宽到半小时
    liq_cache = _cached("liq_agg", 1200)      # 300s 刷新
    ob_cache = _cached("ob_imbalance", 1800)  # 900s 刷新

    ser_cvd = list(cvd_cache.get("series") or []) if cvd_cache else []
    liq = liq_cache
    ob = ob_cache
    px = px5

    # -- 评分（纯 CPU，瞬时） ------------------------------------------------
    if ser_taker:
        parts["taker"], detail["taker"] = score_taker(ser_taker, side)
    # CVD 是 1h 粒度，价格必须用同一时间尺度，否则斜率对比无意义
    if ser_cvd and px1h:
        parts["cvd"], detail["cvd"] = score_cvd(ser_cvd, px1h, side)
        if parts["cvd"] is not None:
            detail["cvd"]["granularity"] = "1h"
    if ser_oi and px:
        parts["oi"], detail["oi"] = score_oi(ser_oi, px, side)
    if liq:
        parts["liq"], detail["liq"] = score_liq(liq, side)
    if ob:
        parts["book"], detail["book"] = score_book(
            ob.get("bids_usd"), ob.get("asks_usd"), side)

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
        "r_now": round(r_now, 3), "verdict": v, "advice": advice,
        "detail": json.dumps(detail, ensure_ascii=False),
    }
    c = conn()
    with c:
        c.execute("""INSERT INTO radar_history
            (ts, symbol, side, score, r_now, verdict, advice, detail)
            VALUES (:ts,:symbol,:side,:score,:r_now,:verdict,:advice,:detail)""",
                  row)
    return row


# ── 执行层：什么时候叫、叫几次、叫完怎么验证 ──────────────────────────
#
# 目标不是"每分钟报一次数字"（那没人看），而是：
#   ① 只在【分数跨档往下掉】的那一次叫 —— 档内抖动不叫
#   ② 叫的时候直接给可执行的动作（平一半 / 全平 / 交给系统）
#   ③ 同一个币同一个方向 30 分钟内不重复叫
#   ④ 叫完记下来，事后回填 1h/4h 走势 —— 用来判断这个雷达到底准不准

ALERT_COOLDOWN = 1800          # 同币 30 分钟内不重复叫
_alert_state = {}              # symbol -> {"level": int, "ts": int}


def _level_of(score):
    """分数 -> 档位。数字越小越弱。"""
    if score < 40:
        return 0
    if score < 60:
        return 1
    if score < 80:
        return 2
    return 3


def _advice_action(score, r_now):
    """把「分数 + 浮盈」翻译成可执行的动作。

    返回 (action, advice)：
      half  平一半（锁一半利润，剩下的让系统跑）
      full  全平
      hold  没浮盈，不掺和，交给系统止损
      watch 观望
      None  不用提醒
    """
    if score >= 60:
        return None, None
    if score < 40:
        if r_now >= 1.0:
            return "half", "走势力竭但浮盈够厚：平一半锁利润，剩下交给系统"
        if r_now >= 0.3:
            return "full", "走势力竭且利润薄：扛不住回撤，建议全平"
        return "hold", "走势力竭但没浮盈：不掺和，交给系统止损"
    # 40 ~ 60
    if r_now >= 1.5:
        return "half", "走势转弱但浮盈够厚：可以考虑平一半落袋"
    return "watch", "走势转弱：观望，等下一轮确认"


def check_alert(row):
    """一行新分数进来，判断要不要叫。

    只在【跨档往下掉】时叫，并且有冷却，避免刷屏。
    返回 alert 记录（没叫则 None）。
    """
    sym, ts, score = row["symbol"], row["ts"], row["score"]
    lvl = _level_of(score)
    c = conn()
    prev = c.execute(
        "SELECT score FROM radar_history WHERE symbol=? AND ts<? ORDER BY ts DESC LIMIT 1",
        (sym, ts)).fetchone()
    if prev is None:
        # 第一次见到这个币，只记状态不叫（没有"变化"可言）
        _alert_state[sym] = {"level": lvl, "ts": ts}
        return None
    prev_lvl = _level_of(prev["score"])
    _alert_state[sym] = {"level": lvl, "ts": ts}
    if lvl >= prev_lvl:
        return None                                  # 没跨档，不叫

    action, advice = _advice_action(score, row["r_now"])
    if not action:
        return None

    # 冷却：同一币 30 分钟内不重复叫 —— 但如果这次比上次更严重（档位更低），
    # 必须放行。否则「55 掉到 55」不刷屏的目的达到了，
    # 却会把「55 再掉到 35」这种真正要命的情况一起吞掉。
    last = c.execute(
        "SELECT ts, level FROM radar_alerts WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        (sym,)).fetchone()
    if last and ts - last["ts"] < ALERT_COOLDOWN and lvl >= last["level"]:
        return None

    rec = {"ts": ts, "symbol": sym, "side": row["side"],
           "level": lvl, "score": round(score, 1),
           "action": action, "advice": advice}
    with c:
        c.execute("""INSERT INTO radar_alerts
            (ts, symbol, side, level, score, action, advice)
            VALUES (:ts,:symbol,:side,:level,:score,:action,:advice)""", rec)
    log.info("RADAR ALERT %s %s %.0f分 %s", sym, row["side"], score, action)
    return rec


def alerts(limit=100, symbol=None):
    c = conn()
    if symbol:
        rows = c.execute("SELECT * FROM radar_alerts WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                         (symbol, limit))
    else:
        rows = c.execute("SELECT * FROM radar_alerts ORDER BY ts DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


async def scan_once():
    """扫一遍当前持仓。空仓直接返回（不扫全市场，省流量）。

    注意：mark_price 必须现取（factors.mark_price_of），
    positions 原始行里没有这个字段，直接 p.get() 会恒为 None，
    导致浮盈恒为 0、所有动作建议全部走错分支。
    """
    try:
        poss = db.open_positions()
    except Exception as exc:  # noqa: BLE001
        # 原来这里是静默 return []：一旦查询出错，界面就显示"空仓·雷达待命"，
        # 完全看不出是查询失败还是真的没仓（2026-11-09 排查时踩到）。
        log.warning("radar scan: open_positions 查询失败: %s", exc)
        return []
    out = []
    for p in poss:
        sym = p["symbol"]
        side = p["side"]
        try:
            entry = float(p.get("entry_price") or 0)
            mark = factors.mark_price_of(sym) or entry
            stop = float(p.get("stop_price") or 0)
            risk = abs(entry - stop) or entry * 0.01
            r_now = (mark - entry) / risk * (1 if side == "long" else -1) if risk else 0
            # 单币整体硬上限：五个因子各自已有超时，这里再兜一层，
            # 防止某个上游"半死不活"（连上了但不回包）把整轮拖住。
            r = await asyncio.wait_for(sample(sym, side, r_now),
                                       timeout=RADAR_SRC_TIMEOUT * 3)
            if r:
                out.append(r)
                try:
                    check_alert(r)
                except Exception as exc:  # noqa: BLE001
                    log.warning("radar alert %s: %s", sym, exc)
        except asyncio.TimeoutError:
            log.warning("radar scan %s: 超时跳过（单币上限 %ss）",
                        sym, RADAR_SRC_TIMEOUT * 3)
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
    tick = 0
    while True:
        try:
            await scan_once()
            tick += 1
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
