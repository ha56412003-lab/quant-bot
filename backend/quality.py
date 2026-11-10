"""监控池（三层池的第 2 层）—— 止损距离 + 趋势效率 + 插针。

2026-11-08 浩哥拍板重构（入口指标整套换掉，ATR 退出监控池）：

【为什么换 —— ATR 筛反了】
  200 币全量实测：
      按 ATR>0.40% 进池的 50 个：EFF 中位 0.387、42% 是横盘(EFF≤0.30)
      被 ATR 拦掉的 150 个：     EFF 中位 0.525、21% 是横盘
  → 进池那组趋势效率反而比被拦组低 36%。ATR 筛出来的恰恰是更没方向的币。
  根因两条：
    · ATR 测「跳得狠不狠」，不测「有没有方向」—— 排横盘是 EFF 的活
    · ATR 与手续费只是间接关系，止损距离才是直接的那个量

【新三道闸门】
  第 1 道  止损距离 ≥ 1.00%（管手续费，物理直接）
      止损距离 = (现价 − 最近 20 根 15m 最低价) ÷ 现价 × 100%
      与 strategies.py 的止损算法同口径（swing_lookback=20，原始 K 线）
      1.00% 时：仓位 = 1R ÷ 1.0% = 15,000U，手续费(0.1%) 占 1R 恰为 10%
      0.78% 是临界点 —— 再窄就被单币名义上限截断，手续费占比飙升
        （临界点 = 单笔风险1.5% ÷ (单币上限32% × 杠杆6) = 0.781%，与净值无关）
      实测链条：止损 0.2%→手续费占 50% / 0.5%→20% / 1.0%→10% / 2.0%→5%

  第 2 道  趋势效率 EFF ≥ 0.30（排横盘）
      EFF = |末价/首价 − 1| ÷ ((最高−最低)/最低)，96 根 15m = 24 小时
      ≥0.55 单边 / 0.30~0.55 半趋势 / ≤0.30 宽震
      出处 Kaufman 1995（KAMA），与 radar.py 的 _eff 同口径
      低滞后：不用 MA/EMA 交叉、MACD、RSI、24h 涨跌幅

  第 3 道  插针 ≤ 8.0（防闪崩滑点）
      spike = 最近 96 根 1h 里最大单根振幅 ÷ 1h ATR
      2026-11-08 从 4.0 放宽到 8.0：实测这一关卡掉 177/200 个币（89%），
      而里面确实有好东西（TAUSDT 插针 4.9 / 止损 1.56% / EFF 0.87 被一刀切）

  另外：成交额 ≥ 500 万 U（2026-11-08 从 1000 万放宽）

【滞回（防每分钟抖动）】
  进池：止损 ≥ 1.00%  且  EFF ≥ 0.30
  出池：止损 < 0.85%   或  EFF < 0.25
  中间：止损 0.85~1.00 / EFF 0.25~0.30 → 不动

【平滑（STOP_EFF 都取 5 分钟中位数）】
  实测（1m 数据）：止损距离每分钟抖 0.033%、EFF 抖 0.0137
  从 1.00 掉到 0.85 只要 4.5 分钟 → 不平滑会有 27% 的币刚进 5 分钟就出
  取过去 5 分钟采样的中位数 → 刚进就出降到 1%，每币进出 6.5 次/75小时
  用中位数不用均值：1 分钟的尖刺不影响中位数

【刷新（每分钟，增量拉取）】
  启动：全量拉 96 根 15m 建缓存（200 币一次性约 1.9MB）
  每 60 秒：只拉最新 2 根去重合并 → 55MB/天、权重 200/分钟（上限 1200）
  ⚠ 绝不能每分钟全量拉 96 根 = 2,637MB/天，不可行
  实测 limit=2 能拿到「正在走、还没收盘」那根，high/low 确实在动

【持仓中的币】
  2026-11-08 浩哥定：不归监控池管 —— 达标就留、不达标就踢。
  出场由 engine.manage_positions() 遍历 db.open_positions() 负责，
  它不看监控池名单，所以踢出池不会导致出场失联（已核实 engine.py:84）。

【参数对照（旧 → 新）】
  刷新        300 秒   →  60 秒
  第 1 道     5m ATR>0.40%  →  止损距离 ≥1.00%（滞回 0.85）
  第 2 道     （无）        →  EFF ≥0.30（滞回 0.25）
  第 3 道     插针 ≤4.0    →  插针 ≤8.0
  成交额      ≥1000万      →  ≥500万
  池子容量    200          →  40
  保护期      30 分钟      →  15 分钟
  ATR         生效         →  停用（参数保留仅为回滚）

⚠ CE 的 ATR（strategies.py 里 _rma(tr, ce_len=1)）与本文件无关，一直保留 ——
  去掉它就不是 S15 了。本文件只管监控池入口。
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import threading
import time

from . import binance

log = logging.getLogger(__name__)

# 首次池子算完之前，扫描循环不该开工 —— 否则 monitor_list() 为空，
# 策略会回落到全量候选池 200 个币，白拉一堆 K 线还灌噪音信号。
READY = threading.Event()

# ── 默认值（可被 strategies.yaml 的 pools.monitor 段覆盖）──────
MIN_QUOTE_VOL = 5e6      # 24h 成交额下限：500 万 U（2026-11-08 从 1000万 放宽）
MAX_SPIKE = 8.0          # 单根最大振幅 ÷ ATR 上限（2026-11-08 从 4.0 放宽）
MONITOR_SIZE = 40        # 监控池容量（2026-11-08 从 200 收紧，实测组合约 39 个）

# ── 第 1 道：止损距离 ──────────────────────────────────────
# 2026-11-10 B 档：进池 1.00 → 0.50，出池 0.85 → 0.30
#   必须成对改：出池线一旦高于进池线，币刚进池就被踢，池子空转。
#   0.5% 依据：B 档回测 279 笔里止损 0.5~1.0% 那档 106 笔胜率 51.9%、仍赚钱；
#   过滤掉的是 0.5% 以下（手续费吃掉 1R 的 20%+）。
STOP_MIN_PCT = 0.50      # 进池线（%）
STOP_EXIT_PCT = 0.30     # 出池线（%），必须低于进池线
# 摆动回看根数 —— 只作回退默认值，真实值从 strategies.yaml 的
# S15_CE_ZLSMA.params.swing_lookback 读（见 _swing_lb）。
# 2026-11-09 修：原先是硬编码 20，与策略各存一份 —— 一旦改 yaml，
# 池表仍按 20 算止损距离、策略按新窗口算，两边对不上，
# 会出现「池表显示止损 3% 通过过滤、实际开仓却按 1.8% 算」的鬼故事。
STOP_LOOKBACK = 8

# ── 第 2 道：趋势效率（2026-11-10 B 档：从池子闸删除，0 = 关闭）────────
# 为什么删：EFF 从「选币」挪到「择时」更有效（回测净R +66%）。
# 池子四道闸从此只管「有没有量、会不会插针、止损距离合不合理」，
# 趋势的事交给进场那一刻（strategies.yaml → eff_min_signal: 0.30 / eff_win_signal: 48）。
EFF_MIN = 0.0            # 进池线（0 = 关闭本闸）
EFF_EXIT = 0.0           # 出池线（0 = 关闭本闸）
EFF_WIN = 96             # 窗口：96 根 15m = 24 小时

# ── 平滑与刷新 ────────────────────────────────────────────
SMOOTH_SEC = 300         # 平滑窗口：5 分钟（取窗口内所有采样的中位数）
KL_INTERVAL = "15m"      # K 线周期（止损与 EFF 都用它）
KL_NEED = 96             # 缓存根数：EFF 需要 96 根，止损需要 20 根 → 取 96
MIN_DWELL_SEC = 900      # 进池后最短停留 15 分钟（从 30 分钟缩短）
REFRESH_SEC = 60         # 每分钟刷新一次
TICK_SEC = 60            # 主循环节拍

# ── 插针专用的 ATR ────────────────────────────────────
# spike = 最近 96 根 1h 里最大单根振幅 ÷ 1h ATR（Wilder 14）。
# ⚠ 这个 ATR 只服务插针门槛，跟监控池的入池口径无关；
#   CE 的 ATR（strategies.py 的 _rma(tr, ce_len=1)）更是另一个东西。
ATR_N = 14

# ── 刷新节奏 ──────────────────────────────────────────
_RETRY_SEC = 600          # 整轮刷新全失败时的重试间隔（秒）
SPIKE_LOOKBACK = 100      # 插针：拉 100 根 1h（实际用最后 96 根）
DAILY_REFRESH_SEC = 86400  # 日线插针：每天

FETCH_CONCURRENCY = 8    # 并发数（200 币串行要 200 秒，并发 8 约 25 秒）

# ── 状态 ──────────────────────────────────────────────────
_rows: dict[str, dict] = {}       # sym -> {spike, qv, ts_daily}
_monitor: list[str] = []          # 当前监控池
_in_since: dict[str, float] = {}  # sym -> 进池时间戳

# 15m K 线缓存：sym -> [{"ts","high","low","close"}, ...]，按 ts 升序
_kl: dict[str, list] = {}
# 平滑采样队列：sym -> {"stop": [...], "eff": [...], "ts": [...]}
_hist: dict[str, dict] = {}

_ts_daily = 0.0      # 日线插针上次刷新
_ts_kl = 0.0         # K 线上次增量拉取
_ts = 0.0            # 监控池上次重算
_bootstrap_done = False   # 首次全量建缓存是否完成


def configure(cfg: dict | None = None) -> None:
    """从 strategies.yaml 的 pools.monitor 段热更新阈值（缺项保留原值）。"""
    global MIN_QUOTE_VOL, MAX_SPIKE, MONITOR_SIZE, MIN_DWELL_SEC
    global STOP_MIN_PCT, STOP_EXIT_PCT, EFF_MIN, EFF_EXIT, EFF_WIN
    global SMOOTH_SEC, KL_INTERVAL, REFRESH_SEC, TICK_SEC
    global DAILY_REFRESH_SEC
    cfg = cfg or {}

    def _f(k, cur):
        return float(cfg[k]) if k in cfg else cur

    def _i(k, cur):
        return int(cfg[k]) if k in cfg else cur

    # 新方案（生效）
    MONITOR_SIZE = _i("size", MONITOR_SIZE)
    MIN_QUOTE_VOL = _f("min_quote_vol", MIN_QUOTE_VOL)
    MAX_SPIKE = _f("max_spike", MAX_SPIKE)
    MIN_DWELL_SEC = _i("min_dwell_sec", MIN_DWELL_SEC)
    STOP_MIN_PCT = _f("stop_min_pct", STOP_MIN_PCT)
    STOP_EXIT_PCT = _f("stop_exit_pct", STOP_EXIT_PCT)
    EFF_MIN = _f("eff_min", EFF_MIN)
    EFF_EXIT = _f("eff_exit", EFF_EXIT)
    EFF_WIN = _i("eff_win", EFF_WIN)
    SMOOTH_SEC = _i("smooth_sec", SMOOTH_SEC)
    REFRESH_SEC = _i("refresh_sec", REFRESH_SEC)
    TICK_SEC = _i("tick_sec", TICK_SEC)
    DAILY_REFRESH_SEC = _i("daily_refresh_sec", DAILY_REFRESH_SEC)
    _iv = cfg.get("kl_interval")
    if _iv is not None:
        KL_INTERVAL = str(_iv)


# ── 指标计算 ──────────────────────────────────────────────

def _atr(bars, n: int = ATR_N):
    """Wilder ATR（旧监控池口径，已停用；保留供回滚与自检脚本调用）。"""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    val = sum(trs[:n]) / n
    for x in trs[n:]:
        val = (val * (n - 1) + x) / n
    return val


# swing_lookback 读取缓存：(值, yaml mtime)。改完 yaml 自动失效，无需重启。
_SWING_LB_CACHE = None
_SWING_LB_MTIME = None


def _swing_lb() -> int:
    """读 strategies.yaml → S15_CE_ZLSMA.params.swing_lookback。

    必须与 strategies.py 同源：池表第 1 道闸（止损距离 ≥1.00%）和策略的
    止损价必须是同一个窗口算出来的。各存一份的后果是——
      池表说「止损 3.0%，通过」→ 实际开仓按另一窗口算出 4.8%
      → 看着该开却没开，或者开了之后 R 全对不上，排查时白费一整天。

    按文件 mtime 缓存：改完 yaml 下一轮池刷新即生效，不需要重启服务。
    """
    global _SWING_LB_CACHE, _SWING_LB_MTIME
    try:
        from . import config          # 延迟导入，避开 config ↔ quality 循环
        import yaml
        path = config.STRATEGIES_PATH
        mtime = path.stat().st_mtime
        if _SWING_LB_CACHE is not None and mtime == _SWING_LB_MTIME:
            return _SWING_LB_CACHE
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sc = (cfg.get("strategies") or {}).get("S15_CE_ZLSMA") or {}
        val = int((sc.get("params") or {}).get("swing_lookback") or STOP_LOOKBACK)
        if val <= 0:
            raise ValueError("swing_lookback 非法: %r" % (val,))
        _SWING_LB_CACHE, _SWING_LB_MTIME = val, mtime
        return val
    except Exception as exc:  # noqa: BLE001
        log.warning("quality: 读 swing_lookback 失败，回退 %d（%s）", STOP_LOOKBACK, exc)
        return STOP_LOOKBACK


def _stop_dist(bars, lb: int | None = None):
    """做多止损距离（%）=（现价 − 最近 lb 根最低价）÷ 现价 × 100。

    与 strategies.py 的止损算法同口径：用原始 K 线 low（不是 HA）。
    lb 默认从 strategies.yaml 的 swing_lookback 读（20 → 15m × 20 = 5 小时窗口）。

    返回 None 的情况：K 线不够、价格非法、现价已在最低点之下。
    注意：这里只算「做多」方向。做空的距离是 (最高价 − 现价)/现价，
    监控池需要知道「这个币现在做哪个方向止损够宽」，取两者最大值更宽松：
    现价离区间低点远 → 做多止损宽；离区间高点远 → 做空止损宽。
    实际取 max(多, 空)，理由：只要有一个方向能做，这币就值得监控。
    """
    lb = _swing_lb() if lb is None else lb      # 与策略止损同源（见 _swing_lb）
    if not bars or len(bars) < lb + 1:
        return None
    px = bars[-1]["close"]
    if not px or px <= 0:
        return None
    seg = bars[-lb:]
    lo = min(b["low"] for b in seg)
    hi = max(b["high"] for b in seg)
    if lo <= 0:
        return None
    d_long = (px - lo) / px * 100.0 if px > lo else 0.0
    d_short = (hi - px) / px * 100.0 if hi > px else 0.0
    return max(d_long, d_short)


def _eff(bars, win: int = EFF_WIN):
    """趋势效率 = |末价/首价 − 1| ÷ ((最高−最低)/最低)。

    与 radar.py 的 _eff 同口径（Kaufman 1995）。
    ≥0.55 单边 / 0.30~0.55 半趋势 / ≤0.30 宽震。
    """
    if not bars or len(bars) < win:
        return None
    seg = bars[-win:]
    p0, p1 = seg[0]["close"], seg[-1]["close"]
    if not p0 or p0 <= 0:
        return None
    lo = min(b["low"] for b in seg)
    hi = max(b["high"] for b in seg)
    if lo <= 0:
        return None
    amp = (hi - lo) / lo
    if amp <= 0:
        return None
    return abs(p1 / p0 - 1.0) / amp


def _push_hist(sym: str, stop, eff) -> None:
    """把本次采样推进平滑队列，只保留 SMOOTH_SEC 内（按 TICK_SEC 折算条数）。"""
    h = _hist.setdefault(sym, {"stop": [], "eff": [], "ts": []})
    h["ts"].append(time.time())
    h["stop"].append(stop)
    h["eff"].append(eff)
    # 容量：窗口 / 节拍，至少 1 条
    cap = max(1, int(SMOOTH_SEC / max(1, TICK_SEC)))
    for k in ("stop", "eff", "ts"):
        if len(h[k]) > cap:
            del h[k][:len(h[k]) - cap]


def _median(vals):
    """中位数，忽略 None。空集返回 None。"""
    xs = [v for v in vals if v is not None]
    if not xs:
        return None
    return statistics.median(xs)


def _smoothed(sym: str):
    """(平滑后的止损距离, 平滑后的 EFF)，各自取窗口内中位数。"""
    h = _hist.get(sym)
    if not h:
        return None, None
    return _median(h.get("stop") or []), _median(h.get("eff") or [])


# ── 数据采集 ──────────────────────────────────────────────

async def _fetch_kl(sym: str, limit: int = KL_NEED):
    """拉 15m K 线。limit=2 用于每分钟增量；limit=KL_NEED 用于首次建缓存。"""
    try:
        bars = await binance.klines(sym, KL_INTERVAL, limit)
        if not bars:
            return []
        return [{"ts": int(b["ts"]), "high": float(b["high"]),
                 "low": float(b["low"]), "close": float(b["close"])} for b in bars]
    except Exception as exc:  # noqa: BLE001
        log.debug("kl fetch %s failed: %s", sym, exc)
        return []


def _merge_kl(sym: str, bars) -> None:
    """按 ts 去重合并进缓存，裁剪到最多 KL_NEED*2 根（够算所有指标还有余量）。"""
    if not bars:
        return
    cur = _kl.setdefault(sym, [])
    if not cur:
        cur.extend(bars)
    else:
        idx = {b["ts"]: i for i, b in enumerate(cur)}
        for b in bars:
            i = idx.get(b["ts"])
            if i is None:
                cur.append(b)
            else:
                cur[i] = b          # 同 ts 覆盖（未收盘那根会被后续覆盖成收盘版）
        cur.sort(key=lambda x: x["ts"])
    cap = KL_NEED * 2
    if len(cur) > cap:
        del cur[:len(cur) - cap]


async def _fetch_spike(sym: str):
    """1 小时 K 线：拉 100 根，算插针（最大振幅 ÷ ATR）。

    ⚠ 周期写死 "1h"，不要改成可配置 —— 2026-11-06 的坑：曾把 yaml 的
    bbw_interval 直接当 klines 的 interval 传，别人写 "off" 就变成
    请求 interval=off → 币安 400 → 函数返回 {} → 插针算不出来 →
    「插针 ≤4.0」门槛静默失效，日志只有 refreshed for 0 symbols。
    """
    try:
        bars = await binance.klines(sym, "1h", SPIKE_LOOKBACK)
        if len(bars) < 96:
            return {}
        a = _atr(bars)
        if not a:
            return {}
        return {"spike": max(b["high"] - b["low"] for b in bars[-96:]) / a}
    except Exception as exc:  # noqa: BLE001
        log.debug("spike fetch %s failed: %s", sym, exc)
        return {}


async def _update_quote_vol(universe) -> None:
    """24h 成交额走全市场 ticker，零额外请求（引擎本来就在拉）。"""
    try:
        tick = await binance.ticker_24h_all()
        m = {d["symbol"]: float(d.get("quoteVolume") or 0) for d in tick}
        for s in universe:
            row = _rows.setdefault(s, {})
            if s in m:
                row["qv"] = m[s]
                row["ts_qv"] = time.time()
    except Exception as exc:  # noqa: BLE001
        log.debug("ticker fetch failed: %s", exc)


# ── 准入判定 ──────────────────────────────────────────────

def _judge(sym: str) -> tuple[bool, str]:
    """单币的池子资格：成交额 + 插针。进出池用同一把尺子。

    止损距离与 EFF 不在这里 —— 它们要带滞回，逻辑在 recompute()。
    """
    r = _rows.get(sym) or {}
    why = []
    qv = r.get("qv")
    if qv is not None and qv < MIN_QUOTE_VOL:
        why.append("量小")
    spike = r.get("spike")
    if spike is not None and spike > MAX_SPIKE:
        why.append("易插针")
    return (not why), ",".join(why)


# ── 刷新流程 ──────────────────────────────────────────────

async def _gather(syms, fn):
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _one(s):
        async with sem:
            r = await fn(s)
            await asyncio.sleep(0.03)   # 轻微错峰，避免瞬时打满权重
            return s, r

    return await asyncio.gather(*[_one(s) for s in syms])


async def _bootstrap_kl(universe) -> int:
    """首次建缓存：全量拉 KL_NEED 根 15m（一次性约 1.9MB）。"""
    global _bootstrap_done
    res = await _gather(list(universe), lambda s: _fetch_kl(s, KL_NEED))
    n = 0
    for sym, bars in res:
        if bars:
            _merge_kl(sym, bars)
            n += 1
    if n:
        _bootstrap_done = True
        log.info("quality: 15m 缓存建立完成 %d/%d 个币", n, len(universe))
    else:
        log.warning("quality: 15m 缓存建立全失败（%d 个），下轮重试", len(universe))
    return n


async def _refresh_kl_incr(universe) -> int:
    """每分钟增量：只拉最新 2 根，去重合并。"""
    global _ts_kl
    if not _bootstrap_done:
        return 0
    res = await _gather(list(universe), lambda s: _fetch_kl(s, 2))
    n = 0
    for sym, bars in res:
        if bars:
            _merge_kl(sym, bars)
            n += 1
    if n == 0:
        # 整轮失败：退避 10 分钟，避免每个 tick 都重试打爆权重
        _ts_kl = time.time() - REFRESH_SEC + _RETRY_SEC
        log.warning("quality kl: 增量刷新全部失败（%d 个），%ds 后重试",
                    len(universe), _RETRY_SEC)
    else:
        _ts_kl = time.time()
    return n


def _update_samples(universe) -> None:
    """按当前缓存算止损距离 + EFF，推进平滑队列。"""
    for sym in universe:
        bars = _kl.get(sym)
        if not bars:
            continue
        stop = _stop_dist(bars)
        eff = _eff(bars, EFF_WIN)
        _push_hist(sym, stop, eff)


async def _refresh_daily(universe) -> int:
    """日线插针：全候选池每天一次。"""
    global _ts_daily
    res = await _gather(list(universe), _fetch_spike)
    n = 0
    for sym, d in res:
        if not d:
            continue
        _rows.setdefault(sym, {}).update(d)
        n += 1
    if n == 0:
        # 整轮失败：退避 10 分钟重试，而不是空等 24 小时（插针门槛会一直失效）
        _ts_daily = time.time() - DAILY_REFRESH_SEC + _RETRY_SEC
        log.warning("quality daily: 插针刷新全部失败（%d 个），%ds 后重试",
                    len(universe), _RETRY_SEC)
    else:
        _ts_daily = time.time()
        log.info("quality daily: spike refreshed for %d/%d symbols", n, len(universe))
    return n


# ── 池子重算（三道闸门 + 滞回 + 容量）─────────────────────

def recompute(universe) -> list[str]:
    """根据当前数据重算监控池名单。返回新名单。

    进出池用平滑后的（5 分钟中位数）止损距离与 EFF，带滞回：
        进池：止损 ≥ STOP_MIN_PCT（EFF_MIN > 0 时另加 EFF ≥ EFF_MIN）
        出池：止损 < STOP_EXIT_PCT（EFF_EXIT > 0 时另加 EFF < EFF_EXIT）
        中间：不动
    2026-11-10 B 档起 EFF_MIN / EFF_EXIT 均为 0 → 趋势效率闸关闭，
    只判「成交额 / 插针 / 止损距离」三道。持仓中的币不特殊对待
    （2026-11-08 浩哥定）—— 出场由 engine 负责。
    """
    global _monitor, _ts
    now = time.time()
    now_set = set(universe)

    # ① 已在池的：判断留不留
    kept: list[str] = []
    for sym in _monitor:
        if sym not in now_set:
            continue                       # 掉出候选池，自动出局
        # 缺失进池时间时按「现在」补上：否则 now - 0 是个巨大值，
        # 最短停留保护会反向失效（刚进池就被踢，分钟级反复进出）。
        _in_since.setdefault(sym, now)
        # 进池未满 MIN_DWELL_SEC → 保护期内不因「变差」被踢
        if now - _in_since[sym] < MIN_DWELL_SEC:
            kept.append(sym)
            continue
        # —— 满保护期，才做清退判断 ——
        stop, eff = _smoothed(sym)
        # 止损距离掉到出池线以下 → 移除（手续费要吃不消了）
        if stop is not None and stop < STOP_EXIT_PCT:
            continue
        # 趋势效率掉到出池线以下 → 移除（走成横盘了）
        # 2026-11-10 B 档：EFF_EXIT = 0 → 本闸关闭，不再按趋势效率踢人
        if EFF_EXIT > 0 and eff is not None and eff < EFF_EXIT:
            continue
        # 成交额 / 插针不达标 → 移除
        ok, _why = _judge(sym)
        if not ok:
            continue
        kept.append(sym)

    # 容量下调时 kept 也要截断，否则 size 改小后页面自相矛盾
    if len(kept) > MONITOR_SIZE:
        log.info("quality: 监控池容量下调 %d → %d，裁掉末位", len(kept), MONITOR_SIZE)
        kept = kept[:MONITOR_SIZE]

    # ② 未在池的：必须过三道闸门，按成交额降序
    fresh: list[tuple[str, float]] = []
    in_pool = set(kept)
    for sym in universe:
        if sym in in_pool:
            continue
        stop, eff = _smoothed(sym)
        if stop is None:
            continue                       # 还没采样到 → 不冒进
        if stop < STOP_MIN_PCT:
            continue                       # 止损太窄，手续费会吃掉 1R
        if EFF_MIN > 0:                    # EFF 闸开着才判（EFF_MIN=0 时整段跳过）
            if eff is None or eff < EFF_MIN:
                continue                   # 没数据 / 宽幅震荡，没方向
        ok, _why = _judge(sym)
        if not ok:
            continue
        r = _rows.get(sym) or {}
        fresh.append((sym, float(r.get("qv") or 0.0)))

    # ③ 排序：成交额高的在前（流动性好 = 滑点小 = 能真成交）
    fresh.sort(key=lambda x: -x[1])

    # ④ 容量：已留下的优先，剩余名额按成交额从高到低补
    picked = list(kept)
    for sym, _qv in fresh:
        if len(picked) >= MONITOR_SIZE:
            break
        picked.append(sym)
        _in_since.setdefault(sym, now)

    # ⑤ 清理已出局的状态
    keep_set = set(picked)
    for s in list(_in_since):
        if s not in keep_set:
            _in_since.pop(s, None)
    for s in list(_hist):
        if s not in keep_set and s not in now_set:
            _hist.pop(s, None)             # 彻底不在候选池了，丢历史

    _monitor = picked
    _ts = now
    if picked:
        READY.set()      # 池子有内容了，扫描循环可以开工
    log.info("monitor recomputed: %d 个，容量 %d（止损≥%.2f%% + EFF≥%.2f + 插针≤%.1f）",
             len(picked), MONITOR_SIZE, STOP_MIN_PCT, EFF_MIN, MAX_SPIKE)
    return picked


def get(symbol: str):
    """取单币质量明细。未采集到时返回 None。"""
    r = _rows.get(symbol)
    stop, eff = _smoothed(symbol)
    if not r and stop is None and eff is None:
        return None
    ok, why = _judge(symbol)
    return {"qv": (r or {}).get("qv"), "spike": (r or {}).get("spike"),
            "stop_pct": stop, "eff": eff,
            "ok": ok, "why": why}


def monitor_list() -> list[str]:
    """当前监控池。策略扫描以它为准，不扫全候选池。"""
    return list(_monitor)


def is_tradable(symbol: str):
    """(是否放行, 原因)。

    2026-11-09 改 fail-closed：之前监控池为空时**放行全部候选币**，注释写的是
    「避免因刷新失败误停整个系统」。但那等于——池子因为 bootstrap 失败 / yaml
    配错 / 全市场转横盘 而变空时，涨跌幅、成交额、趋势效率三道闸门加上池子
    成员资格**全部失效**，引擎会从 200 个币里随便开仓。风控静默下线的代价
    远大于「今天不交易」，所以改成空池直接拒绝。
    """
    if not _monitor and not _ts:
        # 从未算过（启动阶段）：池子还没准备好，不该开任何仓
        return False, "监控池尚未初始化"
    if not _monitor:
        # 算过但结果为空：所有候选都不合格，或刷新失败
        return False, "监控池为空"
    ok, why = _judge(symbol)
    if not ok:
        return False, why
    if symbol not in _monitor:
        return False, "未进监控池"
    return True, ""


def snapshot() -> dict:
    data = {}
    for s, r in _rows.items():
        ok, why = _judge(s)
        stop, eff = _smoothed(s)
        data[s] = {**r, "ok": ok, "why": why,
                   "stop_pct": stop, "eff": eff}
    return {
        "ts": _ts,
        "min_quote_vol": MIN_QUOTE_VOL,
        "max_spike": MAX_SPIKE,
        # 新方案（生效）
        "stop_min_pct": STOP_MIN_PCT,
        "stop_exit_pct": STOP_EXIT_PCT,
        "eff_min": EFF_MIN,
        "eff_exit": EFF_EXIT,
        "eff_win": EFF_WIN,
        "smooth_sec": SMOOTH_SEC,
        "kl_interval": KL_INTERVAL,
        "min_dwell_sec": MIN_DWELL_SEC,
        "size": MONITOR_SIZE,
        "refresh_sec": REFRESH_SEC,
        "data": data,
        "monitor": list(_monitor),
        "monitor_size": MONITOR_SIZE,
        "monitor_n": len(_monitor),
    }


# ── 主循环（每分钟增量刷新）───────────────────────────────

async def loop(universe_fn, cfg_fn=None):
    """后台循环。K 线每分钟增量拉取，插针每天一次。"""
    global _ts_daily
    while True:
        try:
            if cfg_fn is not None:
                configure(cfg_fn())
            universe = universe_fn()
            now = time.time()

            # 日线插针：每天一次
            if now - _ts_daily >= DAILY_REFRESH_SEC:
                await _refresh_daily(universe)

            # 24h 成交额：每轮（走 ticker，零成本）
            await _update_quote_vol(universe)

            # 首次：全量拉 96 根建缓存
            if not _bootstrap_done:
                await _bootstrap_kl(universe)

            # 每分钟：增量拉最新 2 根
            if now - _ts_kl >= REFRESH_SEC:
                await _refresh_kl_incr(universe)

            # 重算采样 + 名单
            _update_samples(universe)
            recompute(universe)

        except Exception as exc:  # noqa: BLE001
            log.warning("quality loop failed: %s", exc)
        await asyncio.sleep(TICK_SEC)
