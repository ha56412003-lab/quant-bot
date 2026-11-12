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
    stop_src: str = ""        # 止损来源诊断：swing / ce / key / dual_ce / dual_swing
    # 反手标记（2026-11-11 晚 浩哥定）：本时段方向与在管仓位相反时置 True。
    # 引擎收到后必须「先平旧仓、再开新仓」——risk.allow_entry 有「同币已有仓位
    # 禁止对锁/加仓」的硬闸，顺序反了会被拒。
    reverse: bool = False
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

    def open_side(self, symbol, strategy=None):
        """该币在管仓位的方向：'long' / 'short' / None（无仓）。

        反手要用 —— has_position 只告诉你「有没有」，反向开仓需要知道「是哪边」。
        """
        ps = db.open_positions(strategy=strategy, symbol=symbol)
        if not ps:
            return None
        return str(ps[0].get("side") or "").lower() or None


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

# 进场闸门诊断：每根 15m K 线评估完把每道闸的即时状态存在这里。
# 只写内存、不额外拉 K 线（扫描本来就在算 CE/HA/BBW，算完原本是丢掉的）。
# 页面用它回答「今天是没机会，还是门槛卡死了」。
_GATE_STATE = {}   # symbol -> {ts, dir, ce, ha, bbw, bbw_thr, bbw_exempt, atr_pct, ok, why}


# ── S15 趋势反转出场的 bar 边界缓存 ──────────────────────────────────────
# check_exit 每 0.5 秒被调用一次（engine.position_loop），8 个仓位就是
# 16 次/秒。若每次都去拉 1h K 线，一天约 553MB —— 而且 99.9% 的调用
# 拿到的都是同一根未收盘 bar，算出来的 ZLSMA 完全一样。
# 这里只缓存「当前 bar 的 ZLSMA / ATR 值」，跨过 bar 边界才重新拉取。
# 键: (symbol, interval)  值: (cur_bar, zlsma, ha_close, bar_ts, dir_now, dir_prev, ha_open)
_S15_ZL_CACHE = {}

# ── S15 右侧止盈的逐仓位去重状态 ──────────────────────────────────────
# 键: pos_id（或 symbol）-> {"bar_ts": 已判定过的 K 线时间戳}
# 目的：同一根 15m K 线只判一次右侧止盈，避免 0.5 秒一轮反复触发。
# （2026-11-10 B 档：原镜像出场那一份状态表随方法一起删除，只剩这一份。）
_S15_RIGHT_STATE = {}

# ── S15 逐仓位状态（峰顶浮盈 / 连续破均线计数）──────────────────────────
# 键: (symbol, side, opened_at) -> {"peak": float, "bar_ts": any, "cnt": int}
# 为什么不放数据库：这两个值只影响出场时机，丢了最多晚一根 K 线出场，
# 不值得为它加一次写库（positions 每次写都要加锁，已有 database is locked 隐患）。
_S15_POS_STATE = {}

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


# ══════════════════════════════════════════════════════════════════════════
# ZLSMA 实体穿越 → 多/空时段（leg）序列    【2026-11-11 晚 浩哥定档】
# ══════════════════════════════════════════════════════════════════════════
def _zlsma_legs(ks, ind, max_wait=3):
    """用「ZLSMA50 实体穿越 + 下一根确认」切出多头段 / 空头段。

    浩哥 2026-11-11 晚定档的定义：
      · 穿越根 = ZLSMA 落在这根 K 线**实体内部**（min(开,收) ≤ ZLSMA ≤ max(开,收)）
        连续多根都在实体内时，取「实体最长」的那一根
      · 确认根 = 穿越根之后，第一根「实体完全在反面」的 K 线
        转空：max(开,收) < ZLSMA      转多：min(开,收) > ZLSMA
      · 逐根往后等确认，但最多等 max_wait 根（超时视为震荡，本次作废）
      · 中途回到原来那一侧 = 假穿越，作废，等下一次
      · 时段 = 从确认根开始，持续到下一次穿越确认为止

    为什么不用 CE 的 dir：
      CE 的 dir 本来就是「状态」（+1/-1 两个值，天然表示一段持续）。
      旧代码把它降级成瞬时事件（只在翻那一根判断），结果翻转那根不合格，
      整个时段就全废 —— UNI 2026-11-11 13:15 翻空那根不是关键K线，
      13:15~18:15 四小时的空头行情一笔都没吃到。
      正确用法：dir 划定战场（时段），关键 K 线决定开枪时点。

    返回 (leg_dir, leg_ts, side, starts)：
      leg_dir[i] = +1 / -1  第 i 根所属时段方向；0 = 尚未确认（不在任何时段内）
      leg_ts[i]  = 该时段确认根的时间戳（用于「本时段是否已入场」的去重）
      side[i]    = +1 实体全在均线上 / -1 全在下 / 0 均线在实体内 / None 数据不足
      starts     = [(确认根下标, 方向, 穿越根下标), ...]
    """
    n = len(ks)
    z = ind["zlsma"]
    ho, hc = ind["open"], ind["close"]

    # ① 逐根算实体相对均线的侧位
    side: list = [None] * n
    for i in range(n):
        if z[i] is None or ho[i] is None or hc[i] is None:
            continue
        lo = min(ho[i], hc[i])
        hi = max(ho[i], hc[i])
        if lo > z[i]:
            side[i] = 1
        elif hi < z[i]:
            side[i] = -1
        else:
            side[i] = 0

    # ② 找穿越根 + 等确认
    starts = []
    i = 1
    while i < n:
        if side[i] == 0 and side[i - 1] in (1, -1):
            j = i
            while j < n and side[j] == 0:
                j += 1
            # [i, j) 是连续「均线在实体内」的区间 → 取实体最长的那根做穿越根
            cross = i
            best = -1.0
            for k in range(i, min(j, n)):
                body = abs(hc[k] - ho[k])
                if body > best:
                    best, cross = body, k
            prev = side[i - 1]
            conf = None
            waited = 0
            for m in range(j, n):
                s = side[m]
                if s is None or s == 0:
                    waited += 1
                    if waited > max_wait:
                        break
                    continue
                if s == prev:          # 回到原侧 = 假穿越
                    break
                conf = (m, s, cross)
                break
            if conf:
                starts.append(conf)
            i = j
            continue
        i += 1

    # ③ 按时段起点填充
    leg_dir = [0] * n
    leg_ts = [None] * n
    for idx, (ci, d, _xi) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        ts = ks[ci].get("ts", ci)
        for t in range(ci, end):
            leg_dir[t] = d
            leg_ts[t] = ts
    return leg_dir, leg_ts, side, starts


_LEG_ENTERED = {}      # (sid, sym) -> 已入场的时段起点 ts；保证「一个时段只进一次」
_LEG_MAX_WAIT = 3      # 穿越之后最多等几根确认


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
        "entry": "六道闸全过才开仓：① 时段确认 —— ZLSMA50 实体穿越切多空时段（CE 翻转不再是触发器）"
                 " ② HA 形态 —— HA 收盘站在均线同一侧、方向一致"
                 " ③ 关键K线 —— 原始 K 线实体 ≥ 1.0×ATR20，收盘落在极值 1/3 区，实体占比 ≥ 50%"
                 " ④ BBW 分位 —— 布林带宽度近 100 根分位 ≥ 70（波动扩张才做）"
                 " ⑤ 止损距离 —— 含 0.2×ATR20 缓冲后必须落在 0.60% ~ 5.00%"
                 " ⑥ 趋势效率 —— 进场前 48 根单边度 ≥ 0.30（单边才做）",
        "exit": "五个出口按顺序判，先命中先执行：① 硬止损 = 8 根摆动 / CE 通道 / 关键K线端点外扩 0.2×ATR20，"
                "三条取最近；距离 < 0.50% 时只保留 CE 通道（⑤c 两档）"
                " ② 峰顶到 1.5R → 止损抬到入场价（保本）"
                " ③ 到 1.2R 平一半（先锁定不亏）"
                " ④ 平半后到 2.5R 全平（兜底）"
                " ⑤ 平半后出现反向关键K线且 HA 收穿 ZLSMA50 → 平剩余（右侧止盈）。"
                "已删除：时间止损、快速失败、移动止盈、镜像出场、收盘价止损、偏离闸。",
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
        "entry_en": "All six gates: (1) phase = ZLSMA50 body cross defines the long/short phase "
                    "(a CE flip is no longer the trigger) (2) HA close on the same side of ZLSMA50 "
                    "(3) the raw candle is a key bar (body >= 1.0x ATR20, close in the extreme "
                    "third, body >= 50% of range) (4) BBW percentile over the last 100 bars >= 70% "
                    "(5) stop distance including the 0.2x ATR20 pad within 0.60% ~ 5.00% "
                    "(6) trend efficiency of the previous 48 bars >= 0.30.",
        "exit_en": "Five exits, first hit wins: (1) hard stop = nearest of 8-bar swing / CE channel / "
                   "key-bar extreme padded by 0.2x ATR20; below 0.50% only the CE channel is kept "
                   "(2) peak reaches 1.5R -> stop moves to entry (breakeven) (3) take half at 1.2R "
                   "(4) after the half, close the rest at 2.5R as a backstop (5) after the half, a "
                   "reverse key bar whose HA close crosses back through ZLSMA50 closes the rest. "
                   "Removed: time stop, quick-fail, trailing stop, mirror exit, close-price stop, "
                   "deviation gate.",
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

    def _stop_ref(self, ks, i, ind, side, atr_n, swing_lb, stop_key, dual_stop_min_pct):
        """止损价 = 三条并列取近 + 两档修正 —— **唯一实现**。

        开仓判定和光闸面板第⑤道闸都调这一个方法，保证「面板显示的距离 = 真实开仓用的距离」。
        返回 (止损价, 来源, 距离%)；来源取值 swing / ce / key / dual_ce / dual_swing。

        三条取近（多头取最高、空头取最低 = 离入场价最近的那条）：
          ① 左侧 swing_lb 根摆动低/高点（原始 K 线，含进场那根）
          ② CE 通道（ce_mult × ATR(1)，棘轮只进不退）
          ③ 关键K线的低点（多）/ 高点（空）
        两档（dual_stop_min_pct > 0 时）：三条取近后距离 < 阈值 → 这一档只用 ②CE 通道。
          ★不看来源：摆动/CE/关键K线哪条最近都算 —— 与回测母本同口径。
        """
        px = float(ks[i]["close"])
        want_long = (side == "long")
        lo_win = ind["low_raw"][i - swing_lb + 1:i + 1]
        hi_win = ind["high_raw"][i - swing_lb + 1:i + 1]
        stop = min(lo_win) if want_long else max(hi_win)
        stop_src = "swing"
        _ce_arr = ind.get("long_stop" if want_long else "short_stop")
        _ce_stop = _ce_arr[i] if _ce_arr is not None and i < len(_ce_arr) else None
        if _ce_stop is not None:
            try:
                _ce_stop = float(_ce_stop)
            except (TypeError, ValueError):
                _ce_stop = None
        if _ce_stop is not None and math.isfinite(_ce_stop):
            if (want_long and _ce_stop > stop) or ((not want_long) and _ce_stop < stop):
                stop = _ce_stop
                stop_src = "ce"
        if stop_key:
            _kb = float(ks[i]["low"] if want_long else ks[i]["high"])
            # 2026-11-12 浩哥批准：关键K线端点再往外扩 stop_key_atr_buf × ATR(20)。
            #   目的：躲开「影线扫一下就走」的插针 —— 只加 0.1~0.2 个 ATR，
            #   不改变「止损=关键K线端点」这个本质，只是给它留一层气垫。
            #   回测（双样本 + 80币拆两半分组验证全部同向）：
            #     0.1 ATR → 40币 净值 +18.4% / 80币 +49.6%
            #     0.2 ATR → 40币 +5.0%  / 80币 +59.1%
            #   ★ 这里返回的 dist 是「含缓冲的真实止损距离」——第⑤道闸必须看这个数，
            #     否则加缓冲后实际止损超过 5% 上限的单会被放进来（实测 80 币多亏 36R）。
            _kb_buf = self._p("stop_key_atr_buf", 0.2)
            if (_kb_buf > 0 and atr_n is not None and i < len(atr_n)
                    and atr_n[i] and float(atr_n[i]) > 0):
                _pad = _kb_buf * float(atr_n[i])
                _kb = (_kb - _pad) if want_long else (_kb + _pad)
            if (want_long and _kb > stop) or ((not want_long) and _kb < stop):
                stop = _kb
                stop_src = "key"
        # ⑤c 两档止损
        if dual_stop_min_pct > 0:
            _d3 = ((px - stop) if want_long else (stop - px)) / px * 100.0
            if _d3 < dual_stop_min_pct - 1e-9:
                if _ce_stop is not None and math.isfinite(_ce_stop):
                    stop = _ce_stop
                    stop_src = "dual_ce"
                else:
                    stop = min(lo_win) if want_long else max(hi_win)
                    stop_src = "dual_swing"
        risk = (px - stop) if want_long else (stop - px)
        dist = (risk / px * 100.0) if px > 0 else 0.0
        return stop, stop_src, dist

    async def evaluate(self, ctx) -> list:
        """入场：时段确认 + HA 形态 + 关键K线力度 + BBW 分位 + 止损距离 + 趋势效率（六道闸）。

        VS15-1.2 对齐后的真实顺序（与前端「六道闸」面板逐列一一对应）：
          ① 时段确认   ZLSMA50 实体穿越切多空时段；CE 翻转不再是触发器
          ② HA 形态    HA 收盘站在 ZLSMA 同一侧且方向一致
          ③ 关键K线    原始 K 线实体 ≥ key_mult×ATR20，收盘落在极值 1/3 区，实体占比 ≥ 50%
          ④ BBW 分位   布林带宽度近 100 根分位 ≥ 70（波动扩张才做）
          ⑤ 止损距离   含 ATR 缓冲后必须落在 0.60% ~ 5.00%
          ⑥ 趋势效率   进场前 48 根单边度 EFF ≥ 0.30

        设计要点：
          · 时段是「状态」不是「事件」—— CE 翻转那种事件口径每根都会重刷信号。
          · 布林带宽度只衡量「价格分散程度」，是唯一没被证伪的趋势过滤器
            （ADX、ADX上升、效率比全部失效）。
          · 止损 = 三条并列取近：8 根摆动 / CE 通道 / 关键K线极值（外扩 0.2×ATR20）。
          · ATR 门槛（min_atr_pct）已挪到监控池层，S15 里保持 0 = 不启用。
          · 偏离闸已于 2026-11-11 物理删除，VS15-1.2 把两个配置键也一并删掉了。
        """
        ce_len = int(self._p("ce_len", 1))
        ce_mult = float(self._p("ce_mult", 2.0))
        z_len = int(self._p("zlsma_len", 50))
        use_close = bool(self._p("use_close", True))
        use_ha = bool(self._p("use_ha", True))
        swing_lb = int(self._p("swing_lookback", 8))
        min_atr_pct = float(self._p("min_atr_pct", 0.0))
        bbw_pct_min = float(self._p("bbw_pct_min", 70.0))
        max_risk_pct = float(self._p("max_risk_pct", 5.0))
        # 偏离闸已于 2026-11-11 晚物理删除；VS15-1.2 连 min_dev_pct / dev_atr_mult
        # 两个配置键一起删掉 —— config.py 不再校验、yaml 不再有这两个键、面板不再显示。
        # ②b 关键K线闸（2026-11-10 浩哥定义）：HA 判方向，原始 K 线判力度
        key_bar = bool(self._p("key_bar", True))
        key_mult = float(self._p("key_mult", 1.0))
        key_closepos = float(self._p("key_closepos", 0.66))
        key_bodyfrac = float(self._p("key_bodyfrac", 0.5))
        # ⑤b 关键K线止损：那根大阳线的低点 / 大阴线的高点
        stop_key = bool(self._p("stop_key", True))
        # ⑤c 两档止损（2026-11-10 定档 · 浩哥）：
        #    三条取近后若距离 < dual_stop_min_pct（0.50%），说明是「关键K线」那条
        #    把止损贴到了脸上（BTC/ETH 这类大币常见，0.3~0.5%）→ 这一档只保留 ②CE 通道。
        #    回测 60 天（40 币）：只 CE 336 笔 / 54.8% / 1.31 / +52.21R / −13.32R，
        #    相对「一律三条取近」+14.84R、回撤少 1.23R。取 0 = 关闭两档。
        dual_stop_min_pct = float(self._p("dual_stop_min_pct", 0.5))
        # ⑥ 进场 EFF 闸（2026-11-10 从池子闸挪到进场闸，回测净R +66%）
        eff_min_sig = float(self._p("eff_min_signal", 0.3))
        eff_win_sig = int(self._p("eff_win_signal", 48))
        # 成本口径：默认按 maker 0.02% / 零滑点，和回测一致。
        # ⚠ 这是「假设挂单能成交」的记账口径，不代表线上真能挂到单。
        #    真实限价挂单能力是执行层待办（观察期后单独做）。
        fee_rate = float(self._p("fee_rate", 0.0005))
        slip_pct = float(self._p("slip_pct", 0.0))
        # 反手开关（2026-11-11 深夜 浩哥定：关）。
        #   回测实证（20/40/80 币三组样本）：反手 71 笔均笔 −0.45R，净值 −32.21R，
        #   而且回撤同时变大（−83.63R → −98.84R）—— 不是「少赚」是多亏。
        #   原因：反手要等「时段确认 + 六闸全过」，那时趋势已走一半，追在反转尾巴上。
        #   代码保留（Signal.reverse + engine 平旧仓那段），这里一个开关就能重开。
        reverse_enabled = bool(self._p("reverse_enabled", False))
        kline_iv = str(self.p.get("kline_interval", "15m"))

        # EFF 窗口也在 need 里（2026-11-10 从池子闸挪到进场闸）
        need = max(ce_len, z_len * 2, swing_lb, eff_win_sig, 100) + 80
        out = []

        for sym in self.syms(ctx):
            try:
                # 2026-11-11 晚：旧写法 `if has_position: continue` 把「反向开仓」
                # 这条路整个堵死了 —— 只要这个币有仓，无论新时段方向如何都跳过。
                # 现在只记方向；反手开关关闭时（默认），行为与旧写法完全一致。
                _pos_side = ctx.open_side(sym, self.sid)
                if _pos_side and not reverse_enabled:
                    continue
                ks = await ctx.klines(sym, kline_iv, need)
                if not ks or len(ks) < need:
                    continue

                ind = _ce_zlsma(ks, ce_len, ce_mult, z_len, use_close, use_ha)
                # _ce_zlsma 已内部算好 bbw / bbw_pct，这里不再重复计算
                bbw = ind["bbw"]
                bbw_pct = ind["bbw_pct"]
                # 关键K线 / 关键K线止损 都要 ATR(20)，每个币只算一次
                atr_n = (_atr_n(ks, 20)
                         if (key_bar or stop_key) else None)

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

                # ① 时段判定（ZLSMA50 实体穿越 + 下一根确认）—— 2026-11-11 晚 浩哥定档
                #    旧写法 `d_now != d_prev` 只在 CE 翻转那一根判定，那根不是关键K线
                #    整个时段就全废（UNI 13:15 翻空就是这样，四小时空头行情全丢）。
                #    现在：ZLSMA 穿越划定多/空时段，时段内任意一根关键K线都能开枪。
                _leg_dir, _leg_ts_arr, _side_arr, _leg_starts = _zlsma_legs(
                    ks, ind, _LEG_MAX_WAIT)
                d_now = int(_leg_dir[i] or 0)
                leg_ts_now = _leg_ts_arr[i]

                # 在管仓位 vs 本时段方向（2026-11-11 晚 浩哥定「反向开仓」）
                #   同向 → 本时段已有仓，不重复开（一个时段一段仓位）
                #   反向 → 继续往下走闸门，全过则发 reverse 信号，引擎先平后开
                if _pos_side and d_now != 0:
                    _want = "long" if d_now == 1 else "short"
                    if _pos_side == _want:
                        # 面板诊断表在这条路径上还没建条目，用 setdefault 现建一个，
                        # 否则 why 永远是 None，面板看不出「为什么这只币没开」
                        _GATE_STATE.setdefault(sym, {})["why"] = "同向已有仓位"
                        continue

                # ── 进场闸门诊断（纯内存，零额外流量）──────────────────────
                # 三步的判断本来就要做，这里只是把结果留下给页面看。
                # 顺序：① 时段确认（ZLSMA50 实体穿越划出的多空段）→ ② HA 形态
                #       → ③ 关键K线 → ④ BBW 分位 → ⑤ 止损距离 → ⑥ 趋势效率，全过才开仓。
                _bp = bbw_pct[i]
                _ha_ok = (c > z and c > o) if d_now == 1 else (c < z and c < o)
                _thr = bbw_pct_min if bbw_pct_min > 0 else 70.0
                # 变量曾名 _ce_ok，容易让人以为判的是 CE 翻转 —— 实际判的是
                # _zlsma_legs 切出来的多空时段（状态，不是翻转事件）。改名对齐语义。
                _leg_ok = (d_now != 0)
                # ⚠ 2026-11-06 修：原来写成 `_bp is None or _bp >= _thr`，
                # 数据缺失时诊断表显示"这道过了"，而下面 ④ 的实际开仓判断是
                # `bp is None → continue`（拒绝）。两处相反，诊断会骗人。
                # 现在与开仓逻辑对齐：门槛开着时，None 一律算不通过。
                if bbw_pct_min > 0:
                    _bbw_ok = _bp is not None and _bp >= _thr
                else:
                    _bbw_ok = True          # 策略层关掉 BBW 门槛，不参与判断
                if not _leg_ok:
                    _why = "时段未确认"
                elif not _ha_ok:
                    _why = "HA形态不对"
                elif not _bbw_ok:
                    _why = ("BBW数据缺失" if _bp is None
                            else f"BBW差{_thr - _bp:.0f}点")
                else:
                    _why = "应开仓"
                # ⑤ 止损距离（面板参考值）—— 不管前面几道闸过没过，每个池内币都算一遍。
                #   以前只在「前五道全过」之后才回填，导致光闸面板第⑤道永远是 0/N
                #   （没币走到就没人算），看着像"40 个币全被止损拦了"，实际是一个都没到。
                _ref_stop, _ref_src, _ref_dist = self._stop_ref(
                    ks, i, ind, "long" if d_now == 1 else "short",
                    atr_n, swing_lb, stop_key, dual_stop_min_pct)
                from .quality import _eff as _eff_fn   # 延迟导入，避免模块循环
                _eff_diag = (round(float(_eff_fn(ks[:i + 1], eff_win_sig)), 3)
                             if eff_win_sig > 0 else None)
                _GATE_STATE[sym] = {
                    "ts": bar_ts, "dir": "多" if d_now == 1 else "空",
                    # ★ 键名 "ce" 是前端契约（ActivityTabs 直接读、main.py 统计 ce_n），
                    #   不能动；值已是 ZLSMA 时段判定结果，名字纯属历史包袱。
                    "ce": _leg_ok, "ha": _ha_ok, "bbw": _bp, "bbw_thr": _thr,
                    # ★ 2026-11-12 夜：由顶级关键K线豁免 BBW 时置 True。
                    #   没有这个标记，面板会出现「BBW 3%（灰，没过）+ 结论说全闸通过」的自相矛盾。
                    "bbw_exempt": False,
                    # ②b 关键K线 / ⑥ 进场 EFF（面板必须能看到，否则「没开仓」
                    # 永远只能看到前三道，看不出是卡在关键K线还是趋势效率）
                    "key": (bool(_is_key_bar(ks, i, atr_n,
                                             "long" if d_now == 1 else "short",
                                             key_mult, key_closepos, key_bodyfrac))
                            if (key_bar and atr_n is not None and i < len(atr_n)) else None),
                    "eff": _eff_diag,
                    # 2026-11-09：第五道闸（止损距离上限）在面板上原本不可见，
                    # 被拦的币显示「全部闸门通过·待成交」，等一晚上也不开仓，误导。
                    # 这里先占位，算完止损后回填真实值。
                    "risk": (round(_ref_dist, 3)
                             if (_ref_dist > 0 and math.isfinite(_ref_dist)) else None),
                    "stop_src": _ref_src,
                    "risk_thr": max_risk_pct,
                    # 第⑤道闸现在有上下限（2026-11-11 加了 min_stop_dist_pct），
                    # 面板必须两个都拿到，否则会出现「面板说通过、实际被拦」的不一致。
                    "risk_lo": float(self._p("min_stop_dist_pct", 0.6)),
                    "atr_pct": round(atr / c * 100.0, 3) if c > 0 else None,
                    "ok": False, "why": _why,
                }

                # ① 时段未确认 → 不参与开仓判断（面板已标「时段未确认」）
                if d_now == 0:
                    continue

                # ①b 本时段已经进过场 → 不再重复（一个时段只开一次仓）
                #     为什么需要这个标记：旧代码靠 `d_now != d_prev` 天然保证「一根只进一次」；
                #     改成时段后，同一时段会有几十根 K 线，不加标记会连续重复开仓。
                _leg_key = (self.sid, sym)
                if leg_ts_now is not None and _LEG_ENTERED.get(_leg_key) == leg_ts_now:
                    _g0 = _GATE_STATE.get(sym)
                    if _g0 is not None:
                        _g0["why"] = "本时段已入场"
                    continue

                # ② ATR 门槛 —— 2026-11-06 已搬到池子模块（5m 双档）
                #    池子只管「资格」（这个币能不能做），策略只管「时机」（何时进出）
                #    yaml 里 min_atr_pct 设 0 即关闭；保留代码是为了回测对比
                atr_pct = atr / c * 100.0 if c > 0 else 0.0
                if min_atr_pct > 0 and atr_pct < min_atr_pct:
                    continue

                # ③ 关键 K 线（HA 阳线站上均线 / 阴线跌破均线）
                #    2026-11-06 核对后调整：HA 形态必须在 BBW 之前判断。
                #    顺序定为 CE → HA → KEY → BBW → DEV → RISK → EFF，与面板列序一致。
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
                    # ★ 2026-11-12 新增「顶级关键K线豁免 BBW」（浩哥拍板 1.0/0.8/0.8）
                    #   为什么需要这道后门：BBW 是【滞后确认】—— 它看过去 N 根的整体振幅，
                    #   而突破的第一根不可能已经放大。实测 LIT 11-12：14:30 已是光头光脚大阴线
                    #   （力度 1.077×ATR / 收盘位置 1.000 / 实体占比 1.000），BBW 分位却只有 3，
                    #   一路爬到 86 才放行 → 晚进 2.75 小时、进场价差 2.2%、止损被推远 27%，
                    #   同样一段行情换来的 R 少 36%。晚进不是「少赚」，是把风险回报比搞坏了。
                    #   而「力度 > 1.0×ATR + 收盘位置 > 0.8 + 实体占比 > 0.8」本身就是爆发证据，
                    #   且不滞后 —— 用不滞后的事实替代滞后的确认。
                    #   ★ 实测（双样本 + 后 40 独立验证，60 天）：
                    #     前40 +109.79R（基线 +88.98）｜后40 +54.91R（基线 +29.57）｜80币 +164.70R（基线 +118.55）
                    #     回撤 −100.08（基线 −89.38，恶化 12%）← 已知代价，靠坑位限制兜住
                    #   ★ 被实测否决的相邻档（别再来回试）：
                    #     0.9/0.9 → 后40 仅 +25.31R（比基线还低，过拟合）❌
                    #     1.5/0.8 → 救不了 LIT 14:30（力度 1.077 < 1.5）❌
                    #     buf 0.5 → 即使叠加本豁免仍只有 +85.75R（比基线差）❌
                    #   bbw_exempt_mult = 0 即关闭本豁免。
                    _ex_mult = float(self._p("bbw_exempt_mult", 1.0))
                    if _ex_mult > 0:
                        _ex_cp = float(self._p("bbw_exempt_closepos", 0.8))
                        _ex_bf = float(self._p("bbw_exempt_bodyfrac", 0.8))
                        if not _is_key_bar(ks, i, atr_n, want, _ex_mult, _ex_cp, _ex_bf):
                            _g0 = _GATE_STATE.get(sym)
                            if _g0 is not None:
                                _g0["why"] = "BBW分位"
                            continue
                        # ★ 豁免成立：BBW 分位不合格，但这根 K 线是顶级关键K线 → 放行。
                        #   必须打标记，否则面板看起来像「BBW 没过却全通了」。
                        _g0 = _GATE_STATE.get(sym)
                        if _g0 is not None:
                            _g0["bbw_exempt"] = True
                    else:
                        _g0 = _GATE_STATE.get(sym)
                        if _g0 is not None:
                            _g0["why"] = "BBW分位"
                        continue

                # 委托价用「真实收盘价」，不用 HA 合成价。
                # 2026-11-06 定案（方案 B）：指标域用 HA 判方向，委托域用真实价算钱。
                # 实测证据：LIT 那笔 HA 收盘 3.4170，真实收盘 3.4530（差 +2.11%），
                # 导致实际盈亏比从 1.20 掉到 0.73、单笔风险从 0.5% 放大到 0.82%。
                px = float(ks[i]["close"])
                if px <= 0 or not math.isfinite(px):
                    continue

                # ⑥ 偏离闸已于 2026-11-11 晚按浩哥决定【物理删除】；
                #    VS15-1.2 把配置键和面板那一列一起删干净了。
                #    为什么删：换成「时段内首次关键K线」后，刚确认的时段天然贴着均线，
                #    这道闸挡掉的恰恰是「刚启动的趋势」（UNI 16:15 差 0.664% vs 0.931% 误杀）。
                #    ⚠ 留证（旧触发口径 60 天 20 币实测）：偏离 ≤0.5% 的 104 笔净R −14.8、
                #      胜率 34.6%；>0.5% 的 640 笔净R +90.9、胜率 47.8%。
                #      这组数挂在旧口径上，若以后加反手后胜率下滑，第一个回来查的就是它。
                _g = _GATE_STATE.get(sym)

                # ⑤ 止损 = 三条并列取近 + 两档修正
                #    逻辑只在 _stop_ref() 里写一份，面板第⑤道闸也调它 ——
                #    避免「面板显示的距离」和「真实开仓用的距离」两套代码各算各的。
                side = "long" if d_now == 1 else "short"
                stop, stop_src, _rpct = _ref_stop, _ref_src, _ref_dist
                risk = (px - stop) if side == "long" else (stop - px)
                if risk <= 0 or not math.isfinite(risk):
                    continue
                # ⑤ 止损距离上限（第五道闸）—— 2026-11-09 浩哥定：30% → 5%
                #    止损距离 = 1R 的大小。太宽的单子价格根本跑不到 1.8R/2R：
                #    实盘 80 笔中止损 ≥5% 的 23 笔（占 29%）合计 -118.78U，均峰值仅 0.32R。
                #    这里回填面板可见值，让「被拦」有痕迹，不再假装「待成交」。
                #    （_rpct 由 _stop_ref 一起算好，不重复计算）
                if _g is not None:
                    _g["risk"] = round(_rpct, 3)
                # ⑤ 止损距离下限（2026-11-11 浩哥定：0.60%）
                #    为什么要有下限：手续费按名义仓位收（0.05%×2），止损越窄，
                #    手续费占 1R 的比例越大 —— 入场止损 0~0.5% 那档手续费吃掉
                #    2R 的 18.4%，而且噪音轻易就能打掉它。40币60天按入场止损距离
                #    分桶实测：<0.75% 的 81 笔（占 24%）合计亏 3.6R，是唯一整体
                #    亏钱的一档；卡到 0.60% 后五项同时变好（见 strategies.yaml 注释）。
                #    ★ 0 = 关闭。别和旧的「监控池筛币」混淆：这是进场闸。
                _min_stop = float(self._p("min_stop_dist_pct", 0.6))
                if _min_stop > 0 and _rpct < _min_stop - 1e-9:
                    if _g is not None:
                        _g["why"] = f"止损太近{_rpct:.2f}%"
                    log.info("S15 %s 止损距离 %.2f%% < %.2f%%（下限），跳过开仓",
                             sym, _rpct, _min_stop)
                    continue
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
                # 任何一道拒绝都不会有仓位。所以策略层只能说「全部闸门通过」，
                # 真实结论由 /api/gates 用持仓表 + 信号表的 skip_reason 覆盖。
                _GATE_STATE[sym]["ok"] = True
                _GATE_STATE[sym]["why"] = "全闸通过"
                # 标记本时段已入场，同一个时段后续的 K 线不再重复开仓
                _LEG_ENTERED[_leg_key] = leg_ts_now
                out.append(Signal(
                    strategy=self.sid, symbol=sym, side=side,
                    reason=(f"ZLSMA{'站上' if side == 'long' else '跌破'}时段+"
                            f"{'阳' if side == 'long' else '阴'}线关键K"
                            f"{'站上' if side == 'long' else '跌破'}ZLSMA "
                            f"ATR={atr_pct:.2f}%"
                            + (f" BBW分位={bp:.0f}%" if bp is not None else "")),
                    stop_price=round(stop, 8),
                    stop_src=stop_src,
                    max_hold_sec=self.max_hold,
                    fresh=0, strength=abs(c - z) / atr,
                    fee_rate=fee_rate, slip_pct=slip_pct,
                    # 反手：这只币原本有仓，且方向与本时段相反（同向的上面已经 continue 掉了）
                    reverse=bool(_pos_side and _pos_side != side),
                ))
            except Exception as exc:  # noqa: BLE001
                log.warning("S15 %s 计算失败: %s", sym, exc)
        return out

    async def check_exit(self, ctx, pos):
        """S15 出场总入口（2026-11-10 B 档重排，浩哥定案）。

        出场共 **5 条**，按判定顺序（硬止损在 engine.py 先判；其余在本方法）：

            —— engine.py 负责（优先级最高，先判）——
            ⓪ 硬止损：现价碰到止损价 → 全平（三条候选取最近那条）

            —— 本方法负责（每 0.5 秒判一次）——
            ① 保本移损：峰顶到过 be_at_r(1.5R) → 有效止损抬到入场价
                         （_breakeven，下面第 ⓪ 段；只抬损不平仓，剩的仓最差打平）
            ② 平一半：  现价 ≥ half_at_r(1.2R) → 平掉 50%
                         （_half_take_profit）
            ③ 兜底全平：已平半 且 现价 ≥ full_at_r(2.5R) → 平剩余
                         （_full_target）—— 防止一直不穿均线时没有出口
            ④ 右侧止盈：已平半 且 HA 反向 + HA收穿 ZLSMA50
                         + 那根原始K线本身是反向关键K线（_right_exit）

        ★ 2026-11-10 B 档下线了四条规则（方法体已删）。但注意：
           · 16h 时间止损【没有完全删干净】—— 它留了一道安全阀：
             strategies.py:92 仍会读 max_hold_hours 算出 max_hold_sec，
             存进 positions 表，engine.py:178 用 `pos["max_hold_sec"] and ...`
             判断（0 值短路，永不触发）。yaml 里 max_hold_hours: 0 就是关着。
             这个 0 值保护必须留着：没有它，now-opened_at>=0 恒真，每单开出来就平。
           · 快速失败（_quick_fail）—— 方法体已删，实跑 12 笔 −440U
           · 镜像出场（_mirror_exit）—— 方法体已删，与右侧止盈那两条重叠且会抢在前面掐单
           · 移动止盈 / 平半后再赚 N 个 R 全平（trail_arm/trail_dd/half_tp_r）—— 已删

        ★ 平半后的逻辑变了（与旧版最大的区别）：
           旧：3R 全平 + 移动止盈 1.5/1.2（两条都在盯着"已经赚到的钱不要吐回去"）
           新：右侧止盈（等反向信号）+ 2.5R 兜底 —— 让趋势自己走完，不主动收网

        流量：check_exit 每 0.5 秒被调用一次，6 个仓位就是 12 次/秒。
        这里按「bar 边界」缓存指标 —— 一根 K 线没收盘指标不会变，
        只有跨过 bar 边界才真正去拉数据。不做这个缓存是每天几百 MB。
        """
        try:
            # 峰顶维护（无条件，必须）—— 前端「峰顶R / 峰顶→当前价回撤区」靠它。
            # 原先这段藏在已删除的镜像出场里，镜像一删峰顶就冻结，所以提到最前面。
            st = self._refresh_peak(ctx, pos)

            # ⓪ 保本移损（2026-11-11 浩哥定：1.5R）
            #    峰顶到过 be_at_r → 把「有效止损」抬到入场价，剩下的仓最差打平。
            #    ★ 不动 pos["stop_price"]：那是全系统算 R 的分母
            #      （_refresh_peak / _half_take_profit / _full_target / _right_exit
            #       都用 abs(entry-stop_price)），改成 entry 就等于分母归零。
            #      所以只置 be_on=1，由 engine 判「要不要平」时改用 entry_price。
            #    ★ 必须落库：只放内存的话重启后丢失 → 已经抬过的止损又掉回原位。
            #    回测 40币60天：1.5R 触发 29 笔、净值 +52.54R（比无保本 +0.33R）、
            #      回撤 -13.32→-12.82R、胜率 55.1%（五档最高）。
            _be_at_r = float(self._p("be_at_r", 1.5))
            if (_be_at_r > 0 and not int(pos.get("be_on") or 0) and st
                    and float(st.get("peak") or 0.0) >= _be_at_r - 1e-9):
                try:
                    if db.update_position(pos["id"], be_on=1):
                        pos["be_on"] = 1
                        log.info("S15 %s 峰顶 %.2fR ≥ %.2fR → 止损抬到入场价 %s（保本）",
                                 pos.get("symbol"), float(st.get("peak") or 0.0),
                                 _be_at_r, pos.get("entry_price"))
                except Exception as exc:  # noqa: BLE001
                    # 落库失败不能拖死出场判断（下一轮还会再判一次），
                    # 但 ★必须是 warning★：第一版用 log.debug 把
                    # 「update_position 白名单里没有 be_on」的 ValueError 吞掉了，
                    # 表现是「代码看着对、保本从来不生效」。静默失败比报错更贵。
                    log.warning("be_on 落库失败 %s: %s", pos.get("symbol"), exc)

            # ① 平一半（half_at_r = 1.2R）
            half = await self._half_take_profit(ctx, pos)
            if half:
                return half

            # ② 兜底全平（full_at_r = 2.5R）→ ③ 右侧止盈
            #    顺序与回测 sim() 一致：先 2R，再右侧
            full = self._full_target(ctx, pos)
            if full:
                return full
            return await self._right_exit(ctx, pos)
        except Exception as exc:  # noqa: BLE001
            log.warning("S15 check_exit %s 失败: %s", pos.get("symbol"), exc)
            return None

    def _refresh_peak(self, ctx, pos):
        """只做一件事：把当前浮盈 R 送进峰顶状态机（_touch_pos_state）。

        为什么单独拆出来：镜像出场删除后，原先唯一的调用点就没了；
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
            return None
        # 返回值给 check_exit 用：保本移损要读峰顶（st["peak"]）
        return self._touch_pos_state(pos, entry, risk, cur_r)

    def _full_target(self, ctx, pos):
        """② 兜底全平：已平半 且 现价R ≥ full_at_r(2.5，2026-11-12 由 2.0 上调) → 平剩余半仓。

        为什么需要它：③ 右侧止盈要等「一根够力道的反向K线」。如果价格慢慢阴跌回均线
        附近，却始终不出这样一根K线，仓位就会一直挂着 —— 2.5R 全平是那条保底线。
        回测出场归因：它占 12.2%（34 笔），是第四大出口。

        full_at_r = 0 即关闭。
        """
        thr = float(self._p("full_at_r", 2.5))
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
        if not bool(self._p("right_exit", True)):
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

        ⚠ 缓存是共享的：_right_exit 和 _refresh_peak 必须看到同一组数。
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

    async def _half_take_profit(self, ctx, pos):
        """浮盈达 half_at_r（B 档 1.2R，从 yaml 读）且没平过一半 → 平掉 50%，剩一半放开跑。

        ⚠ 以下这张表是 **2026-08 的老记录（1.2R / 2.0R 时代）**，只作决策沿革参考，
          **不是当前参数**。VS15-1.2 现役出场是五条，按顺序先命中先执行：
          硬止损 → 保本 1.5R → 平半 1.2R → 兜底全平 2.5R → 右侧止盈
          （时间止损/快速失败/移动止盈/镜像出场/收盘价止损 全部已删）。
          最新口径见 F:/obsidian笔记库/QUANT-BOT_VS15-1.2版本/VS15-1.2定版.md

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
        thr = float(self._p("half_at_r", 1.2))
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



# 只注册线上真正跑的策略。
# 2026-11-06 自检清理：S02/S06/S11/S14/S16 五个策略早已停用，代码在
# backend/strategies_legacy.py 存档。这里只留当前在跑的 S15。
REGISTRY = {cls.sid: cls for cls in [S15CeZlsma]}
