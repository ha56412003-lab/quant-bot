"""Active strategies: S02 / S06 / S11 / S14 (trimmed 2026-07-18 per backtest; the removed ones live in backup/2026-07-18-pre-trim/).

Each strategy implements evaluate(ctx) -> [Signal]; optionally check_exit(ctx,
position) -> reason|None for strategy-specific exits. All entries/holds respect
the <=24h intraday constraint via max_hold_sec.
"""
import logging
import math
import time
from dataclasses import dataclass

from . import binance, db, factors

log = logging.getLogger("strategies")


@dataclass
class Signal:
    strategy: str
    symbol: str
    side: str                 # long / short
    reason: str
    stop_price: float = 0.0
    max_hold_sec: int = 86400
    size_mult: float = 1.0
    fresh: int = 999          # 距信号触发（如 CE 翻转）已过几根 K 线，越小越新鲜
    strength: float = 0.0     # 信号强度（如 |close-ZLSMA| / ATR），越大趋势越强
    # 成本口径覆盖：None = 用全局 PAPER_FEE_RATE / PAPER_SLIPPAGE。
    # 为什么需要：S15 是挂单（maker）策略，回测按 0.02% 手续费 + 零滑点算出来
    # 的结论；如果线上按 taker 0.05% + 0.03% 滑点记账，每笔成本从 0.017R 涨到
    # 0.0425R，回测里 +21.71R 会直接翻成负数 —— 记账口径和回测不一致时，
    # 观察期看到的数字毫无意义。
    fee_rate: float | None = None
    slip_pct: float | None = None

class Ctx:
    """Per-tick context handed to strategies: factors, klines/ATR cache, positions."""

    def __init__(self, universe, monitor=None):
        self.universe = universe
        self.monitor = monitor or []
        self._kcache = {}
        self._atr = {}

    def factor(self, name, symbol=""):
        return db.get_factor(name, symbol)

    async def klines(self, symbol, interval="1h", limit=100):
        key = (symbol, interval, limit)
        if key not in self._kcache:
            self._kcache[key] = await factors.get_klines(symbol, interval, limit)
        return self._kcache[key]

    async def atr(self, symbol):
        if symbol not in self._atr:
            ks = await self.klines(symbol, "1h", 60)
            self._atr[symbol] = factors.atr_from_klines(ks) or 0.0
        return self._atr[symbol]

    async def depth_ratio(self, symbol):
        """sum(bid qty)/sum(ask qty) top-50 levels."""
        try:
            d = await binance.depth(symbol, 50)
            bids = sum(float(q) for _, q in d.get("bids", []))
            asks = sum(float(q) for _, q in d.get("asks", []))
            return bids / asks if asks else 1.0
        except Exception:  # noqa: BLE001
            return 1.0

    def price(self, symbol):
        return factors.mark_price_of(symbol)

    def has_position(self, symbol, strategy=None):
        return bool(db.open_positions(strategy=strategy, symbol=symbol))


class Base:
    sid = "BASE"
    META = {}
    FACTORS = []
    DEFAULT_INTERVAL = "15m"   # design scan cadence; live cadence comes from yaml

    def __init__(self, cfg):
        self.cfg = cfg
        # instance identity: multiple yaml instances may share one template
        # class; positions/signals/cooldowns are attributed to the instance id
        self.sid = cfg.get("_iid") or type(self).sid
        self.p = cfg.get("params", {})
        self.symbols = [s.upper() for s in (cfg.get("symbols") or [])]
        self.risk = cfg.get("risk", {})
        self.max_hold = int(float(cfg.get("max_hold_hours", 24)) * 3600)

    def syms(self, ctx):
        """Per-strategy symbol list.

        默认用「监控池」（30 个）而不是全量候选池（100 个）：执行层的
        quality.is_tradable() 只放行监控池里的币，扫其余的纯属浪费 ——
        75 个币的 K 线请求打水漂，还往 signals 表灌一堆被拦的噪音。
        监控池为空（还没刷新完）时回落到全量，避免启动初期完全不出信号。
        """
        if self.symbols:
            return self.symbols
        return getattr(ctx, "monitor", None) or ctx.universe

    async def evaluate(self, ctx) -> list:
        return []

    async def check_exit(self, ctx, pos):
        return None


def strategy_meta():
    """sid -> META for the dashboard strategy panel."""
    return {sid: cls.META for sid, cls in REGISTRY.items()}







# ── S15 Chandelier Exit [everget] + ZLSMA [veryfid] ─────────────────────────
# 纯 K 线计算，不依赖 Clawby 因子 -> 零额度消耗。
# 公式严格对齐作者原版：CE 用 useClose 极值 + 棘轮(只朝有利方向移动) + dir 状态机。


def _linreg(vals, length, offset=0):
    """Pine ta.linreg 语义：最近 length 个值做最小二乘，取 offset 位置。"""
    n = len(vals)
    out: list = [None] * n
    if length < 2 or n < length:
        return out
    sx = length * (length - 1) / 2.0
    sxx = (length - 1) * length * (2 * length - 1) / 6.0
    denom = length * sxx - sx * sx
    if denom == 0:
        return out
    for i in range(length - 1, n):
        w = vals[i - length + 1:i + 1]
        if any(v is None for v in w):
            continue
        sy = sxy = 0.0
        for j, v in enumerate(w):
            sy += v
            sxy += j * v
        slope = (length * sxy - sx * sy) / denom
        inter = (sy - slope * sx) / length
        out[i] = slope * (length - 1 - offset) + inter
    return out


def _rma(vals, length):
    """Pine ta.rma（Wilder 平滑）。"""
    n = len(vals)
    out: list = [None] * n
    acc = None
    for i in range(n):
        v = vals[i]
        if v is None:
            continue
        if acc is None:
            if i >= length - 1:
                w = vals[i - length + 1:i + 1]
                if any(x is None for x in w):
                    continue
                acc = sum(w) / length
                out[i] = acc
        else:
            acc = (acc * (length - 1) + v) / length
            out[i] = acc
    return out


def _heikin_ashi(ks):
    """把原始 K 线转成 Heikin Ashi 序列。返回 (ho, hh, hl, hc)。

    为什么用 HA：浩哥在 TradingView 上看图时切的就是 HA 图，指标（ZLSMA /
    Chandelier Exit）的输入是 HA 的收盘价，而不是原始收盘价。用原始 K 线算
    出来的买卖点和他在图上看到的对不上。
    """
    ho, hh, hl, hc = [], [], [], []
    for i, k in enumerate(ks):
        o, h, l, c = (float(k["open"]), float(k["high"]),
                      float(k["low"]), float(k["close"]))
        hc_ = (o + h + l + c) / 4.0
        ho_ = (o + c) / 2.0 if i == 0 else (ho[i - 1] + hc[i - 1]) / 2.0
        ho.append(ho_)
        hc.append(hc_)
        hh.append(max(h, ho_, hc_))
        hl.append(min(l, ho_, hc_))
    return ho, hh, hl, hc


def _bollinger_width(src, length=20, mult=2.0):
    """布林带宽度 % = (上轨 - 下轨) / 中轨 × 100。

    宽度小 = 价格挤在一起（震荡/变盘前）；宽度大 = 价格分散（有行情）。
    """
    n = len(src)
    out: list = [None] * n
    for i in range(length - 1, n):
        seg = src[i - length + 1:i + 1]
        if any(v is None for v in seg):
            continue
        ma = sum(seg) / length
        if ma == 0:
            continue
        sd = (sum((v - ma) ** 2 for v in seg) / length) ** 0.5
        out[i] = (2 * mult * sd) / ma * 100.0
    return out


def _pct_rank(series, lookback=100):
    """当前值在过去 lookback 根里的百分位（0~100）。

    用相对分位而不是绝对值，是因为不同币的波动率差好几倍，绝对阈值没法通用。
    """
    n = len(series)
    out: list = [None] * n
    for i in range(n):
        if series[i] is None:
            continue
        lo = max(0, i - lookback + 1)
        vals = [v for v in series[lo:i + 1] if v is not None]
        if len(vals) < 20:          # 样本太少，分位没有意义
            continue
        out[i] = sum(1 for v in vals if v <= series[i]) / len(vals) * 100.0
    return out


def _ce_zlsma(ks, ce_len=1, ce_mult=2.0, z_len=50, use_close=True, use_ha=True):
    """返回 {zlsma, atr, dir, close, open, low_raw, high_raw} 序列。

    use_ha=True 时所有指标输入用 Heikin Ashi 序列（对齐 TradingView 图表）；
    另附 low_raw / high_raw（原始 K 线的真实高低点），供止损取阶段低点使用 ——
    HA 的 high/low 是合成值，实盘不可能在那个价位成交。
    """
    n = len(ks)
    low_raw = [float(k["low"]) for k in ks]
    high_raw = [float(k["high"]) for k in ks]
    if use_ha:
        openp, high, low, close = _heikin_ashi(ks)
    else:
        close = [float(k["close"]) for k in ks]
        openp = [float(k["open"]) for k in ks]
        high = high_raw[:]
        low = low_raw[:]

    l1 = _linreg(close, z_len, 0)
    l2 = _linreg(l1, z_len, 0)
    zlsma = [None if (a is None or b is None) else a + (a - b) for a, b in zip(l1, l2)]

    tr = [None] * n
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = _rma(tr, ce_len)

    long_stop: list = [None] * n
    short_stop: list = [None] * n
    dirs: list = [1] * n
    for i in range(n):
        if atr[i] is None or i < ce_len:
            continue
        if use_close:
            hh = max(close[i - ce_len + 1:i + 1])
            ll = min(close[i - ce_len + 1:i + 1])
        else:
            hh = max(high[i - ce_len + 1:i + 1])
            ll = min(low[i - ce_len + 1:i + 1])
        adj = ce_mult * atr[i]
        ls_raw, ss_raw = hh - adj, ll + adj
        ls_prev = long_stop[i - 1] if long_stop[i - 1] is not None else ls_raw
        ss_prev = short_stop[i - 1] if short_stop[i - 1] is not None else ss_raw
        # 棘轮：多头止损只上移，空头止损只下移
        long_stop[i] = max(ls_raw, ls_prev) if close[i - 1] > ls_prev else ls_raw
        short_stop[i] = min(ss_raw, ss_prev) if close[i - 1] < ss_prev else ss_raw
        if close[i] > ss_prev:
            dirs[i] = 1
        elif close[i] < ls_prev:
            dirs[i] = -1
        else:
            dirs[i] = dirs[i - 1]
    bbw = _bollinger_width(close, 20, 2.0)
    bbw_pct = _pct_rank(bbw, 100)
    return dict(zlsma=zlsma, atr=atr, dir=dirs, close=close, open=openp,
                long_stop=long_stop, short_stop=short_stop,
                low_raw=low_raw, high_raw=high_raw,
                bbw=bbw, bbw_pct=bbw_pct)


_LAST_BAR = {}   # (sid, symbol) -> 已评估过的K线时间戳，防止高频扫描重复出信号

# 三道光闸诊断：每根 15m K 线评估完把三道闸的即时状态存在这里。
# 只写内存、不额外拉 K 线（扫描本来就在算 CE/HA/BBW，算完原本是丢掉的）。
# 页面用它回答「今天是没机会，还是门槛卡死了」。
_GATE_STATE = {}   # symbol -> {ts, dir, ce, ha, bbw, bbw_thr, atr_pct, ok, why}


# ── S15 趋势反转出场的 bar 边界缓存 ──────────────────────────────────────
# check_exit 每 0.5 秒被调用一次（engine.position_loop），8 个仓位就是
# 16 次/秒。若每次都去拉 1h K 线，一天约 553MB —— 而且 99.9% 的调用
# 拿到的都是同一根未收盘 bar，算出来的 ZLSMA 完全一样。
# 这里只缓存「当前 bar 的 ZLSMA / ATR 值」，跨过 bar 边界才重新拉取。
# 键: (symbol, interval)  值: (cur_bar, zlsma, ha_close, bar_ts, dir_now, dir_prev, ha_open)
_S15_ZL_CACHE = {}

# ── S15 逐仓位状态（峰顶浮盈 / 连续破均线计数）──────────────────────────
# 键: (symbol, side, opened_at) -> {"peak": float, "bar_ts": any, "cnt": int}
# 为什么不放数据库：这两个值只影响出场时机，丢了最多晚一根 K 线出场，
# 不值得为它加一次写库（positions 每次写都要加锁，已有 database is locked 隐患）。
_S15_POS_STATE = {}

# ── S15 镜像出场的逐仓位去重状态 ──────────────────────────────────────
# 键: pos_id（或 symbol）-> {"bar_ts": 已判定过的 K 线时间戳}
# 目的：同一根 15m K 线只判一次镜像出场，避免 0.5 秒一轮反复触发。
_S15_MIRROR_STATE = {}
# 键: pos_id（或 symbol）-> {"bar_ts": 已判定过的 K 线时间戳}
# 目的：右侧止盈同一根 K 线只判一次（2026-11-10 B 档新增，与镜像各存一份，
#       互不干扰 —— 两条规则判的是不同条件，共用一份会导致"被一条吃掉"）。
_S15_RIGHT_STATE = {}


def _atr_n(ks, n=20):
    """ATR(n) 序列（真实波幅均值）。与 backtest/exit_speed_test._atr20 同款。

    ⚠ 必须用**原始 K 线**，不能用 HA —— HA 平滑过，实体天然偏小会低估力度。
    ⚠ 这个 ATR(20) 和 CE 的 ATR(1) 是两个东西：CE 的 ATR(1)=当根 TR，
      进场那根往往是平时的 3~5 倍，用它当尺子会把门槛放大成 2~3%。
    """
    tr: list = []
    out: list = [None] * len(ks)
    for i, k in enumerate(ks):
        h = float(k["high"])
        l = float(k["low"])
        if i == 0:
            tr.append(h - l)
            out[0] = h - l
            continue
        pc = float(ks[i - 1]["close"])
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        if i >= n - 1:
            out[i] = sum(tr[i - n + 1:i + 1]) / n
    return out


def _is_key_bar(ks, i, atr_n, want, mult=1.0, closepos=0.66, bodyfrac=0.5):
    """关键K线（大阳线 / 大阴线）—— 三条同时满足，全部用原始 K 线：

      ① 力度：实体 > mult × ATR(20)        「比平常波动明显大」← mult 唯一控制项
      ② 掌控力：收盘位置 > closepos        「收在最高/最低的 1/3 区」
      ③ 决断力：实体占全幅 > bodyfrac      「实体比影线长，不是十字星」

    收盘位置是核心：收盘价 = 多空最终达成的共识，收在极值说明对手毫无反击。

    实测（40 币 · 24 万根 15m）：三条全满足通过率 mult=1.0 → 9.34%，
    0.8 → 15.00%，0.7 → 18.83%，0.6 → 23.30%；普通 K 线实体中位仅 0.25%，
    关键K线实体中位 0.73%(0.7×) ~ 0.96%(1.0×)。它是七道闸里最窄的一道。
    """
    o = float(ks[i]["open"])
    c = float(ks[i]["close"])
    h = float(ks[i]["high"])
    l = float(ks[i]["low"])
    rng = h - l
    if rng <= 0:
        return False
    a = atr_n[i] if atr_n is not None and i < len(atr_n) else None
    if a is None or a <= 0:
        return False
    body = abs(c - o)
    if body <= mult * a:              # ① 力度不够
        return False
    if body / rng <= bodyfrac:        # ③ 影线太长
        return False
    if want == "long":
        if c <= o:                    # 必须是阳线
            return False
        return (c - l) / rng > closepos   # ② 收在上 1/3
    if c >= o:                        # 必须是阴线
        return False
    return (h - c) / rng > closepos       # ② 收在下 1/3


class S15CeZlsma(Base):
    sid = "S15_CE_ZLSMA"
    FACTORS = []                 # 不需要任何 Clawby 因子，不消耗额度
    DEFAULT_INTERVAL = 900       # 15m K线，每 15 分钟检查一次
    META = {
        "name": "CE+ZLSMA 趋势跟随", "type": "趋势跟随 / 短线", "direction": "双向",
        "logic": "Chandelier Exit 用 ATR 画一条只朝有利方向移动的止损线来判定大方向，"
                 "方向翻转那一刻就是信号；ZLSMA(50) 零延迟均线用来确认趋势站没站稳；"
                 "布林带宽度分位衡量波动是否扩张，只在市场真的在动的时候才出手。"
                 "入场用真实收盘价算钱，用 HA 合成价判方向，用原始 K 线判力度。",
        "entry": "七道闸全过才开仓：① CE 方向翻转 ② HA 方向K线（阳线站上 ZLSMA / 阴线跌破）"
                 " ②b 那根原始 K 线本身是「关键K线」（实体 > 0.7×ATR20，且收盘落在极值 1/3 区）"
                 " ③ 布林带宽度近 100 根分位 > 70% ④ 进场价离 ZLSMA ≥ 1.0×ATR20（自适应偏离）"
                 " ⑤ 止损距离不超过 5% ⑥ 进场前 48 根的走势效率 ≥ 0.30（单边才做）",
        "exit": "平半 + 右侧，共四条：① 硬止损 = 关键K线低点 / 8 根摆动 / CE 通道，三条取最近"
                " ② 到 1.2R 平一半（先锁定不亏）"
                " ③ 平半后出现反向关键K线且 HA 收穿 ZLSMA50 → 平剩余（右侧止盈）"
                " ④ 平半后一直不穿均线 → 到 2R 全部平（兜底）。"
                "已删除：时间止损、快速失败、移动止盈、镜像出场。",
        "factors": "15m K线（自算 CE(1×1.5) / ZLSMA50 / ATR / 布林带宽度分位）",
        "risk": "震荡市 CE 会频繁翻转，所以入场加了 BBW 分位闸（近 100 根布林带宽度分位 > 70%），"
                "把开仓限在波动扩张期。费率口径敏感：市价往返 0.10%，止损只有 0.50% 时仓位放大到"
                "3 倍本金，手续费=单笔风险的 20%；止损 1.0% 时 10%，2% 时 5%。"
                "所以监控池进池线 0.50%、出池线 0.30%（出池线必须更低，否则池子来回抖）。",
        "name_en": "CE + ZLSMA Trend Follow",
        "logic_en": "Chandelier Exit ratchets an ATR stop that defines the regime; a flip is the signal. "
                    "ZLSMA(50) confirms price is on the right side; Bollinger-band width percentile "
                    "gates entries to expanding-volatility regimes. Directions use HA prices, "
                    "money uses real closes, bar strength uses raw candles.",
        "entry_en": "All seven gates: (1) CE direction flip (2) HA bar on the right side of ZLSMA50 "
                    "(3) that same raw candle is a key bar (body > 0.7x ATR20 and closing in the "
                    "extreme third) (4) BBW percentile over the last 100 bars > 70% "
                    "(5) entry price at least 1.0x ATR20 away from ZLSMA (hugging the line = false "
                    "breakout) (6) stop distance <= 5% of price (7) trend efficiency of the previous "
                    "48 bars >= 0.30 (only trade one-sided moves).",
        "exit_en": "Four exits: (1) hard stop = nearest of key-bar extreme / 8-bar swing / CE channel "
                   "(2) take half at 1.2R (3) after the half, a reverse key bar whose HA close "
                   "crosses back through ZLSMA50 closes the rest (right-side take-profit) "
                   "(4) if it never crosses, the remainder closes at 2.0R as a backstop. "
                   "Removed: 16h time stop, quick-fail, trailing stop, mirror exit.",
        "factors_en": "15m klines (self-computed CE 1x1.5 / ZLSMA50 / ATR / Bollinger width percentile)",
        "risk_en": "CE flips constantly in chop, so entries add a BBW percentile gate (>70% over last "
                   "100 bars) restricting them to expanding-volatility regimes. Fee-sensitive: a "
                   "0.10% taker round-trip costs 20% of per-trade risk when the stop is only 0.50% "
                   "wide (position = 3x equity), 10% at 1.0%, 5% at 2%. Hence the pool requires "
                   "stop distance >= 0.50% and drops coins below 0.30%.",
    }

    def _p(self, key, default):
        try:
            return type(default)(self.p.get(key, default))
        except (TypeError, ValueError):
            return default

    async def evaluate(self, ctx) -> list:
        """入场：CE 方向翻转（事件）+ 均线同侧关键K线 + 布林带宽度分位 + 偏离闸 + 止损距离。

        设计要点（都是 2026-11-05 那轮回测定下来的）：
          · CE 方向翻转是「事件」不是「状态」—— 只在翻那一刻出信号，否则每根
            都满足条件会刷出 7 倍的假箭头。
          · 布林带宽度分位 > 70% 用来排除震荡市 —— 布林带宽度只衡量「价格分散
            程度」，是唯一没有被证伪的趋势过滤器（ADX、ADX上升、效率比全部失效）。
          · 进场价离 ZLSMA 至少 dev_atr_mult×ATR(20)（B 档 1.0×）：贴均线 = 假突破。
          · 止损距离上限 5%：离得太远的单赔率差，实盘 80 笔验证过。
          · 止损 = 三条并列取近：左侧 8 根摆动 / CE 通道 / 关键K线极值（都用原始 K 线）。
          · ATR 门槛（min_atr_pct）已挪到监控池层，S15 里保持 0 = 不启用。
        """
        ce_len = int(self._p("ce_len", 1))
        ce_mult = float(self._p("ce_mult", 2.0))
        z_len = int(self._p("zlsma_len", 50))
        use_close = bool(self._p("use_close", True))
        use_ha = bool(self._p("use_ha", True))
        swing_lb = int(self._p("swing_lookback", 20))
        min_atr_pct = float(self._p("min_atr_pct", 2.5))
        bbw_pct_min = float(self._p("bbw_pct_min", 70.0))
        max_risk_pct = float(self._p("max_risk_pct", 30.0))
        # ⑥ 进场位置闸（2026-11-08 浩哥批准落地）：进场价必须离 ZLSMA 至少这个百分比。
        #    0 = 关闭该闸，行为退回三闸（回滚只需把这行配置删掉或设 0）。
        min_dev_pct = float(self._p("min_dev_pct", 0.0))
        # ④b 偏离闸（2026-11-10 B 档）：自适应门槛 = max(min_dev_pct, mult × ATR(20)%)
        #    mult=1.0 就把「离均线太近」的假突破挡在门外，且随每个币的波动自动缩放。
        dev_atr_mult = float(self._p("dev_atr_mult", 0.0))
        # ②b 关键K线闸（2026-11-10 浩哥定义）：HA 判方向，原始 K 线判力度
        key_bar = bool(self._p("key_bar", False))
        key_mult = float(self._p("key_mult", 1.0))
        key_closepos = float(self._p("key_closepos", 0.66))
        key_bodyfrac = float(self._p("key_bodyfrac", 0.5))
        # ⑤b 关键K线止损：那根大阳线的低点 / 大阴线的高点
        stop_key = bool(self._p("stop_key", False))
        # ⑥ 进场 EFF 闸（2026-11-10 从池子闸挪到进场闸，回测净R +66%）
        eff_min_sig = float(self._p("eff_min_signal", 0.0))
        eff_win_sig = int(self._p("eff_win_signal", 48))
        # 成本口径：默认按 maker 0.02% / 零滑点，和回测一致。
        # ⚠ 这是「假设挂单能成交」的记账口径，不代表线上真能挂到单。
        #    真实限价挂单能力是执行层待办（观察期后单独做）。
        fee_rate = float(self._p("fee_rate", 0.0002))
        slip_pct = float(self._p("slip_pct", 0.0))
        kline_iv = str(self.p.get("kline_interval", "15m"))

        # EFF 窗口也在 need 里（2026-11-10 从池子闸挪到进场闸）
        need = max(ce_len, z_len * 2, swing_lb, eff_win_sig, 100) + 80
        out = []

        for sym in self.syms(ctx):
            try:
                if ctx.has_position(sym, self.sid):
                    continue
                ks = await ctx.klines(sym, kline_iv, need)
                if not ks or len(ks) < need:
                    continue

                ind = _ce_zlsma(ks, ce_len, ce_mult, z_len, use_close, use_ha)
                # _ce_zlsma 已内部算好 bbw / bbw_pct，这里不再重复计算
                bbw = ind["bbw"]
                bbw_pct = ind["bbw_pct"]
                # 关键K线 / 自适应偏离闸 / 关键K线止损 都要 ATR(20)，每个币只算一次
                atr_n = (_atr_n(ks, 20)
                         if (key_bar or stop_key or dev_atr_mult > 0) else None)

                i = len(ks) - 2          # 最后一根已收完的 K 线
                if i < 1 or i < swing_lb:
                    continue
                bar_key = (self.sid, sym)
                bar_ts = ks[i].get("ts", i)
                if _LAST_BAR.get(bar_key) == bar_ts:
                    continue
                _LAST_BAR[bar_key] = bar_ts

                z, atr = ind["zlsma"][i], ind["atr"][i]
                dirs_arr = ind["dir"]
                if z is None or atr is None or atr <= 0:
                    continue
                z, atr = float(z), float(atr)
                c, o = ind["close"][i], ind["open"][i]
                if not (math.isfinite(c) and math.isfinite(z)):
                    continue

                # ① CE 方向翻转（事件，不是状态）
                d_now = int(dirs_arr[i] or 1)
                d_prev = int(dirs_arr[i - 1] or 1)

                # ── 三道光闸诊断（纯内存，零额外流量）──────────────────────
                # 三步的判断本来就要做，这里只是把结果留下给页面看。
                # 顺序：CE 翻转 → HA 形态 → BBW 分位，全过才开仓。
                _bp = bbw_pct[i]
                _ha_ok = (c > z and c > o) if d_now == 1 else (c < z and c < o)
                _thr = bbw_pct_min if bbw_pct_min > 0 else 70.0
                _ce_ok = d_now != d_prev
                # ⚠ 2026-11-06 修：原来写成 `_bp is None or _bp >= _thr`，
                # 数据缺失时诊断表显示"这道过了"，而下面 ④ 的实际开仓判断是
                # `bp is None → continue`（拒绝）。两处相反，诊断会骗人。
                # 现在与开仓逻辑对齐：门槛开着时，None 一律算不通过。
                if bbw_pct_min > 0:
                    _bbw_ok = _bp is not None and _bp >= _thr
                else:
                    _bbw_ok = True          # 策略层关掉 BBW 门槛，不参与判断
                if not _ce_ok:
                    _why = "CE没翻转"
                elif not _ha_ok:
                    _why = "HA形态不对"
                elif not _bbw_ok:
                    _why = ("BBW数据缺失" if _bp is None
                            else f"BBW差{_thr - _bp:.0f}点")
                else:
                    _why = "应开仓"
                _GATE_STATE[sym] = {
                    "ts": bar_ts, "dir": "多" if d_now == 1 else "空",
                    "ce": _ce_ok, "ha": _ha_ok, "bbw": _bp, "bbw_thr": _thr,
                    # 2026-11-10 B 档：偏离闸已改成自适应 = max(固定下限, mult×ATR20%)。
                    # 面板必须在这里就填真实门槛 —— 否则没走到第④闸的币 dev_thr=0，
                    # 前端按「门槛 0 一律算通过」显示，第④道闸会永远绿，面板会骗人。
                    "dev": (round(abs(float(ks[i]["close"]) - z) / z * 100.0, 3)
                            if z else None),
                    "dev_thr": round(max(
                        min_dev_pct,
                        (dev_atr_mult * (atr_n[i] / float(ks[i]["close"]) * 100.0)
                         if (dev_atr_mult > 0 and atr_n is not None
                             and i < len(atr_n) and atr_n[i]
                             and float(ks[i]["close"]) > 0) else 0.0)
                    ), 3),
                    # 2026-11-09：第五道闸（止损距离上限）在面板上原本不可见，
                    # 被拦的币显示「四闸全过·待成交」，等一晚上也不开仓，误导。
                    # 这里先占位，算完止损后回填真实值。
                    "risk": None, "risk_thr": max_risk_pct,
                    "atr_pct": round(atr / c * 100.0, 3) if c > 0 else None,
                    "ok": False, "why": _why,
                }

                if d_now == d_prev:
                    continue

                # ② ATR 门槛 —— 2026-11-06 已搬到池子模块（5m 双档）
                #    池子只管「资格」（这个币能不能做），策略只管「时机」（何时进出）
                #    yaml 里 min_atr_pct 设 0 即关闭；保留代码是为了回测对比
                atr_pct = atr / c * 100.0 if c > 0 else 0.0
                if min_atr_pct > 0 and atr_pct < min_atr_pct:
                    continue

                # ③ 关键 K 线（HA 阳线站上均线 / 阴线跌破均线）
                #    2026-11-06 核对后调整：HA 形态必须在 BBW 之前判断。
                #    顺序定为 CE → HA → BBW，与「三道光闸」诊断表、与浩哥的理解一致。
                #    两种顺序不影响开不开仓（都是 continue），但会影响诊断显示「卡在哪道」。
                key_long = c > z and c > o
                key_short = c < z and c < o
                if d_now == 1 and not key_long:
                    continue
                if d_now == -1 and not key_short:
                    continue

                # ③b 关键K线闸（2026-11-10 B 档新增）—— 浩哥定义
                #     HA 只判「方向对不对」，这根 K 线本身还得是「大阳线 / 大阴线」。
                #     为什么必须用原始 K 线：HA 平滑过，实体天然偏小，会系统性低估力度。
                #     它是七道闸里最窄的一道（挡掉约 82% 的信号），也是单量的总闸门。
                want = "long" if d_now == 1 else "short"
                if key_bar and not _is_key_bar(ks, i, atr_n, want,
                                               key_mult, key_closepos, key_bodyfrac):
                    _g0 = _GATE_STATE.get(sym)
                    if _g0 is not None:
                        _g0["why"] = "关键K线力度不足"
                    continue

                # ③c 进场 EFF 闸（2026-11-10 从池子闸挪到进场闸）
                #     池子里筛「这个币最近有没有趋势」和进场时确认「此刻是不是单边」是两回事。
                #     只看进场前 eff_win_signal 根，末价位移 ÷ 振幅 ≥ 0.30 才算单边。
                if eff_min_sig > 0:
                    from .quality import _eff        # 延迟导入，避免模块循环
                    _e = _eff(ks[:i + 1], eff_win_sig)
                    if _e is None or _e < eff_min_sig:
                        _g0 = _GATE_STATE.get(sym)
                        if _g0 is not None:
                            _g0["why"] = ("趋势效率无数据" if _e is None
                                          else f"趋势效率{_e:.2f}")
                        continue

                # ④ 布林带宽度分位门槛 —— 2026-11-06 定：必须留在策略层，且必须 15 分钟
                bp = bbw_pct[i]
                if bbw_pct_min > 0 and (bp is None or bp < bbw_pct_min):
                    continue

                # 委托价用「真实收盘价」，不用 HA 合成价。
                # 2026-11-06 定案（方案 B）：指标域用 HA 判方向，委托域用真实价算钱。
                # 实测证据：LIT 那笔 HA 收盘 3.4170，真实收盘 3.4530（差 +2.11%），
                # 导致实际盈亏比从 1.20 掉到 0.73、单笔风险从 0.5% 放大到 0.82%。
                px = float(ks[i]["close"])
                if px <= 0 or not math.isfinite(px):
                    continue

                # ⑥ 进场位置闸 —— 进场价离 ZLSMA 的距离必须够远（2026-11-08 浩哥批准）
                #
                #    为什么加这道（60 天 / 20 币回测，docs/S15_参数优化证伪记录.md 附二）：
                #      偏离 <=0.5% 的 104 笔：净R −14.8  均笔 −0.143  胜率 34.6%  止损率 51.9%
                #      偏离  >0.5% 的 640 笔：净R +90.9  均笔 +0.142  胜率 47.8%  止损率 31.7%
                #    贴均线进场 = 突破还没走出去 = 假突破；离均线够远才说明趋势真启动了。
                #
                #    与前三闸的关系（实测，不是推理）：
                #      · 与 BBW 分位相关系数 −0.020 → 完全不冗余
                #      · 与 HA 形态同向（HA 要求「在均线这一侧」，本闸要求「离得够远」）→ 不矛盾
                #      · 只剔掉 HA 通过但贴均线的 14%，不是限制过死
                dev_pct = abs(px - z) / z * 100.0 if z else 0.0
                # 2026-11-10 B 档：门槛自适应 = max(固定下限, mult × ATR(20) 占价格的百分比)
                #   为什么要自适应：BTC 和 DOGE 的 0.5% 完全不是一个东西。
                #   ATR(20) 是「平常一根 K 线走多远」，用它的倍数当尺子，跨币可比。
                _dthr = min_dev_pct
                if dev_atr_mult > 0:
                    _a20 = atr_n[i] if atr_n is not None and i < len(atr_n) else None
                    _apct = (_a20 / px * 100.0) if _a20 else 0.0
                    _dthr = max(_dthr, dev_atr_mult * _apct)
                _g = _GATE_STATE.get(sym)
                if _g is not None:
                    _g["dev"] = round(dev_pct, 3)
                    _g["dev_thr"] = round(_dthr, 3)
                if _dthr > 0 and dev_pct < _dthr:
                    if _g is not None:
                        _g["why"] = f"离均线仅{dev_pct:.2f}%（需{_dthr:.2f}%）"
                    continue

                # ⑤ 止损 = 三条并列取近（2026-11-10 B 档 · 浩哥定义）
                #   ① 左侧 swing_lb 根摆动低/高点（原始 K 线，含进场那根）
                #   ② CE 通道（ce_mult × ATR(1)，棘轮只进不退）
                #   ③ 关键K线的低点（多）/ 高点（空）
                #   多头取「三者中最高」、空头取「最低」= 离入场价最近的那条。
                #   为什么取近：价格会先打到近的那条，留一条更远的等于不设；
                #   取近也顺带压小单笔风险，同样的风险比例能开出更大仓位。
                if d_now == 1:
                    stop = min(ind["low_raw"][i - swing_lb + 1:i + 1])
                    side = "long"
                else:
                    stop = max(ind["high_raw"][i - swing_lb + 1:i + 1])
                    side = "short"
                stop_src = "swing"
                _ce_arr = ind.get("long_stop" if side == "long" else "short_stop")
                _ce_stop = _ce_arr[i] if _ce_arr is not None and i < len(_ce_arr) else None
                if _ce_stop is not None:
                    try:
                        _ce_stop = float(_ce_stop)
                    except (TypeError, ValueError):
                        _ce_stop = None
                if _ce_stop is not None and math.isfinite(_ce_stop):
                    if (side == "long" and _ce_stop > stop) or \
                       (side == "short" and _ce_stop < stop):
                        stop = _ce_stop
                        stop_src = "ce"
                if stop_key:
                    _kb = float(ks[i]["low"] if side == "long" else ks[i]["high"])
                    if (side == "long" and _kb > stop) or \
                       (side == "short" and _kb < stop):
                        stop = _kb
                        stop_src = "key"
                risk = (px - stop) if side == "long" else (stop - px)

                if risk <= 0 or not math.isfinite(risk):
                    continue
                # ⑤ 止损距离上限（第五道闸）—— 2026-11-09 浩哥定：30% → 5%
                #    止损距离 = 1R 的大小。太宽的单子价格根本跑不到 1.8R/2R：
                #    实盘 80 笔中止损 ≥5% 的 23 笔（占 29%）合计 -118.78U，均峰值仅 0.32R。
                #    这里回填面板可见值，让「被拦」有痕迹，不再假装「待成交」。
                _rpct = risk / px * 100.0
                if _g is not None:
                    _g["risk"] = round(_rpct, 3)
                if _rpct > max_risk_pct:
                    if _g is not None:
                        _g["why"] = f"止损太远{_rpct:.2f}%"
                    # 2026-11-09：以前这里静默 continue —— 事后查「这个币明明有信号
                    # 却没开仓」无从下手：信号流里没有它（信号压根没生成），
                    # 光闸面板也不显示原因。留一行痕迹。
                    # 只有过了前四道闸的币才会走到这里（实测每轮 1~3 个），不会刷屏。
                    _msg = (f"S15 {sym} 止损距离 {_rpct:.2f}% > {max_risk_pct:.1f}%"
                            f"（上限），跳过开仓")
                    log.info(_msg)
                    try:
                        db.log("info", _msg)
                    except Exception:  # noqa: BLE001
                        pass          # 记日志失败绝不能影响开仓判定
                    continue
                # 2026-11-08 清理：原来这里还校验 tp > 0（tp = px ± risk×rr_ratio）。
                # 那道校验在现有配置下恒成立 —— max_risk_pct 上限（现为 5%）已保证
                # risk ≤ 0.05×px，空头 tp = px − 1.2×risk ≥ 0.94×px > 0；多头 px + 1.2×risk 必正。
                # rr_ratio 只是个让 tp 保持为正的占位值，改成 1.0 / 2.0 / 3.0 结果完全一样，
                # 而 take_profit 落库时硬写 0.0（固定止盈已关）。所以 rr_ratio 和 tp
                # 整条链路一并删除，行为零变化。
                if not (math.isfinite(stop) and stop > 0 and px > 0):
                    continue

                # 这里只是「信号已生成」——后面还要过 risk.allow_entry 九道闸
                # （熔断 / 日目标 / 事件静默 / 并发 / 杠杆 / 单币上限…），
                # 任何一道拒绝都不会有仓位。所以策略层只能说「四闸全过」，
                # 真实结论由 /api/gates 用持仓表 + 信号表的 skip_reason 覆盖。
                _GATE_STATE[sym]["ok"] = True
                _GATE_STATE[sym]["why"] = "全闸通过"
                out.append(Signal(
                    strategy=self.sid, symbol=sym, side=side,
                    reason=(f"CE{'多' if side == 'long' else '空'}翻向+"
                            f"{'阳' if side == 'long' else '阴'}线"
                            f"{'站上' if side == 'long' else '跌破'}ZLSMA "
                            f"ATR={atr_pct:.2f}%"
                            + (f" BBW分位={bp:.0f}%" if bp is not None else "")),
                    stop_price=round(stop, 8),
                    max_hold_sec=self.max_hold,
                    fresh=0, strength=abs(c - z) / atr,
                    fee_rate=fee_rate, slip_pct=slip_pct,
                ))
            except Exception as exc:  # noqa: BLE001
                log.warning("S15 %s 计算失败: %s", sym, exc)
        return out

    async def check_exit(self, ctx, pos):
        """S15 出场总入口（2026-11-10 B 档重排，浩哥定案）。

        出场共 **4 条**，按判定顺序（①②③④ 在这里，⑤ 在 engine.py）：

            —— 本方法负责（每 0.5 秒判一次）——
            ① 平一半：  现价 ≥ half_at_r(1.2R) → 平掉 50%
                         （_half_take_profit）
            ② 兜底全平：已平半 且 现价 ≥ full_at_r(2.0R) → 平剩余
                         （_full_target）—— 防止一直不穿均线时没有出口
            ③ 右侧止盈：已平半 且 HA 反向 + HA收穿 ZLSMA50
                         + 那根原始K线本身是反向关键K线（_right_exit）
            ④ 镜像出场：CE 翻转 + HA 反向 + BBW 分位≥70
                         （_mirror_exit）—— **B 档已关闭**（mirror_exit: false）

            —— engine.py 负责（优先级更高，先判）——
            ⑤ 硬止损（三条并列取近的那条：关键K线低点 / 8 根摆动 / CE 通道）

        ★ 2026-11-10 B 档删掉的三条：
           · 16h 时间止损（max_hold_hours: 0）—— 把「慢性子但最终赚钱」那批砍在 4 小时线上
           · 快速失败（quickfail_hours: 0）—— 实跑 12 笔 −440U
           · 镜像出场（mirror_exit: false）—— 与 ③ 后两条完全重叠，且会抢在 ③ 之前掐单

        ★ 平半后的逻辑变了（与旧版最大的区别）：
           旧：3R 全平 + 移动止盈 1.5/1.2（两条都在盯着"已经赚到的钱不要吐回去"）
           新：右侧止盈（等反向信号）+ 2R 兜底 —— 让趋势自己走完，不主动收网

        流量：check_exit 每 0.5 秒被调用一次，6 个仓位就是 12 次/秒。
        这里按「bar 边界」缓存指标 —— 一根 K 线没收盘指标不会变，
        只有跨过 bar 边界才真正去拉数据。不做这个缓存是每天几百 MB。
        """
        try:
            # 峰顶维护（无条件，必须）—— 前端「峰顶R / 峰顶→当前价回撤区」靠它。
            # 原先这段藏在 _mirror_exit 里，镜像一关峰顶就冻结，所以提到最前面。
            self._refresh_peak(ctx, pos)

            # ⓪ 快速失败 —— B 档已删除（quickfail_hours: 0，方法内部直接返回 None）
            quick = self._quick_fail(ctx, pos)
            if quick:
                return quick

            # ① 平一半（half_at_r = 1.2R）
            half = await self._half_take_profit(ctx, pos)
            if half:
                return half

            # ② 兜底全平（full_at_r = 2.0R）→ ③ 右侧止盈
            #    顺序与回测 sim() 一致：先 2R，再右侧
            full = self._full_target(ctx, pos)
            if full:
                return full
            right = await self._right_exit(ctx, pos)
            if right:
                return right

            # ④ 镜像出场（B 档 mirror_exit: false → 方法内部立即返回 None）
            return await self._mirror_exit(ctx, pos)
        except Exception as exc:  # noqa: BLE001
            log.warning("S15 check_exit %s 失败: %s", pos.get("symbol"), exc)
            return None

    def _refresh_peak(self, ctx, pos):
        """只做一件事：把当前浮盈 R 送进峰顶状态机（_touch_pos_state）。

        为什么单独拆出来：镜像出场关闭后，原先唯一的调用点就没了；
        峰会冻结在平半前的值，前端那个「峰顶 → 当前价」回撤区会一直显示 0。
        """
        entry = float(pos.get("entry_price") or 0)
        stop = float(pos.get("stop_price") or 0)
        side = pos.get("side")
        if entry <= 0 or stop <= 0 or side not in ("long", "short"):
            return
        risk = abs(entry - stop)
        if risk <= 0 or not math.isfinite(risk):
            return
        px = ctx.price(pos.get("symbol"))
        if not px or not math.isfinite(px):
            return
        sign = 1.0 if side == "long" else -1.0
        cur_r = (px - entry) * sign / risk
        if not math.isfinite(cur_r):
            return
        self._touch_pos_state(pos, entry, risk, cur_r)

    def _full_target(self, ctx, pos):
        """② 兜底全平（2026-11-10 B 档）：已平半 且 现价R ≥ full_at_r(2.0) → 平剩余半仓。

        为什么需要它：③ 右侧止盈要等「一根够力道的反向K线」。如果价格慢慢阴跌回均线
        附近，却始终不出这样一根K线，仓位就会一直挂着 —— 2R 全平是那条保底线。
        回测出场归因：它占 12.2%（34 笔），是第四大出口。

        full_at_r = 0 即关闭。
        """
        thr = float(self._p("full_at_r", 0.0))
        if thr <= 0:
            return None
        if not int(pos.get("half_closed") or 0):
            return None
        entry = float(pos.get("entry_price") or 0)
        stop = float(pos.get("stop_price") or 0)
        side = pos.get("side")
        if entry <= 0 or stop <= 0 or side not in ("long", "short"):
            return None
        risk = abs(entry - stop)
        if risk <= 0 or not math.isfinite(risk):
            return None
        px = ctx.price(pos.get("symbol"))
        if not px or not math.isfinite(px):
            return None
        sign = 1.0 if side == "long" else -1.0
        cur_r = (px - entry) * sign / risk
        # 浮点精度：留 1e-9 容差，否则「恰好到 2.00R」会被漏掉
        if not math.isfinite(cur_r) or cur_r < thr - 1e-9:
            return None
        return (f"兜底{thr:.1f}R全平(半仓 现{cur_r:.2f}R)")

    async def _right_exit(self, ctx, pos):
        """③ 右侧止盈（2026-11-10 B 档新增）：平半之后，等反向信号出现再走完剩下半仓。

        为什么平半后不设固定目标：趋势跟踪的钱在后半段。1.2R 平一半先锁定「不亏」，
        剩下一半让它自己跑 —— 中途出现右侧信号就认输离场，跑到 2R 就兜底全平。

        条件（三条，必须落在同一根已收盘的 K 线上）：
            做多：① HA 阴线（HA收 < HA开）且 HA收 < ZLSMA50
                  ② 那根【原始 K 线】本身是反向关键K线（大阴线）
            做空：① HA 阳线（HA收 > HA开）且 HA收 > ZLSMA50
                  ② 那根【原始 K 线】本身是反向关键K线（大阳线）

        ⚠ right_exit_strict 必须开：实测严格 97 笔 / 57.7% / +19.88R，
          放宽（只判 HA 反向）98 笔 / 57.1%，胜率与盈亏比都更差 ——
          一根没力道的反向K线往往只是回踩，不是转向。
        ⚠ 只在 half_closed 之后判：平半前由 1.2R 规则和硬止损管，不抢跑。
        ⚠ 反向关键K线用的是**原始 K 线**，不是 HA —— HA 平滑后实体偏小，会误判力度。
        """
        if not bool(self._p("right_exit", False)):
            return None
        if not int(pos.get("half_closed") or 0):
            return None
        sym = pos.get("symbol")
        side = pos.get("side")
        if side not in ("long", "short"):
            return None
        pk = await self._bar_pack(ctx, sym)
        if pk is None:
            return None
        z_now, ha_close, ha_open, _dir_now, _dir_prev, _bp, bar_ts, ks, i, atr_n = pk
        if z_now is None or ha_close is None or ha_open is None:
            return None

        # 同一根 K 线只判一次（0.5 秒一轮，不判重会反复触发）
        sk = pos.get("id") or sym
        stt = _S15_RIGHT_STATE.get(sk)
        if stt is None:
            now_ts = time.time()
            for k in [k for k, v in _S15_RIGHT_STATE.items()
                      if now_ts - v.get("seen", now_ts) > 7 * 86400]:
                _S15_RIGHT_STATE.pop(k, None)
            stt = {"bar_ts": None, "seen": now_ts}
            _S15_RIGHT_STATE[sk] = stt
        stt["seen"] = time.time()

        rev = "short" if side == "long" else "long"     # 反向方向（原始K线要检的口径）
        if side == "long":
            ha_rev = ha_close < ha_open and ha_close < z_now
        else:
            ha_rev = ha_close > ha_open and ha_close > z_now
        strict = bool(self._p("right_exit_strict", True))

        # 判完就记：一根K线只看一次（strict 时连"没够力度"也算判过，避免同一根反复算）
        if stt["bar_ts"] == bar_ts:
            return None
        if not ha_rev:
            stt["bar_ts"] = bar_ts
            return None
        if strict:
            ok_kb = _is_key_bar(ks, i, atr_n, rev,
                                float(self._p("key_mult", 1.0)),
                                float(self._p("key_closepos", 0.66)),
                                float(self._p("key_bodyfrac", 0.5)))
            if not ok_kb:
                stt["bar_ts"] = bar_ts
                return None
        stt["bar_ts"] = bar_ts
        return ("右侧止盈(HA反向+穿ZLSMA"
                + ("+反向关键K线)" if strict else ")"))

    async def _bar_pack(self, ctx, sym):
        """出场用「bar 级」指标包，按 cur_bar 缓存（一根 K 线只拉一次数据）。

        返回 10 元组：
            (z_now, ha_close, ha_open, dir_now, dir_prev, bp_now,
             bar_ts, ks, i, atr_n)
        数据还没追到当前窗口时返回 None（不写缓存，下个 tick 再来）。

        ⚠ 为什么返回 ks / i / atr_n：右侧止盈要判「那根原始 K 线是不是反向关键K线」，
          必须拿到原始 K 线序列和它的 ATR(20)。回测（exit_speed_test.sim）里也是
          用 ks[j] 判的，两边口径必须一致。

        ⚠ 缓存是共享的：_right_exit 和 _mirror_exit 必须看到同一组数。
        """
        kline_iv = str(self.p.get("kline_interval", "15m"))
        ce_len = int(self._p("ce_len", 1))
        ce_mult = float(self._p("ce_mult", 2.0))
        z_len = int(self._p("zlsma_len", 50))
        use_close = bool(self._p("use_close", True))
        use_ha = bool(self._p("use_ha", True))

        bsec = 900
        try:
            if kline_iv.endswith("m"):
                bsec = int(kline_iv[:-1]) * 60
            elif kline_iv.endswith("h"):
                bsec = int(kline_iv[:-1]) * 3600
        except Exception:  # noqa: BLE001
            pass
        cur_bar = int(time.time() // bsec) * bsec
        ck = (sym, kline_iv)
        hit = _S15_ZL_CACHE.get(ck)
        # 11 项：(cur_bar, z_now, ha_close, bar_ts, dir_now, dir_prev, ha_open,
        #         bp_now, ks, i, atr_n)
        if hit and hit[0] == cur_bar and len(hit) >= 11:
            return (hit[1], hit[2], hit[6], hit[4], hit[5], hit[7],
                    hit[3], hit[8], hit[9], hit[10])

        need = max(ce_len, z_len * 2, 100) + 80
        ks = await ctx.klines(sym, kline_iv, need)
        if not ks or len(ks) < need:
            return None
        # ⚠ 币安在窗口刚翻的 0.x 秒内可能还没生成新 K 线。这时 len(ks)-2
        # 指向的不是「最后已收盘那根」，而是「上上一根」。一旦算错，
        # 整根 K 线的出场信号会被跳过，只能等下一窗赌运气
        # （2026-11-08 TAUSDT 实证：该出场拖了 30 分钟，多亏 0.38R）。
        # 数据没追到当前窗口就直接放弃本轮，下个 tick 再来，不写缓存。
        if int(ks[-1].get("ts", 0)) < cur_bar:
            return None
        ind = _ce_zlsma(ks, ce_len, ce_mult, z_len, use_close, use_ha)
        i = len(ks) - 2
        if i < 2 or ind["zlsma"][i] is None:
            return None
        atr_n = _atr_n(ks, 20)
        _S15_ZL_CACHE[ck] = (
            cur_bar, ind["zlsma"][i], ind["close"][i], ks[i].get("ts", i),
            ind["dir"][i], ind["dir"][i - 1], ind["open"][i],
            ind["bbw_pct"][i], ks, i, atr_n)
        h = _S15_ZL_CACHE[ck]
        return (h[1], h[2], h[6], h[4], h[5], h[7], h[3], h[8], h[9], h[10])

    def _quick_fail(self, ctx, pos):
        """快速失败出场：进场后 quickfail_hours 小时内峰顶一直没到 quickfail_min_r R → 平仓。

        为什么加（2026-11-08 浩哥批准，60 天 / 20 币回测）：
            基准（无此闸）        744 笔  +78.1R  均笔+0.105  回撤−24.4
            加本闸 4h/0.50        782 笔 +109.0R  均笔+0.139  回撤−21.4  ← 四条判据全过
            与「进场偏离闸」叠加   681 笔 +120.7R  均笔+0.177  回撤−16.4  ← 净R +59%

        动机是真实案例：2026-11-08 当天 4 笔止损，持仓只有 0.2 / 0.3 / 1.2 / 1.5 小时
        就反向、每笔亏满 −1R（合计 −627U）。进场后几根 K 线内就能看出「这股不对」，
        没必要非等它走到硬止损。

        与镜像出场的关系：实测两者只重叠 15% —— 本闸看「时间」（进去 4h 还在磨），
        镜像看「空间」（CE 翻 + 破均线）。只重叠 15%，所以两条都留。

        峰顶来源：走 _touch_pos_state（与镜像出场共用同一份内存态 + 库里的 peak_r），
        保证进程重启后不会从 0 重新算。已平过一半的仓位 peak 必然 ≥2R，本闸不会误伤。

        quickfail_hours = 0 即关闭本闸（回滚只需把配置设 0）。
        """
        hours = float(self._p("quickfail_hours", 0.0))
        if hours <= 0:
            return None
        min_r = float(self._p("quickfail_min_r", 0.5))
        opened = float(pos.get("opened_at") or 0)
        entry = float(pos.get("entry_price") or 0)
        stop = float(pos.get("stop_price") or 0)
        side = pos.get("side")
        if opened <= 0 or entry <= 0 or stop <= 0 or side not in ("long", "short"):
            return None
        elapsed_h = (time.time() - opened) / 3600.0
        if elapsed_h < hours:
            return None
        risk = abs(entry - stop)
        if risk <= 0 or not math.isfinite(risk):
            return None
        px = ctx.price(pos.get("symbol"))
        if not px or not math.isfinite(px):
            return None
        sign = 1.0 if side == "long" else -1.0
        cur_r = (px - entry) * sign / risk
        if not math.isfinite(cur_r):
            return None
        st = self._touch_pos_state(pos, entry, risk, cur_r)
        peak = float(st.get("peak") or 0.0)
        if peak >= min_r:
            return None
        return (f"快速失败({hours:.0f}h内峰顶{peak:.2f}R<{min_r:.1f}R "
                f"现{cur_r:.2f}R)")

    async def _half_take_profit(self, ctx, pos):
        """浮盈达 half_at_r（B 档 1.2R，从 yaml 读）且没平过一半 → 平掉 50%，剩一半放开跑。

        ⚠ 以下这张表是 **2026-08 的老记录（1.2R / 2.0R 时代）**，只作决策沿革参考，
          **不是当前参数**。B 档现役出场只有四条：1.2R 平半 + 右侧止盈 + 2.0R 兜底全平
          + 硬止损（时间止损/快速失败/移动止盈/镜像 全部已删）。
          最新口径见 strategies.yaml 和 docs/S15_逻辑文档_20261109.md。

        为什么要这一条（20 币 / 60 天 / 双档 ATR 回测，费率 0.02% maker 进 +
        0.05% taker 出）：

            出场规则                 笔数   净R     胜率   最大回撤  赚几段
            1.2R 全平（原线上）       498   -3.08   43.0%   -32.1    1/4
            1.2R 平一半              472  +31.99   43.6%   -24.0    2/4
            2.0R 平一半（当时实现）   477  +38.22   42.1%   -25.8    2/4
            完全放开止盈              478  +46.99   42.3%   -29.2    3/4

        （沿革：2.0R → 1.8R → 2026-11-10 B 档定 1.2R。1.2R 是「锁定不亏」的转折点，
          再往上抬，平半触发率暴跌、剩余半仓更容易被反抽打掉。）

        按 30 天趋势效率分类，2.0R 平一半在三种行情里都不比原规则差：
            单边趋势   +11.00R（原 +9.99）
            半趋势     +23.90R（原 +4.63）
            宽幅震荡    -6.97R（原 -10.84）

        返回 "HALF:..." 交给引擎识别，引擎调 executor.close_position(fraction=0.5)。
        """
        if int(pos.get("half_closed") or 0):
            return None
        thr = float(self._p("half_at_r", 2.0))
        if thr <= 0:                  # 0 = 关闭该出场，行为退回「全仓拿到均线破」
            return None
        entry = float(pos.get("entry_price") or 0)
        stop = float(pos.get("stop_price") or 0)
        if entry <= 0 or stop <= 0:
            return None
        risk = abs(entry - stop)
        if risk <= 0 or not math.isfinite(risk):
            return None
        px = ctx.price(pos.get("symbol"))
        if not px or not math.isfinite(px):
            return None
        sign = 1.0 if pos.get("side") == "long" else -1.0
        cur_r = (px - entry) * sign / risk
        if cur_r < thr:
            return None
        return f"HALF:{thr:.1f}R止盈(先平一半,剩一半放开跑)"

    def _touch_pos_state(self, pos, entry, risk, cur_r):
        """维护逐仓位的峰顶 R（内存态 + 节流落库）。

        为什么单独抽出来：2026-11-08 之前这段只存在于旧的「破均线回撤」出场里，
        换成镜像出场后旧方法不再执行 → peak_r 永远不更新 → 前端「峰顶 R」冻结、
        半仓回撤展示全部失真。现在只有这一个入口，不会再漏。

        注意：2026-11-08 浩哥定案删除旧出场后，这里不再维护 ma_break_cnt
        （「破均线 N/5」这个指标已随旧出场一起废弃）。
        """
        sym = pos.get("symbol")
        st_key = (sym, pos.get("side"), pos.get("opened_at"))
        st = _S15_POS_STATE.get(st_key)
        if st is None:
            # 顺手清理已平仓留下的孤儿状态（超过 7 天没被访问）
            now_ts = time.time()
            for k in [k for k, v in _S15_POS_STATE.items()
                      if now_ts - v.get("seen", now_ts) > 7 * 86400]:
                _S15_POS_STATE.pop(k, None)
            # peak 初始化三重兜底：库里存的 peak_r、mfe_pct 反推、当前浮盈。
            # mfe_pct 由引擎每轮刷新，比 peak_r 更新得勤；只取 peak_r 会偏低，
            # 偏低就会把「回撤门槛」算小 → 提前出场。
            _peak_db = float(pos.get("peak_r") or 0.0)
            _peak_mfe = abs(float(pos.get("mfe_pct") or 0.0)) / 100 * entry / risk
            _peak_init = max(_peak_db, _peak_mfe, max(0.0, cur_r))
            st = {"peak": _peak_init, "peak_db": _peak_db}
            _S15_POS_STATE[st_key] = st
        st["seen"] = time.time()
        if cur_r > st["peak"]:
            st["peak"] = cur_r
            # 峰顶落库（节流：每抬高 0.05R 才写一次）。
            # 不节流的话 check_exit 每 0.5 秒跑一次，8 个仓位就是每天几十万次写库。
            if cur_r - st.get("peak_db", 0.0) >= 0.05:
                st["peak_db"] = cur_r
                # 落库是旁路：写库失败（锁超时、库被占用）不能把出场判断一起拖死，
                # 否则一次写库异常就等于这一轮「不检查出场」，仓位裸奔。
                try:
                    db.update_position(pos["id"], peak_r=cur_r)
                except Exception as exc:  # noqa: BLE001
                    log.debug("peak_r 落库失败 %s: %s", pos.get("symbol"), exc)
        return st

    async def _mirror_exit(self, ctx, pos):
        """镜像出场（2026-11-08 浩哥定案，替代原来的「破均线 + 回撤」）。

        逻辑 = 开仓条件的完全镜像（两条，都必须落在同一根已收盘的 K 线上）：
            做多：① CE 方向由多翻空
                  ② HA 阴线（HA收 < HA开）且 HA收 < ZLSMA50
            做空：① CE 方向由空翻多
                  ② HA 阳线（HA收 > HA开）且 HA收 > ZLSMA50

        为什么出场不设布林带条件（2026-11-08 浩哥定）：
            入场的第③道用布林带宽度分位（挤压结束才进场），但出场不加。
            带 BBW 的镜像出场在无仓位上限制时看着更好（+78.1R vs +61.7R），
            但它把持仓从 26.5 根拖到 43.4 根 —— 在 max_concurrent_coins=6 的
            真实约束下，长期占仓直接挤掉后续信号，6 仓口径反而少赚 14R。
            出场要「快进快出把仓位腾出来」，布林带在这里是拖延器不是过滤器。

        半仓之后另有一条（在镜像之前先判）：
            平半价 2R 之上的新增浮盈 ≥ half_tp_r（默认 1R）→ 全平剩余半仓。
            也就是 2R 平一半 → 3R 全平。

        回测（20 币 / 60 天 / taker 0.05% / 线上口径 use_close=true / 6 仓约束）：
            本方案 镜像 + 2R平半 + 再赚1R全平：597 笔 +61.8R 均笔+0.104 回撤−16.4 持仓26.5根
            被替换的 破均线回撤 + 2R平半 + 再赚2R：608 笔 +57.3R 均笔+0.094 回撤−21.2 持仓24.9根

        为什么半仓看 cur_r 而不是 peak：这条规则只关心「从平半价又走了多远」，
        回撤多少由镜像条件管，不需要另外记峰顶。
        """
        # 2026-11-10 B 档：整条规则关闭（mirror_exit: false，放最前面省一次拉数据）
        if not bool(self._p("mirror_exit", True)):
            return None
        sym = pos.get("symbol")
        side = pos.get("side")
        entry = float(pos.get("entry_price") or 0)
        stop_px = float(pos.get("stop_price") or 0)
        if not sym or side not in ("long", "short") or entry <= 0 or stop_px <= 0:
            return None
        risk = abs(entry - stop_px)
        if risk <= 0 or not math.isfinite(risk):
            return None
        px = ctx.price(sym)
        if not px or not math.isfinite(px):
            return None
        sign = 1.0 if side == "long" else -1.0
        cur_r = (px - entry) * sign / risk
        if not math.isfinite(cur_r):
            return None

        # 峰顶状态维护（与旧出场分支共用，否则前端峰顶 R 会冻结）
        # 移动止盈要读 st["peak"]，所以必须接返回值
        st = self._touch_pos_state(pos, entry, risk, cur_r)

        # ── 半仓专属：再赚 half_tp_r 全平（默认 1R = 到 3R）──
        # ⚠ 浮点精度：2.8 - 1.8 = 0.9999999999999996，直接 >= 会漏掉「恰好到 3R」的单子。
        #   实测本地构造 peak2.8/现2.8R 时本该触发却返回 None。统一留 1e-9 容差。
        _EPS = 1e-9
        if int(pos.get("half_closed") or 0):
            base_r = float(self._p("half_at_r", 2.0))
            ht = float(self._p("half_tp_r", 1.0))
            if ht > 0 and (cur_r - base_r) >= ht - _EPS:
                return (f"半仓再赚{ht:.1f}R止盈(平半@{base_r:.1f}R "
                        f"现{cur_r:.2f}R)")

        # ── 半仓专属：移动止盈（2026-11-09 浩哥定，与 3R 全平并存）──
        # 语义：峰值 ≥ trail_arm 后，从峰值回撤 trail_dd 个 R → 全平剩余半仓。
        # 是「相对回撤」不是「绝对落袋」：峰值 3R 平在 1.8R，峰值 5R 平在 3.8R。
        # trail_arm=1.5 < 平半线 1.8 → 平半那一刻就已在 arm 状态，等效于「平半即武装」。
        # 放在 3R 全平之后：若同一 tick 两者都满足，先按 3R 全平（回测口径一致）。
        #   3R 全平的条件是 cur ≥ 1.8+1.0 = 2.8R，所以移动止盈主要在 2.8R 以下兜底。
        # 实测（20币/60天/cap5%/12h强平）：3R全平+移动 共触发 103 笔，
        #   trail 52 笔 + half_tp 51 笔，合计 +94.2R（关掉 3R 全平只有 +87.5R）。
        # 15m K 线会跳空穿过 trail_dd，实际回吐中位 1.20R（设定 1.2R）。
        # 置 0 = 关闭。
        if int(pos.get("half_closed") or 0):
            t_arm = float(self._p("trail_arm", 0.0))
            t_dd = float(self._p("trail_dd", 0.0))
            if t_arm > 0 and t_dd > 0:
                peak_r = float(st.get("peak") or 0.0)
                if peak_r >= t_arm - _EPS and (peak_r - cur_r) >= t_dd - _EPS:
                    return (f"移动止盈(峰值{peak_r:.2f}R 回吐{peak_r - cur_r:.2f}R"
                            f"≥{t_dd:.1f}R 现{cur_r:.2f}R)")

        # ── 指标：按 bar 边界取，一根 K 线没收盘指标不会变 ──
        # 2026-11-10：抽成 _bar_pack，与右侧止盈共用同一份缓存（口径必须一致）
        pk = await self._bar_pack(ctx, sym)
        if pk is None:
            return None
        (z_now, ha_close, ha_open, dir_now, dir_prev, bp_now,
         bar_ts, _ks, _i, _atr) = pk
        if z_now is None or ha_close is None or ha_open is None:
            return None

        # ── 同一根 K 线只判一次，避免 0.5 秒一轮反复触发 ──
        sk = pos.get("id") or sym
        st = _S15_MIRROR_STATE.get(sk)
        if st is None:
            # 顺手清理已平仓留下的孤儿状态（超过 7 天没被访问），
            # 与 _S15_POS_STATE 同一套策略：这个 dict 只增不减会一直占内存。
            now_ts = time.time()
            for k in [k for k, v in _S15_MIRROR_STATE.items()
                      if now_ts - v.get("seen", now_ts) > 7 * 86400]:
                _S15_MIRROR_STATE.pop(k, None)
            st = {"bar_ts": None, "seen": now_ts}
            _S15_MIRROR_STATE[sk] = st
        st["seen"] = time.time()
        if st["bar_ts"] == bar_ts:
            return None
        st["bar_ts"] = bar_ts

        # ① CE 方向翻转
        flipped = (dir_prev == 1 and dir_now == -1) if side == "long" \
            else (dir_prev == -1 and dir_now == 1)
        if not flipped:
            return None
        # ② HA 反向关键K线 + 站在 ZLSMA 错误一侧
        if side == "long":
            ha_ok = ha_close < ha_open and ha_close < z_now
        else:
            ha_ok = ha_close > ha_open and ha_close > z_now
        if not ha_ok:
            return None
        # ③ 布林带宽度分位门槛（2026-11-09 浩哥选 B：只加这一条，其余不动）
        #
        # 为什么只在镜像加：CE 是 ATR 通道，低波动时价格来回穿，方向翻转会非常
        # 频繁 —— 那是噪音不是翻转。BBW 分位只衡量「价格分散程度」，正是
        # 「这次翻转够不够格」的过滤器。入场早就在用它（bbw_pct_min: 70）。
        #
        # 实测（20 币 / 60 天 / cap5% / 偏离闸 0.5% / 8 仓并发 / 当前出场）：
        #   不带 BBW      620 笔  +50.6R  回撤 −22.5   镜像出场 327 笔（占 43%）
        #   ≥50           603 笔  +61.8R  −23.4        172 笔
        #   ≥60           602 笔  +61.3R  −22.2        140 笔
        #   ≥70           598 笔  +65.5R  −20.5        105 笔  ← 峰
        #   ≥80           594 笔  +56.5R  −21.9         73 笔
        #   ≥90           586 笔  +55.3R  −23.8         38 笔
        # 六档里带 BBW 的五档全好于不带，不是靠一个点撑起来的；70 是峰且回撤最小。
        #
        # 不带 BBW 时镜像均笔只 −0.08R —— 等于在震荡里来回被扫。
        # 过滤后那些单子改走时间止损和移动止盈，反而赚钱。
        # 置 0 = 关闭本闸（回滚只需把 yaml 的 mirror_bbw_min 改 0）。
        m_bbw = float(self._p("mirror_bbw_min", 0.0))
        if m_bbw > 0:
            if bp_now is None or bp_now < m_bbw:
                return None
        return (f"镜像出场(CE翻转+HA{'阴' if side == 'long' else '阳'}线"
                f"{'跌破' if side == 'long' else '站上'}ZLSMA 现{cur_r:.2f}R)")

# 只注册线上真正跑的策略。
# 2026-11-06 自检清理：S02/S06/S11/S14/S16 五个策略早已停用
# （yaml 里 S11/S14 是 enabled:false，S02/S06/S16 根本不在 yaml 里，
#  11-04 日志有「策略实例 xxx 已删除」），却依旧占着本文件六成篇幅。
# 代码已移到 backend/strategies_legacy.py 存档；要恢复的话在那边接线并在
# strategies.yaml 里配 enabled: true。
REGISTRY = {cls.sid: cls for cls in [S15CeZlsma]}
