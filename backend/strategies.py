"""Active strategies: S02 / S06 / S11 / S14 (trimmed 2026-07-18 per backtest; the removed ones live in backup/2026-07-18-pre-trim/).

Each strategy implements evaluate(ctx) -> [Signal]; optionally check_exit(ctx,
position) -> reason|None for strategy-specific exits. All entries/holds respect
the <=24h intraday constraint via max_hold_sec.
"""
import logging
import statistics
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


def _z(series, value=None, window=None):
    s = list(series)[-(window or len(series)):]
    if len(s) < 8:
        return 0.0
    mean, stdev = statistics.fmean(s), statistics.pstdev(s)
    if stdev == 0:
        return 0.0
    return ((value if value is not None else s[-1]) - mean) / stdev


class Ctx:
    """Per-tick context handed to strategies: factors, klines/ATR cache, positions."""

    def __init__(self, universe):
        self.universe = universe
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
        """Per-strategy symbol list, or the global monitored universe if unset."""
        return self.symbols or ctx.universe

    async def evaluate(self, ctx) -> list:
        return []

    async def check_exit(self, ctx, pos):
        return None


def strategy_meta():
    """sid -> META for the dashboard strategy panel."""
    return {sid: cls.META for sid, cls in REGISTRY.items()}


# ── S02 爆仓瀑布接针 ────────────────────────────────────────────────────────

class S02LiqRebound(Base):
    sid = "S02_LIQ_REBOUND"
    FACTORS = ["liq_agg", "liq_orders"]   # 依赖因子(策略页展示/启用校验)
    DEFAULT_INTERVAL = "5m"   # design scan cadence (backtests use this)
    META = {
        "name": "爆仓瀑布接针", "type": "均值回归", "direction": "只多",
        "logic": "密集多头爆仓是清算引擎的被迫卖出而非新增看空共识,爆仓峰值后价格常超调,瀑布衰减确认后快速回填。",
        "entry": "1h多头爆仓 ≥ 7日均值6倍 + 30分钟急跌 + 爆仓单流10分钟内衰减≥70% + 买盘深度回补(bid/ask≥1.1) → 市价开多",
        "exit": "反弹至跌幅0.5回撤位止盈;新低0.6×ATR止损;最长持仓 4h",
        "factors": "liq_agg · liq_orders · 5m K线 · 订单簿深度 · ATR",
        "risk": "接飞刀策略——瀑布仍在放大时绝不入场;交易所级黑天鹅直接熔断不接",
        "name_en": "Liq-Cascade Rebound",
        "logic_en": "Dense long liquidations are forced selling by the liquidation engine, not fresh bearish consensus; price overshoots at the cascade peak and refills fast once the cascade decays.",
        "entry_en": "1h long liqs >= 6x 7d avg + 30m sharp drop + liq order flow decaying >=70% within 10m + bid depth refilled (bid/ask >= 1.1) -> market long",
        "exit_en": "TP at 0.5 retrace of the drop; SL 0.6xATR under the low; max hold 4h",
        "factors_en": "liq_agg / liq_orders / 5m klines / orderbook depth / ATR",
        "risk_en": "Knife-catching: never enter while the cascade is still expanding; exchange-level black swans are halted, not caught",
    }

    async def evaluate(self, ctx):
        out = []
        for sym in self.syms(ctx):
            if ctx.has_position(sym):
                continue
            liq = ctx.factor("liq_agg", sym)
            orders = ctx.factor("liq_orders", sym)
            if not liq or liq.get("long_mult", 0) < self.p["liq_spike_mult"]:
                continue
            ks = await ctx.klines(sym, "5m", 12)
            atr = await ctx.atr(sym)
            price = ctx.price(sym)
            if len(ks) < 7 or not atr or not price:
                continue
            drop = ks[-7]["close"] - price          # ~30m move
            if drop < self.p["drop_atr"] * atr / 2:  # 5m-scale: half the 1h-ATR bar
                continue
            if orders and orders["n_prev_20m"] > 0:
                decay = 1 - orders["n_10m"] / max(orders["n_prev_20m"] / 2, 1)
                if decay * 100 < self.p["decay_pct"]:
                    continue  # cascade still accelerating — don't catch yet
            if await ctx.depth_ratio(sym) < self.p["bid_ask_min"]:
                continue
            out.append(Signal(self.sid, sym, "long",
                              f"多头爆仓{liq['long_mult']:.1f}x均值+瀑布衰减+买盘回补",
                              stop_price=price - self.p["stop_atr"] * atr,
                              take_profit=price + drop * self.p["tp_retrace"],
                              max_hold_sec=self.max_hold))
        return out


# ── S06 CVD 背离反转 ───────────────────────────────────────────────────────

class S06CvdFade(Base):
    sid = "S06_CVD_FADE"
    FACTORS = ["cvd", "ob_wall"]   # 依赖因子(策略页展示/启用校验)
    DEFAULT_INTERVAL = "15m"   # design scan cadence (backtests use this)
    META = {
        "name": "CVD背离反转", "type": "微观结构反转", "direction": "双向",
        "logic": "价格靠稀薄挂单打出新高但主动买量(CVD)不创新高 = 推手是挂单撤退而非真实买盘,背离后大概率回摆。",
        "entry": "价格创4h新高但CVD落后前高≥15% + 卖压比≥1.3 + 上方1%内有大额卖墙 → 开空(镜像开多);仓位减半",
        "exit": "回归1h VWAP止盈;卖墙被吃掉止损(逻辑失效);最长持仓 6h",
        "factors": "cvd · ob_wall · 订单簿深度 · 1h K线",
        "risk": "背离可以持续背离——单笔风险减半,靠高胜率小止损盈利",
        "name_en": "CVD Divergence Fade",
        "logic_en": "New price highs on thin books without new CVD highs mean the move is ask-side retreat, not real buying; the divergence usually snaps back.",
        "entry_en": "Price makes a 4h high but CVD lags >=15% + sell pressure >=1.3 + a large ask wall within 1% above -> short (mirrored long); half size",
        "exit_en": "TP at 1h VWAP; SL when the wall gets eaten (thesis invalid); max hold 6h",
        "factors_en": "cvd / ob_wall / orderbook depth / 1h klines",
        "risk_en": "Divergence can keep diverging: half size per trade, profit via high win-rate small stops",
    }

    async def evaluate(self, ctx):
        out = []
        for sym in self.syms(ctx):
            if ctx.has_position(sym):
                continue
            cvd = ctx.factor("cvd", sym)
            wall = ctx.factor("ob_wall", sym)
            if not cvd or len(cvd.get("series", [])) < 10:
                continue
            ks = await ctx.klines(sym, "1h", 10)
            atr = await ctx.atr(sym)
            price = ctx.price(sym)
            if len(ks) < 5 or not atr or not price:
                continue
            price_hh = price >= max(k["high"] for k in ks[:-1])
            price_ll = price <= min(k["low"] for k in ks[:-1])
            s = cvd["series"]
            rng = (max(s) - min(s)) or 1
            gap_hh = (max(s[:-1]) - s[-1]) / rng * 100
            gap_ll = (s[-1] - min(s[:-1])) / rng * 100
            depth = await ctx.depth_ratio(sym)
            if price_hh and gap_hh >= self.p["div_gap_pct"] and depth <= 1 / self.p["ask_bid_min"]:
                if self._has_wall(wall, price, "ask"):
                    out.append(Signal(self.sid, sym, "short",
                                      f"价格新高但CVD落后{gap_hh:.0f}%+卖压+上方卖墙",
                                      stop_price=price + 1.0 * atr,
                                      take_profit=price - 1.2 * atr,
                                      size_mult=self.p["size_mult"],
                                      max_hold_sec=self.max_hold))
            elif price_ll and gap_ll >= self.p["div_gap_pct"] and depth >= self.p["ask_bid_min"]:
                if self._has_wall(wall, price, "bid"):
                    out.append(Signal(self.sid, sym, "long",
                                      f"价格新低但CVD抬升{gap_ll:.0f}%+买撑+下方买墙",
                                      stop_price=price - 1.0 * atr,
                                      take_profit=price + 1.2 * atr,
                                      size_mult=self.p["size_mult"],
                                      max_hold_sec=self.max_hold))
        return out

    def _has_wall(self, wall, price, side):
        if not wall:
            return True  # wall data unavailable -> don't block, size is halved anyway
        orders = wall.get("orders") or []
        rng = self.p["wall_range_pct"] / 100
        for o in orders if isinstance(orders, list) else []:
            try:
                p = float(o.get("price") or 0)
                o_side = str(o.get("side") or o.get("type") or "").lower()
            except (TypeError, ValueError):
                continue
            if side == "ask" and p > price and (p - price) / price <= rng and "sell" in o_side or \
               side == "bid" and p < price and (price - p) / price <= rng and "buy" in o_side:
                return True
        return not orders  # empty list -> permissive


# ── S11 高频接针(暴跌/暴涨清算捕捉)─────────────────────────────────────────

class S11CrashScalp(Base):
    sid = "S11_CRASH_SCALP"
    FACTORS = ["liq_agg"]   # 依赖因子(策略页展示/启用校验)
    DEFAULT_INTERVAL = 1   # design scan cadence (backtests use this)
    META = {
        "name": "高频接针(暴跌/暴涨)", "type": "高频均值回归", "direction": "双向",
        "logic": "暴跌/暴涨瞬间大量杠杆被强平,清算引擎的被迫单制造价格超调。用 WebSocket 逐笔价格做秒级速度监控,"
                 "在刀势减缓(企稳)的一刻反向接针,吃清算后的均值回摆——不追趋势,只做快进快出的反弹/回落。",
        "entry": "观察窗口(默认30s)内跌幅≥1.2% 且 近3s刀势减缓(近端幅度≤窗口幅度×0.4)→ 市价接多;"
                 "暴涨镜像接空。爆仓倍数因子可用时作为增强确认。同币有冷却期防止一波反复进。",
        "exit": "反弹/回落 0.6% 自动止盈(核心);继续恶化 0.8% 止损;浮盈过半且秒级动能反转 → 提前止盈;最长持仓 2h。",
        "factors": "WebSocket 逐笔价格(秒级速度)· liq_agg 爆仓倍数(确认)· 订单簿深度",
        "risk": "接飞刀本质高风险——严格小止损+秒级监控;黑天鹅单边时靠 0.8% 止损和全局熔断兜底,不加仓不扛单。",
        "name_en": "HF Crash Scalp",
        "logic_en": "Crashes force-liquidate leverage and the forced flow overshoots price. Watch second-level tick velocity and catch the reversal the moment the knife decelerates: quick in, quick out.",
        "entry_en": "Window move >= threshold and the last 3s decelerated (<= decel x window pace) -> market entry against the move; per-coin cooldown",
        "exit_en": "TP +0.6% on the bounce (core); SL 0.8%; early exit when second-level momentum flips past half target; max hold 2h",
        "factors_en": "WebSocket tick velocity / liq_agg confirmation / orderbook depth",
        "risk_en": "Inherently risky knife-catching: strict small stops + second-level monitoring; black swans covered by the 0.8% stop and global halt; never add to losers",
    }

    def _cooldown_key(self, sym):
        return f"s11_cd:{sym}"

    async def evaluate(self, ctx):
        from . import ws
        out = []
        win = self.p["window_sec"]
        recent = self.p["recent_sec"]
        now = time.time()
        for sym in self.syms(ctx):
            if ctx.has_position(sym):
                continue
            # same-coin cooldown
            last = float(db.get_meta(self._cooldown_key(sym), "0") or 0)
            if now - last < self.p["cooldown_sec"]:
                continue
            if ws.buffer_span(sym) < self.p["min_buffer_sec"]:
                continue  # not enough tick history yet
            move = ws.change_pct(sym, win)          # window move (neg = crash)
            near = ws.change_pct(sym, recent)        # last-3s move
            price = ctx.price(sym)
            if move is None or near is None or not price:
                continue

            liq = ctx.factor("liq_agg", sym) or {}
            drop = self.p["drop_pct"]
            decel = self.p["decel_ratio"]

            # ── crash -> catch long ──────────────────────────────────────────
            if move <= -drop:
                # deceleration: recent move no longer as steep as the window slide
                if near < move * decel:      # e.g. near=-0.9 vs move*0.4=-0.48 -> still falling
                    continue
                if self.p.get("require_liq") and liq.get("long_mult", 0) < self.p["liq_confirm_mult"]:
                    continue
                boost = liq.get("long_mult", 0) >= self.p["liq_confirm_mult"]
                out.append(Signal(
                    self.sid, sym, "long",
                    f"{win}s暴跌{move:.2f}%+近{recent}s企稳{near:.2f}%"
                    + (f"+爆仓{liq.get('long_mult',0):.1f}x确认" if boost else ""),
                    stop_price=price * (1 - self.p["stop_pct"] / 100),
                    take_profit=price * (1 + self.p["tp_pct"] / 100),
                    max_hold_sec=self.max_hold))
                db.set_meta(self._cooldown_key(sym), int(now))

            # ── spike -> catch short ─────────────────────────────────────────
            elif self.p.get("spike_short") and move >= drop:
                if near > move * decel:      # still ripping up
                    continue
                if self.p.get("require_liq") and liq.get("short_mult", 0) < self.p["liq_confirm_mult"]:
                    continue
                boost = liq.get("short_mult", 0) >= self.p["liq_confirm_mult"]
                out.append(Signal(
                    self.sid, sym, "short",
                    f"{win}s暴涨{move:.2f}%+近{recent}s见顶{near:.2f}%"
                    + (f"+爆仓{liq.get('short_mult',0):.1f}x确认" if boost else ""),
                    stop_price=price * (1 + self.p["stop_pct"] / 100),
                    take_profit=price * (1 - self.p["tp_pct"] / 100),
                    max_hold_sec=self.max_hold))
                db.set_meta(self._cooldown_key(sym), int(now))
        return out

    async def check_exit(self, ctx, pos):
        """Early take-profit: once past half the target and second-level momentum
        reverses, bank it (the bounce is exhausting)."""
        from . import ws
        price = ctx.price(pos["symbol"])
        if not price:
            return None
        sign = 1 if pos["side"] == "long" else -1
        gain_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100 * sign
        if gain_pct < self.p["tp_pct"] * self.p["exhaust_tp_frac"]:
            return None
        near = ws.change_pct(pos["symbol"], self.p["recent_sec"])
        if near is None:
            return None
        # long: reversal = recent move turns negative; short: turns positive
        if (pos["side"] == "long" and near < -0.05) or (pos["side"] == "short" and near > 0.05):
            return f"反弹动能衰竭提前止盈(浮盈{gain_pct:.2f}%)"
        return None



# ── S14 VWAP 偏离回归 ────────────────────────────────────────────────────────

class S14VwapRevert(Base):
    sid = "S14_VWAP_REVERT"
    FACTORS = ["taker_flow"]   # 依赖因子(策略页展示/启用校验)
    DEFAULT_INTERVAL = 60
    META = {
        "name": "VWAP偏离回归", "type": "高频均值回归", "direction": "双向",
        "logic": "价格对 60 分钟成交量加权均价(VWAP)的过度偏离,在推动性主动买卖流衰竭时大概率回归;"
                 "偏离越大、taker 流越衰竭,回归赔率越好。",
        "entry": "|price/VWAP60 − 1| ≥ dev_pct 且 taker 买卖比确认推动力衰竭(超买时 taker≤阈值,超卖镜像)→ 反向开仓。",
        "exit": "回归至偏离的 exit_frac 处止盈;继续偏离 0.8×dev 止损;最长持仓 45 分钟。",
        "factors": "1m K线(VWAP60)· taker_flow",
        "risk": "趋势日的持续偏离是主要风险——taker 衰竭过滤 + 偏离基础上的固定止损,严禁摊平。",
        "name_en": "VWAP Deviation Revert",
        "logic_en": "Excess deviation from the 60m volume-weighted average price mean-reverts once the driving taker flow exhausts; bigger deviation with drier flow = better odds.",
        "entry_en": "|price/VWAP60 - 1| >= dev threshold with taker flow confirming exhaustion -> enter against the deviation",
        "exit_en": "TP at exit_frac of the deviation back to VWAP; SL at 0.8x further deviation; max hold 45m",
        "factors_en": "1m klines (VWAP60) / taker_flow",
        "risk_en": "Trend days keep deviating: taker exhaustion filter + hard stop beyond the deviation, never average down",
    }

    def _cd_key(self, sym):
        return f"s14_cd:{sym}"

    async def evaluate(self, ctx):
        out = []
        now = time.time()
        for sym in self.syms(ctx):
            if ctx.has_position(sym):
                continue
            if now - float(db.get_meta(self._cd_key(sym), "0") or 0) < self.p["cooldown_sec"]:
                continue
            price = ctx.price(sym)
            if not price:
                continue
            ks = await ctx.klines(sym, "1m", 61)
            if len(ks) < 45:
                continue
            pv = sum((k["high"] + k["low"] + k["close"]) / 3 * k["volume"] for k in ks[:-1])
            v = sum(k["volume"] for k in ks[:-1])
            if not v:
                continue
            vwap = pv / v
            dev = (price / vwap - 1) * 100
            if abs(dev) < self.p["dev_pct"]:
                continue
            tk = ctx.factor("taker_flow", sym)
            conf = self.p["taker_conf"]
            if dev > 0:                      # overbought -> need buy exhaustion
                if tk and tk["latest"] > conf:
                    continue
                side = "short"
                tp = vwap * (1 + dev / 100 * self.p["exit_frac"])
                sp = price * (1 + abs(dev) / 100 * 0.8)
            else:                            # oversold -> need sell exhaustion
                if tk and tk["latest"] < 1 / conf:
                    continue
                side = "long"
                tp = vwap * (1 + dev / 100 * self.p["exit_frac"])
                sp = price * (1 - abs(dev) / 100 * 0.8)
            out.append(Signal(self.sid, sym, side,
                              f"偏离VWAP60 {dev:+.2f}%且taker={tk['latest'] if tk else '?'}衰竭,回归",
                              stop_price=sp, take_profit=tp,
                              max_hold_sec=self.max_hold))
            db.set_meta(self._cd_key(sym), int(now))
        return out



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


def _ce_zlsma(ks, ce_len=22, ce_mult=3.0, z_len=32, use_close=True):
    """返回 {zlsma, atr, dir, close, open} 序列。"""
    close = [float(k["close"]) for k in ks]
    openp = [float(k["open"]) for k in ks]
    high = [float(k["high"]) for k in ks]
    low = [float(k["low"]) for k in ks]
    n = len(close)

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
                long_stop=long_stop, short_stop=short_stop)


_LAST_BAR = {}   # (sid, symbol) -> 已评估过的K线时间戳，防止高频扫描重复出信号


class S15CeZlsma(Base):
    sid = "S15_CE_ZLSMA"
    FACTORS = []                 # 不需要任何 Clawby 因子，不消耗额度
    DEFAULT_INTERVAL = 1800      # 1h K线，每 30 分钟检查一次
    META = {
        "name": "CE+ZLSMA 趋势跟随", "type": "趋势跟随 / 中线", "direction": "双向",
        "logic": "Chandelier Exit 用 ATR 画一条只朝有利方向移动的止损线来判定大方向；"
                 "ZLSMA 是零延迟线性回归均线，用来等回调结束的那根关键K线。"
                 "大方向对 + 关键K线出现 = 顺势进场，用 ATR 倍数止损、固定 R 倍数止盈。",
        "entry": "做多 = CE 处于多头状态 且 上一根为收盘站上 ZLSMA 的阳线；做空镜像。",
        "exit": "止损 = 入场价 - ATR×倍数；止盈 = 止损距离 × R 倍数；另有最长持仓时间兜底。",
        "factors": "1h K线（自算 ZLSMA32 / ATR22 / Chandelier 棘轮）",
        "risk": "震荡市会被反复止损；1h 以下周期手续费占比过高，回测显示 5m/15m 均为负期望。",
        "name_en": "CE + ZLSMA Trend Follow",
        "logic_en": "Chandelier Exit draws a one-way ratcheting ATR stop that defines the regime; "
                    "ZLSMA (zero-lag linreg average) times the pullback-end key bar. "
                    "Right regime + key bar = trend entry, ATR-multiple stop and fixed-R target.",
        "entry_en": "Long = CE in long state AND last closed bar is a bullish bar closing above ZLSMA; short mirrored.",
        "exit_en": "SL = entry - ATR x mult; TP = stop distance x R; max-hold as backstop.",
        "factors_en": "1h klines (self-computed ZLSMA32 / ATR22 / ratcheting Chandelier)",
        "risk_en": "Choppy markets stop you out repeatedly; below 1h the fee drag makes expectancy negative.",
    }

    def _p(self, key, default):
        try:
            return type(default)(self.p.get(key, default))
        except (TypeError, ValueError):
            return default

    async def evaluate(self, ctx) -> list:
        ce_len = int(self._p("ce_len", 22))
        ce_mult = float(self._p("ce_mult", 3.0))
        z_len = int(self._p("zlsma_len", 32))
        stop_mult = float(self._p("stop_atr_mult", 3.0))
        rr = float(self._p("rr_ratio", 2.0))
        use_close = bool(self._p("use_close", True))
        kline_iv = str(self.p.get("kline_interval", "1h"))

        need = max(ce_len, z_len * 2) + 80
        out = []

        for sym in self.syms(ctx):
            try:
                if ctx.has_position(sym, self.sid):
                    continue
                ks = await ctx.klines(sym, kline_iv, need)
                if not ks or len(ks) < need:
                    continue

                ind = _ce_zlsma(ks, ce_len, ce_mult, z_len, use_close)
                i = len(ks) - 2          # 最后一根已收完的 K 线
                if i < 1:
                    continue
                # 同一根已收完的K线只评估一次，避免 2 分钟扫描把同一信号记多遍
                bar_key = (self.sid, sym)
                bar_ts = ks[i].get("ts", i)
                if _LAST_BAR.get(bar_key) == bar_ts:
                    continue
                _LAST_BAR[bar_key] = bar_ts

                z, atr, dirs = ind["zlsma"][i], ind["atr"][i], ind["dir"][i]
                dirs_arr = ind["dir"]
                if z is None or atr is None or atr <= 0:
                    continue
                z = float(z)
                atr = float(atr)
                dirs = int(dirs or 1)

                c, o = ind["close"][i], ind["open"][i]
                key_long = c > z and c > o
                key_short = c < z and c < o
                px = ctx.price(sym) or c
                if px <= 0:
                    continue
                # 信号新鲜度：距上一次 CE 方向翻转已过几根 K 线（0 = 本根刚翻）
                j = i
                while j > 0 and int(dirs_arr[j] or 1) == int(dirs_arr[j - 1] or 1):
                    j -= 1
                fresh = i - j
                # 信号强度：收盘价偏离 ZLSMA 的幅度（以 ATR 为单位），越大趋势越强
                strength = abs(c - z) / atr if atr > 0 else 0.0

                if dirs == 1 and key_long:
                    stop = px - atr * stop_mult
                    if stop <= 0:
                        continue
                    out.append(Signal(
                        strategy=self.sid, symbol=sym, side="long",
                        reason=f"CE多头+阳线站上ZLSMA(RSI无关) ATR={atr:.4g}",
                        stop_price=round(stop, 8),
                        take_profit=round(px + (px - stop) * rr, 8),
                        max_hold_sec=self.max_hold,
                        fresh=fresh, strength=strength,
                    ))
                elif dirs == -1 and key_short:
                    stop = px + atr * stop_mult
                    out.append(Signal(
                        strategy=self.sid, symbol=sym, side="short",
                        reason=f"CE空头+阴线跌破ZLSMA ATR={atr:.4g}",
                        stop_price=round(stop, 8),
                        take_profit=round(max(px - (stop - px) * rr, 1e-8), 8),
                        max_hold_sec=self.max_hold,
                        fresh=fresh, strength=strength,
                    ))
            except Exception as exc:  # noqa: BLE001
                log.warning("S15 %s 计算失败: %s", sym, exc)
        return out




# ── S16 UT Bot + STC + Hull Suite ───────────────────────────────────────────
# 三指标共振：UT Bot 给方向信号、STC 确认超卖/超买反转、Hull 船体均线做趋势过滤。
# 出场：收盘价穿回 Hull 均线即离场（回测证明优于固定 R 止盈：+25.95% vs -8.19%）。

_HULL_CACHE = {}   # (symbol, bar_ts) -> (hull_value, close_value)


class S16UtStcHull(Base):
    sid = "S16_UT_STC_HULL"
    FACTORS = []                 # 纯 K 线，零 Clawby 额度
    DEFAULT_INTERVAL = 900       # 15m K 线
    META = {
        "name": "UT+STC+船体均线", "type": "三指标共振 / 中线", "direction": "双向",
        "logic": "UT Bot(ATR追踪止损)给出方向翻转信号，STC 确认动能从超卖/超买区反转，"
                 "Hull 船体均线（低延迟）确认趋势方向；三者同向且出现关键K线时进场，"
                 "一直持有到收盘价重新穿过 Hull 均线。",
        "entry": "做多 = UT 买入信号 + STC<25 且上升 + 收盘在 Hull 之上 + 阳线；做空镜像。",
        "exit": "止损 = 关键K线低点/高点；止盈 = 收盘价反向穿越 Hull 船体均线。",
        "factors": "15m K线（自算 UT/STC/HMA）",
        "risk": "5 分钟周期回测为强负期望（手续费吞噬）；震荡市三条件互相冲突，信号稀少。",
        "name_en": "UT + STC + Hull Suite",
        "logic_en": "UT Bot flips direction, STC confirms momentum leaving oversold/overbought, "
                    "Hull MA confirms trend; enter on a key bar when all three agree, hold until "
                    "close crosses back through the Hull MA.",
        "entry_en": "Long = UT buy + STC<25 rising + close>Hull + bullish bar; short mirrored.",
        "exit_en": "SL at the key bar extreme; exit when close crosses back through Hull MA.",
        "factors_en": "15m klines (self-computed UT / STC / HMA)",
        "risk_en": "Strongly negative on 5m (fee drag); choppy markets make the three conditions conflict.",
    }

    def _q(self, key, default):
        try:
            return type(default)(self.p.get(key, default))
        except (TypeError, ValueError):
            return default

    def _need(self, hull_len):
        return max(260, hull_len * 3)

    def _calc(self, ks, hull_len, stc_fast, stc_slow):
        from .indicators_extra import stc, hull_hma
        close = [float(k["close"]) for k in ks]
        return stc(close, stc_fast, stc_slow, 10), hull_hma(close, hull_len), close

    async def evaluate(self, ctx) -> list:
        from .indicators_extra import ut_bot
        kv = self._q("ut_key_value", 2.0)
        atrp = self._q("ut_atr_period", 6)
        stc_fast = int(self._q("stc_fast", 27))
        stc_slow = int(self._q("stc_slow", 80))
        hull_len = int(self._q("hull_len", 55))
        stc_lo = float(self._q("stc_lo", 25.0))
        stc_hi = float(self._q("stc_hi", 75.0))
        kline_iv = str(self.p.get("kline_interval", "15m"))
        need = self._need(hull_len)

        out = []
        for sym in self.syms(ctx):
            try:
                if ctx.has_position(sym, self.sid):
                    continue
                ks = await ctx.klines(sym, kline_iv, need)
                if not ks or len(ks) < need:
                    continue

                close = [float(k["close"]) for k in ks]
                high = [float(k["high"]) for k in ks]
                low = [float(k["low"]) for k in ks]
                openp = [float(k["open"]) for k in ks]

                buy, sell, _ = ut_bot(close, high, low, kv, atrp)
                st, hu, _ = self._calc(ks, hull_len, stc_fast, stc_slow)

                i = len(ks) - 2      # 最后一根已收完
                if i < 2 or st[i] is None or st[i-1] is None or hu[i] is None:
                    continue

                # 同一根K线只评估一次
                bar_key = (self.sid, sym)
                bar_ts = ks[i].get("ts", i)
                if _LAST_BAR.get(bar_key) == bar_ts:
                    continue
                _LAST_BAR[bar_key] = bar_ts

                c, o = close[i], openp[i]
                stc_up = st[i] < stc_lo and st[i] > st[i-1]
                stc_dn = st[i] > stc_hi and st[i] < st[i-1]
                px = ctx.price(sym) or c
                if px <= 0:
                    continue

                if buy[i] and stc_up and c > hu[i] and c > o:
                    stop = low[i]
                    if stop <= 0 or stop >= px:
                        continue
                    out.append(Signal(
                        strategy=self.sid, symbol=sym, side="long",
                        reason=f"UT买+STC{st[i]:.0f}回升+站上Hull STC={st[i]:.1f}",
                        stop_price=round(stop, 8), take_profit=0.0,
                        max_hold_sec=self.max_hold,
                    ))
                elif sell[i] and stc_dn and c < hu[i] and c < o:
                    stop = high[i]
                    if stop <= px:
                        continue
                    out.append(Signal(
                        strategy=self.sid, symbol=sym, side="short",
                        reason=f"UT卖+STC{st[i]:.0f}回落+跌破Hull",
                        stop_price=round(stop, 8), take_profit=0.0,
                        max_hold_sec=self.max_hold,
                    ))
            except Exception as exc:  # noqa: BLE001
                log.warning("S16 %s 计算失败: %s", sym, exc)
        return out

    async def check_exit(self, ctx, pos):
        """收盘价穿回 Hull 均线即离场（回测最优出场）。"""
        try:
            hull_len = int(self.p.get("hull_len", 55))
            stc_fast = int(self.p.get("stc_fast", 27))
            stc_slow = int(self.p.get("stc_slow", 80))
            kline_iv = str(self.p.get("kline_interval", "15m"))
            sym = pos["symbol"]
            ks = await ctx.klines(sym, kline_iv, self._need(hull_len))
            if not ks or len(ks) < 60:
                return None
            i = len(ks) - 2
            bar_ts = ks[i].get("ts", i)
            ck = (sym, bar_ts, hull_len)
            hit = _HULL_CACHE.get(ck)
            if hit is None:
                _, hu, close = self._calc(ks, hull_len, stc_fast, stc_slow)
                if hu[i] is None:
                    return None
                hit = (hu[i], close[i])
                if len(_HULL_CACHE) > 500:
                    _HULL_CACHE.clear()
                _HULL_CACHE[ck] = hit
            hull_v, close_v = hit
            if pos["side"] == "long" and close_v < hull_v:
                return "收盘跌破船体均线"
            if pos["side"] == "short" and close_v > hull_v:
                return "收盘站上船体均线"
        except Exception as exc:  # noqa: BLE001
            log.warning("S16 check_exit %s 失败: %s", pos.get("symbol"), exc)
        return None


REGISTRY = {cls.sid: cls for cls in [
    S02LiqRebound, S06CvdFade, S11CrashScalp, S14VwapRevert, S15CeZlsma, S16UtStcHull,
]}
