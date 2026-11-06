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
    take_profit: float = 0.0
    trail_dist: float = 0.0   # absolute price distance for chandelier trailing
    max_hold_sec: int = 86400
    time_exit_at: int = 0
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
    return dict(zlsma=zlsma, atr=atr, dir=dirs, close=close, open=openp,
                long_stop=long_stop, short_stop=short_stop,
                low_raw=low_raw, high_raw=high_raw)


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
# 键: (symbol, interval)  值: (bar_ts, zlsma, atr, ready)
_S15_ZL_CACHE = {}

# ── S15 逐仓位状态（峰顶浮盈 / 连续破均线计数）──────────────────────────
# 键: (symbol, side, opened_at) -> {"peak": float, "bar_ts": any, "cnt": int}
# 为什么不放数据库：这两个值只影响出场时机，丢了最多晚一根 K 线出场，
# 不值得为它加一次写库（positions 每次写都要加锁，已有 database is locked 隐患）。
_S15_POS_STATE = {}


class S15CeZlsma(Base):
    sid = "S15_CE_ZLSMA"
    FACTORS = []                 # 不需要任何 Clawby 因子，不消耗额度
    DEFAULT_INTERVAL = 900       # 15m K线，每 15 分钟检查一次
    META = {
        "name": "CE+ZLSMA 趋势跟随", "type": "趋势跟随 / 短线", "direction": "双向",
        "logic": "Chandelier Exit 用 ATR 画一条只朝有利方向移动的止损线来判定大方向，"
                 "方向翻转那一刻就是信号；ZLSMA(50) 零延迟均线用来确认趋势站没站稳；"
                 "布林带宽度分位衡量波动是否扩张，只在市场真的在动的时候才出手。"
                 "入场用真实收盘价算钱，用 HA 合成价判方向。",
        "entry": "三道闸全过才开仓：① CE 方向翻转 ② HA 出现关键K线（阳线站上 ZLSMA / 阴线跌破）"
                 " ③ 布林带宽度近 100 根分位 > 70%",
        "exit": "四条并列，谁先满足谁全平：① 时间止损 16 小时 ② 止损 = 20 根阶段低点"
                " ③ 止盈 = 1.2R ④ 破 ZLSMA 且连续 2 根且从峰顶回撤 ≥ 0.8R",
        "factors": "15m K线（自算 CE(1×2) / ZLSMA50 / ATR / 布林带宽度分位）",
        "risk": "震荡市 CE 会频繁翻转，所以必须有 BBW 分位这道环境闸——"
                "去掉它回测从 +36.66R 掉到 −85.20R。费率口径敏感："
                "maker 0.02% 赚，市价 0.05% 亏。",
        "name_en": "CE + ZLSMA Trend Follow",
        "logic_en": "Chandelier Exit ratchets an ATR stop that defines the regime; a flip is the signal. "
                    "ZLSMA(50) confirms price is on the right side; Bollinger-band width percentile "
                    "gates entries to expanding-volatility regimes. Directions use HA prices, "
                    "money uses real closes.",
        "entry_en": "All three gates: (1) CE direction flip (2) HTF key bar (bull close above ZLSMA / "
                    "bear close below) (3) BBW percentile over last 100 bars > 70%",
        "exit_en": "Four parallel exits, first hit wins: 16h time stop / stop at 20-bar swing "
                   "extreme / take profit 1.2R / close beyond ZLSMA with 2-bar confirmation "
                   "and 0.8R drawdown from peak.",
        "factors_en": "15m klines (self-computed CE 1x2 / ZLSMA50 / ATR / Bollinger width percentile)",
        "risk_en": "CE flips constantly in chop, hence the BBW regime gate — removing it drops "
                   "backtest from +36.66R to -85.20R. Fee-sensitive: maker 0.02% profits, "
                   "taker 0.05% loses.",
    }

    def _p(self, key, default):
        try:
            return type(default)(self.p.get(key, default))
        except (TypeError, ValueError):
            return default

    async def evaluate(self, ctx) -> list:
        """入场：CE 方向翻转（事件）+ 均线同侧关键K线 + ATR 门槛 + 布林带宽度分位门槛。

        设计要点（都是回测结论，见 docs/S15_回测结论_20261105.md）：
          · CE 方向翻转是「事件」不是「状态」—— 只在翻那一刻出信号，否则每根
            都满足条件会刷出 7 倍的假箭头。
          · ATR > 2.5% 是生死线：止损距离决定手续费占 R 的比例，ATR 太小
            时手续费是毛利的 2.7 倍，数学上必亏。
          · 布林带宽度分位 > 70% 用来排除震荡市 —— 布林带宽度只衡量「价格分散
            程度」，是唯一没有被证伪的趋势过滤器（ADX、ADX上升、效率比全部失效）。
          · 止损取 20 根阶段低点（用原始 K 线真实价格，不用 HA 合成值）。
        """
        ce_len = int(self._p("ce_len", 1))
        ce_mult = float(self._p("ce_mult", 2.0))
        z_len = int(self._p("zlsma_len", 50))
        rr = float(self._p("rr_ratio", 1.2))
        use_close = bool(self._p("use_close", True))
        use_ha = bool(self._p("use_ha", True))
        swing_lb = int(self._p("swing_lookback", 20))
        min_atr_pct = float(self._p("min_atr_pct", 2.5))
        bbw_pct_min = float(self._p("bbw_pct_min", 70.0))
        max_risk_pct = float(self._p("max_risk_pct", 30.0))
        # 成本口径：默认按 maker 0.02% / 零滑点，和回测一致。
        # ⚠ 这是「假设挂单能成交」的记账口径，不代表线上真能挂到单。
        #    真实限价挂单能力是执行层待办（观察期后单独做）。
        fee_rate = float(self._p("fee_rate", 0.0002))
        slip_pct = float(self._p("slip_pct", 0.0))
        kline_iv = str(self.p.get("kline_interval", "15m"))

        need = max(ce_len, z_len * 2, swing_lb, 100) + 80
        out = []

        for sym in self.syms(ctx):
            try:
                if ctx.has_position(sym, self.sid):
                    continue
                ks = await ctx.klines(sym, kline_iv, need)
                if not ks or len(ks) < need:
                    continue

                ind = _ce_zlsma(ks, ce_len, ce_mult, z_len, use_close, use_ha)
                bbw = _bollinger_width(ind["close"], 20, 2.0)
                bbw_pct = _pct_rank(bbw, 100)

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

                # ⑤ 止损 = 20 根阶段低点/高点（原始 K 线真实价格）
                if d_now == 1:
                    stop = min(ind["low_raw"][i - swing_lb + 1:i + 1])
                    risk = px - stop
                    side = "long"
                    tp = px + risk * rr
                else:
                    stop = max(ind["high_raw"][i - swing_lb + 1:i + 1])
                    risk = stop - px
                    side = "short"
                    tp = px - risk * rr

                if risk <= 0 or not math.isfinite(risk):
                    continue
                # 止损距离过宽直接放弃：R 太大 → 仓位被压得极小，且可能有数据问题
                if risk / px * 100.0 > max_risk_pct:
                    continue
                if not (math.isfinite(stop) and math.isfinite(tp) and stop > 0 and tp > 0):
                    continue

                _GATE_STATE[sym]["ok"] = True
                _GATE_STATE[sym]["why"] = "已开仓"
                out.append(Signal(
                    strategy=self.sid, symbol=sym, side=side,
                    reason=(f"CE{'多' if side == 'long' else '空'}翻向+"
                            f"{'阳' if side == 'long' else '阴'}线"
                            f"{'站上' if side == 'long' else '跌破'}ZLSMA "
                            f"ATR={atr_pct:.2f}%"
                            + (f" BBW分位={bp:.0f}%" if bp is not None else "")),
                    stop_price=round(stop, 8),
                    # ⚠ 2026-11-06 改：原来这里写死 take_profit=1.2R 价位。
                    # 引擎的 take_profit 检查排在 check_exit 之前，一到 1.2R
                    # 就整仓全平 → 永远走不到「2R 平一半」，后面的趋势也全丢。
                    # 回测里 1.2R 全平只有 -3.08R，放开后是 +38.22R。
                    # tp 变量仍参与上面的合法性校验，只是不再作为平仓价落到库里。
                    take_profit=0.0,
                    max_hold_sec=self.max_hold,
                    fresh=0, strength=abs(c - z) / atr,
                    fee_rate=fee_rate, slip_pct=slip_pct,
                ))
            except Exception as exc:  # noqa: BLE001
                log.warning("S15 %s 计算失败: %s", sym, exc)
        return out

    async def check_exit(self, ctx, pos):
        """趋势反转出场：趋势掉头且已有浮盈时提前落袋。

        为什么加「必须已有浮盈」这道门槛 —— 回测数据（20 币 / 20 天 /
        组合级 8 仓上限，费率 0.05% 双边 + 滑点）对比 6 种出场规则：

            规则            笔数   胜率   净总R   费用R   盈亏比
            ATR+R止盈(现状)  197   35.0%  -34.94    9.6    0.58
            一穿就走        1803   28.7%  -95.05   97.4    0.57
            连续2根确认      1373   35.6%  -72.08   72.5    0.61
            均线转向才走     1059   34.6%  -25.20   55.9    0.65
            浮盈后趋势出场     392   61.2%  -10.49   18.9    1.21   ← 本实现
            pine 原版       2345   22.5% -1053.63 1011.6   0.49

        单纯「收盘价穿越均线就平仓」在震荡市疯狂进出：1717 次触发占
        全部出场的 95%，光手续费就吃掉 97R，把原本 -35R 的结果恶化到
        -95R。加浮盈门槛后交易次数降到 392，胜率从 35% 提到 61%，
        盈亏比 0.58 → 1.21，是唯一没有因手续费失血的规则。

        关键设计：亏损时不用趋势出场，把回踩空间留给 硬止损（前 20 根阶段低点）。
        这是趋势跟踪的标准用法 —— 亏损单让止损说话，盈利单才谈保护
        利润。反过来（亏损就跑）会在大趋势启动前的正常回踩中被打掉。

        流量：check_exit 每 0.5 秒被调用一次，8 个仓位就是 16 次/秒。
        这里按「bar 边界」缓存 ZLSMA 值 —— 一根 K 线没收盘，指标就不
        会变，只有跨过 bar 边界才真正去拉数据。1h 周期下 8 个币一天
        约 192 次请求 ≈ 3.8MB；不做这个缓存会是 553MB/天。
        """
        try:
            half = await self._half_take_profit(ctx, pos)
            if half:
                return half
            return await self._trend_reversal_exit(ctx, pos)
        except Exception as exc:  # noqa: BLE001
            log.warning("S15 check_exit %s 失败: %s", pos.get("symbol"), exc)
            return None

    async def _half_take_profit(self, ctx, pos):
        """浮盈达 half_at_r（默认 2.0R）且没平过一半 → 平掉 50%，剩一半放开跑。

        为什么要这一条（20 币 / 60 天 / 双档 ATR 回测，费率 0.02% maker 进 +
        0.05% taker 出）：

            出场规则                 笔数   净R     胜率   最大回撤  赚几段
            1.2R 全平（原线上）       498   -3.08   43.0%   -32.1    1/4
            1.2R 平一半              472  +31.99   43.6%   -24.0    2/4
            2.0R 平一半（本实现）     477  +38.22   42.1%   -25.8    2/4
            完全放开止盈              478  +46.99   42.3%   -29.2    3/4

        选 2.0R 平一半而不是完全放开：收益只少 8.8R，但回撤小 3.4R，
        且宽幅震荡行情亏得更少（-6.97R vs -10.13R）—— 浩哥明确要的是
        「2R 平一半，后面放开跑」。

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

    async def _trend_reversal_exit(self, ctx, pos):
        """出场③：收盘跌破 ZLSMA 且从峰顶回撤 ≥ trail_r，连续 N 根确认后全平。

        为什么必须和「从峰顶回撤」绑定，而不是「一穿均线就走」：
        震荡市里价格会反复穿越均线，一穿就走会被来回割 —— 回测里一次性
        穿均线就平有 1717 次触发、占全部出场 95%，光手续费就吃掉 97R，
        把 -35R 的结果恶化到 -95R。要求「已赚到的利润回吐超过 trail_r」
        才平，才能滤掉这部分假突破。

        为什么还要连续 N 根确认：震荡市里的假突破常常是单根插针，
        连续 2 根收盘都在均线另一侧，才算真掉头。回测里 1 根→2 根，
        胜率 38.0%→38.9%，净 R -0.88→-0.35。

        注意「回撤」是相对持仓期最高浮盈（峰顶）算的，不是浮盈门槛 ——
        这是浩哥明确纠正过的：以前做成「浮盈不足 X R 就不平」方向反了。

        流量：check_exit 每 0.5 秒调用一次，8 仓位 = 16 次/秒。
        按 bar 边界缓存 ZLSMA（15m 8 币一天约 768 次请求 ≈ 15MB）；
        不做缓存会是 553MB/天。
        """
        sym = pos["symbol"]
        side = pos["side"]
        entry = float(pos.get("entry_price") or 0)
        if entry <= 0:
            return None

        trail_r = float(self._p("trail_r", 0.8))
        if trail_r <= 0:              # 0 = 关闭该出场，只剩止损/止盈/超时
            return None
        ma_confirm = max(1, int(self._p("ma_confirm_bars", 2)))
        # ── 分段回撤（K5 方案）──
        # 峰顶还没到 trail_pct_max_r（默认 2R）时，改用「回撤浮盈的 trail_pct」：
        #   峰顶 0.6R 回撤 50% → 在 +0.3R 走，永远留住一半，不会倒亏。
        # 满 2R 之后退回固定 trail_r（0.8R），让大赢家继续放开跑。
        # trail_pct = 0 时整段关闭，行为与旧版完全一致。
        trail_pct = float(self._p("trail_pct", 0.0))
        trail_pct_max_r = float(self._p("trail_pct_max_r", 2.0))
        trail_pct_cap = float(self._p("trail_pct_cap", 0.0))
        ma_bars_lt = int(self._p("ma_bars_lt", ma_confirm))
        ce_len = int(self._p("ce_len", 1))
        ce_mult = float(self._p("ce_mult", 2.0))
        z_len = int(self._p("zlsma_len", 50))
        use_close = bool(self._p("use_close", True))
        use_ha = bool(self._p("use_ha", True))
        kline_iv = str(self.p.get("kline_interval", "15m"))

        # 风险基数：入场时确定的止损距离（止损 = 阶段低点）
        risk = abs(entry - float(pos.get("stop_price") or 0))
        if risk <= 0 or not math.isfinite(risk):
            return None
        sign = 1.0 if side == "long" else -1.0

        px = ctx.price(sym)
        if not px or not math.isfinite(px):
            return None
        cur_r = (px - entry) * sign / risk

        # ── 逐仓位状态 ──
        st_key = (sym, side, pos.get("opened_at"))
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
            st = {"peak": _peak_init,
                  "peak_db": _peak_db,
                  "bar_ts": None,
                  "cnt": int(pos.get("ma_break_cnt") or 0)}   # 重启后从库里接着数
            _S15_POS_STATE[st_key] = st
        st["seen"] = time.time()
        if cur_r > st["peak"]:
            st["peak"] = cur_r
            # 峰顶落库（节流：每抬高 0.05R 才写一次）。
            # 不节流的话 check_exit 每 0.5 秒跑一次，8 个仓位就是每天几十万次写库。
            if cur_r - st.get("peak_db", 0.0) >= 0.05:
                st["peak_db"] = cur_r
                db.update_position(pos["id"], peak_r=cur_r)

        # ── bar 边界缓存 ──
        bar_secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
        bsec = bar_secs.get(kline_iv, 900)
        cur_bar = int(time.time() // bsec) * bsec
        ck = (sym, kline_iv)
        hit = _S15_ZL_CACHE.get(ck)
        if hit and hit[0] == cur_bar:
            z_now, ha_close, bar_ts = hit[1], hit[4], hit[5]
        else:
            need = max(ce_len, z_len * 2, 100) + 80
            ks = await ctx.klines(sym, kline_iv, need)
            if not ks or len(ks) < need:
                return None
            ind = _ce_zlsma(ks, ce_len, ce_mult, z_len, use_close, use_ha)
            i = len(ks) - 2          # 最后一根已收完的 K 线
            if i < 2 or ind["zlsma"][i] is None:
                return None
            z_now = ind["zlsma"][i]
            ha_close = ind["close"][i]
            bar_ts = ks[i].get("ts", i)
            _S15_ZL_CACHE[ck] = (cur_bar, z_now, ind["atr"][i], True,
                                 ha_close, bar_ts)

        if z_now is None or ha_close is None:
            return None

        # ── 破均线判断：用 HA 收盘价，和回测口径一致 ──
        broke = (ha_close < z_now) if side == "long" else (ha_close > z_now)
        if st["bar_ts"] is None:
            # 进程刚起来（或该仓位第一次被访问）：内存里没有上一根的信息。
            # 只对齐 bar_ts，不重复计数 —— 否则库里 cnt=1 的仓位会立刻被算成 2，
            # 凭空打出一张提前出场的单子。真正的更新留给下一根 K 线。
            st["bar_ts"] = bar_ts
        elif st["bar_ts"] != bar_ts:      # 跨到新的一根 K 线才更新计数
            st["bar_ts"] = bar_ts
            new_cnt = st["cnt"] + 1 if broke else 0
            if new_cnt != st["cnt"]:
                st["cnt"] = new_cnt
                # 落库：重启后能接着数，卡片上也能显示 N/2
                db.update_position(pos["id"], ma_break_cnt=new_cnt)

        peak = st["peak"]        # 半仓判定和③都要用，先取出来

        # ── 半仓专属出场（2R 已平掉一半之后）──
        # 三个条件并列，达到任一就全平剩下的那一半。
        # 默认只开 ⓒ（浩哥 2026-11-06 的明确要求：③不变，再加一条「再满 2R 直接走」）；
        # ⓐⓑ 做成参数，回测里对比过再决定要不要开。
        if int(pos.get("half_closed") or 0):
            base_r = float(self._p("half_at_r", 2.0))
            # 关键：门槛按「平半之后新赚的那一段」算，不是从开仓算。
            # 从开仓算的话峰顶 3.0R 回撤 50%=1.5R，出场落在 1.5R，
            # 比平半价 2.0R 还低 —— 等于把已经落袋的一半又吐回去。
            extra_peak = peak - base_r
            extra_cur = cur_r - base_r
            # ⓐ 半仓额外浮盈回撤 50%
            hp = float(self._p("half_dd_pct", 0.0))
            if hp > 0 and extra_peak > 0.05 and (peak - cur_r) >= extra_peak * hp:
                return (f"半仓回撤{hp*100:.0f}%(峰值{peak:.2f}R "
                        f"额外{extra_peak:.2f}R 回撤{peak-cur_r:.2f}R)")
            # ⓑ 半仓峰顶回撤固定 0.8R（不看均线，比③更松）
            # 半仓是独立的一段：它的峰顶就是平半后的新高，回撤 0.8R 就是
            # 回撤 0.8R，与已落袋的那一半无关（那半在平仓时已经结算完毕）。
            # 半仓只赚了 0.5R 时这条不会触发 —— 本来就该如此，不是缺陷。
            hr = float(self._p("half_dd_r", 0.0))
            if hr > 0 and (peak - cur_r) >= hr:
                return f"半仓回撤{hr:.2f}R(峰值{peak:.2f}R)"
            # ⓒ 半仓又赚满 2R → 全平落袋
            ht = float(self._p("half_tp_r", 0.0))
            if ht > 0 and extra_cur >= ht:
                return f"半仓再赚{ht:.2f}R止盈(峰值{peak:.2f}R 现{cur_r:.2f}R)"

        # ── 出场判定：峰顶不足 2R 走「比例回撤」，满 2R 走「固定 0.8R」──
        # peak <= 0 说明内存态还没重建（进程刚起来、仓位刚被访问），
        # 此时按比例算出来的门槛是 0 → 任何回撤都会触发，等于提前乱砍仓。
        # 这种情况必须退回固定 trail_r。
        use_pct = trail_pct > 0 and 0.05 < peak < trail_pct_max_r
        need_bars = ma_bars_lt if use_pct else ma_confirm
        if use_pct:
            need_dd = peak * trail_pct
            if trail_pct_cap > 0:
                need_dd = min(need_dd, trail_pct_cap)
        else:
            need_dd = trail_r

        if st["cnt"] < need_bars:
            return None
        drawdown = peak - cur_r
        if drawdown < need_dd:
            return None

        mode_txt = (f"浮盈{trail_pct*100:.0f}%={need_dd:.2f}R" if use_pct
                    else f"固定{need_dd:.2f}R")
        return (f"破均线回撤出场(连续{st['cnt']}根确认, "
                f"峰顶{peak:.2f}R 回撤{drawdown:.2f}R/{mode_txt})")



# 只注册线上真正跑的策略。
# 2026-11-06 自检清理：S02/S06/S11/S14/S16 五个策略早已停用
# （yaml 里 S11/S14 是 enabled:false，S02/S06/S16 根本不在 yaml 里，
#  11-04 日志有「策略实例 xxx 已删除」），却依旧占着本文件六成篇幅。
# 代码已移到 backend/strategies_legacy.py 存档；要恢复的话在那边接线并在
# strategies.yaml 里配 enabled: true。
REGISTRY = {cls.sid: cls for cls in [S15CeZlsma]}
