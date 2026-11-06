"""监控池（三层池的第 2 层）—— 双档 ATR 多周期共振。

2026-11-06 架构改造（浩哥定稿，见 docs/架构_S15三模块.md）：

【为什么改】
  旧版用 1h ATR > 0.6% 单一门槛，选出来的全是大币慢币；
  而 S15 策略要 15m ATR > 2.5%，两套门槛打架 → 8 小时只出 1 个信号。
  实测（120天/14币/1150笔）发现 ATR 与收益不是线性关系：
      0~1.0%(15m)  赚钱（大币靠趋势，止损窄但胜率高）
      1.25~3.0%    亏钱（死亡区：波动够打穿止损，不够走到止盈）
      3.0~4.5%     大赚（野币靠波段）
  双档筛选（<1.0% 或 >=3.0%）净 +31.74R，且是唯一「前半段不亏」的方案。

【四个门槛各看什么周期】
  成交额      日线口径（走全市场 ticker，零额外成本）
  插针        日线口径（96 根 1h 内最大振幅 ÷ ATR）
  ATR         5 分钟（小一级，提前发现波动）
  布林带分位  1 小时（大一级，判断趋势环境成不成立）

【分级刷新（约 99MB/天；全量 3 分钟刷新是 288MB）】
  5m ATR  · 池内         每 3 分钟
  5m ATR  · 池外         每 15 分钟
  1h BBW                 每小时（只算 ATR 已过双档的币）
  日线插针               每天
  24h 成交额             每轮（走 ticker）

【缓冲带（防池子抖动）】
  低档：进 <0.33%，留 <0.40%，踢 >=0.45%
  高档：进 >=1.20%，留 >=1.10%，踢 <1.00%
  进池后 30 分钟内不因「变差」被踢（跌穿硬底线除外）
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from . import binance

log = logging.getLogger(__name__)

# 首次池子算完之前，扫描循环不该开工 —— 否则 monitor_list() 为空，
# 策略会回落到全量候选池 200 个币，白拉一堆 K 线还灌噪音信号。
READY = threading.Event()

# ── 默认值（可被 strategies.yaml 的 pools.monitor 段覆盖）──────
MIN_QUOTE_VOL = 1e7     # 24h 成交额下限：1000 万 U
MAX_SPIKE = 4.0         # 单根最大振幅 ÷ ATR 上限
MONITOR_SIZE = 23       # 监控池容量（最多，不凑数）

# 双档 ATR（5 分钟口径）
ATR_LO_MAX = 0.33       # 低档上界
ATR_HI_MIN = 1.20       # 高档下界
ATR_LO_HOLD = 0.40      # 低档留池缓冲
ATR_LO_EXIT = 0.45      # 低档硬踢线
ATR_HI_HOLD = 1.10      # 高档留池缓冲
ATR_HI_EXIT = 1.00      # 高档硬踢线
MIN_DWELL_SEC = 1800    # 进池后最短停留 30 分钟

ATR_INTERVAL = "5m"     # ATR 周期
ATR_N = 14              # ATR 回看根数
BBW_INTERVAL = "1h"     # 布林带周期（仅表示 BBW 采集周期；K 线请求周期已写死 1h）
_RETRY_SEC = 600         # 整轮刷新全失败时的重试间隔（秒）
BBW_LEN = 20            # 布林带长度
BBW_MULT = 2.0          # 布林带倍数
BBW_LOOKBACK = 100      # 分位回看根数
BBW_PCT_MIN = 70.0      # 分位门槛

# 分级刷新
REFRESH_SEC = 180          # 池内 5m ATR：3 分钟
SCAN_SEC = 900             # 池外 5m ATR：15 分钟
BBW_REFRESH_SEC = 3600     # 1h 布林带：每小时
DAILY_REFRESH_SEC = 86400  # 日线插针：每天
TICK_SEC = 60              # 主循环节拍

FETCH_CONCURRENCY = 8   # 并发数（200 币串行要 200 秒，并发 8 约 25 秒）

# ── 状态 ────────────────────────────────────────────────────
_rows: dict[str, dict] = {}       # sym -> {atr_pct, bbw_pct, spike, qv, ts_*}
_monitor: list[str] = []          # 当前监控池
_band_of: dict[str, str] = {}     # sym -> "lo" / "hi"（进池时属于哪一档）
_in_since: dict[str, float] = {}  # sym -> 进池时间戳

_ts_atr_pool = 0.0   # 池内 ATR 上次刷新
_ts_atr_scan = 0.0   # 池外 ATR 上次扫描
_ts_bbw = 0.0        # BBW 上次刷新
_ts_daily = 0.0      # 日线上次刷新
_ts = 0.0            # 监控池上次重算


def configure(cfg: dict | None = None) -> None:
    """从 strategies.yaml 的 pools.monitor 段热更新阈值（缺项保留原值）。"""
    global MIN_QUOTE_VOL, MAX_SPIKE, MONITOR_SIZE
    global ATR_LO_MAX, ATR_HI_MIN, ATR_LO_HOLD, ATR_LO_EXIT, ATR_HI_HOLD, ATR_HI_EXIT
    global MIN_DWELL_SEC, BBW_PCT_MIN
    global REFRESH_SEC, SCAN_SEC, BBW_REFRESH_SEC, DAILY_REFRESH_SEC, TICK_SEC
    cfg = cfg or {}

    def _f(k, cur):
        return float(cfg[k]) if k in cfg else cur

    def _i(k, cur):
        return int(cfg[k]) if k in cfg else cur

    MIN_QUOTE_VOL = _f("min_quote_vol", MIN_QUOTE_VOL)
    MAX_SPIKE = _f("max_spike", MAX_SPIKE)
    MONITOR_SIZE = _i("size", MONITOR_SIZE)
    ATR_LO_MAX = _f("atr_band_lo_max", ATR_LO_MAX)
    ATR_HI_MIN = _f("atr_band_hi_min", ATR_HI_MIN)
    ATR_LO_HOLD = _f("atr_lo_hold", ATR_LO_HOLD)
    ATR_LO_EXIT = _f("atr_lo_exit", ATR_LO_EXIT)
    ATR_HI_HOLD = _f("atr_hi_hold", ATR_HI_HOLD)
    ATR_HI_EXIT = _f("atr_hi_exit", ATR_HI_EXIT)
    MIN_DWELL_SEC = _i("min_dwell_sec", MIN_DWELL_SEC)
    BBW_PCT_MIN = _f("bbw_pct_min", BBW_PCT_MIN)
    REFRESH_SEC = _i("refresh_sec", REFRESH_SEC)
    SCAN_SEC = _i("scan_sec", SCAN_SEC)
    BBW_REFRESH_SEC = _i("bbw_refresh_sec", BBW_REFRESH_SEC)
    DAILY_REFRESH_SEC = _i("daily_refresh_sec", DAILY_REFRESH_SEC)
    TICK_SEC = _i("tick_sec", TICK_SEC)
    # 2026-11-06 自检修复：这三个配置此前只写不读（configure 漏读），
    # 改 yaml 会静默失效。现在真正生效：
    #   atr_interval — ATR 用的 K 线周期
    #   bbw_interval — 布林带周期；写 "off" 表示连采集都不做（双保险，
    #                  另一个开关是 bbw_pct_min=0）
    #   sort_by      — 仅描述性（容量排序固定为低档取最低、高档取最高），
    #                  从 yaml 移除，此处不再读取
    global ATR_INTERVAL, BBW_INTERVAL
    # 注意 YAML 坑：裸写的 off/on/yes/no 会被解析成布尔 False/True，
    # 所以这里要显式处理 False 的情况（yaml 里建议写带引号的 "off"）。
    _atr_iv = cfg.get("atr_interval")
    if _atr_iv is not None:
        ATR_INTERVAL = str(_atr_iv)
    _bbw_iv = cfg.get("bbw_interval")
    if _bbw_iv is False:
        BBW_INTERVAL = "off"
    elif _bbw_iv is not None:
        BBW_INTERVAL = str(_bbw_iv)


# ── 指标计算 ────────────────────────────────────────────────

def _atr(bars, n: int = ATR_N):
    """Wilder ATR（与回测 backtest/s15_final.py 的 _rma 口径一致）。"""
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


def _bbw_pct(bars, length: int = BBW_LEN, mult: float = BBW_MULT,
             lookback: int = BBW_LOOKBACK):
    """布林带宽度在近 lookback 根中的百分位（0~100）。

    当前值用最后一根（可以是未收盘的，代表「此刻」）；
    分位基数只取它之前的已收盘根，避免自己跟自己比。
    """
    n = len(bars)
    if n < length + 2:
        return None
    widths = []
    for i in range(length - 1, n):
        seg = [b["close"] for b in bars[i - length + 1: i + 1]]
        mid = sum(seg) / length
        if not mid:
            widths.append(None)
            continue
        var = sum((x - mid) ** 2 for x in seg) / length
        sd = var ** 0.5
        widths.append((2 * mult * sd) / mid * 100.0)
    cur = widths[-1]
    hist = [w for w in widths[-lookback - 1:-1] if w is not None]
    if cur is None or len(hist) < 10:
        return None
    below = sum(1 for w in hist if w < cur)
    return below / len(hist) * 100.0


# ── 数据采集 ────────────────────────────────────────────────

async def _fetch_atr(sym: str):
    """5 分钟 ATR% —— 只拉 20 根，够算 ATR14，省流量。"""
    try:
        bars = await binance.klines(sym, ATR_INTERVAL, ATR_N + 6)
        if len(bars) < ATR_N + 1:
            return None
        a = _atr(bars, ATR_N)
        px = bars[-1]["close"]
        if not a or not px:
            return None
        return a / px * 100.0
    except Exception as exc:  # noqa: BLE001
        log.debug("atr fetch %s failed: %s", sym, exc)
        return None


async def _fetch_1h(sym: str, need_spike: bool, need_bbw: bool):
    """1 小时 K 线：一次拉 100 根，同时算插针和布林带分位。

    ⚠ 这里的周期必须写死 "1h"，不能再用 BBW_INTERVAL。
    2026-11-06 踩过的坑：为了省流量把 yaml 的 bbw_interval 设成 "off"
    （本意只是"别采布林带"），结果这个变量同时被当成 klines 的 interval
    传给币安 → 请求 interval=off → 400 → 整个函数返回 {} → 插针值算不出来
    → 「插针 ≤4.0」这道池子门槛静默失效（日志只显示 refreshed for 0 symbols，
    一条报错都没有，异常被 log.debug 吞掉）。
    BBW 采不采由调用方的 need_bbw 控制，与 K 线周期是两件事。
    """
    try:
        bars = await binance.klines(sym, "1h", BBW_LOOKBACK)
        if len(bars) < 96:
            return {}
        out = {}
        a = _atr(bars)
        if need_spike and a:
            out["spike"] = max(b["high"] - b["low"] for b in bars[-96:]) / a
        if need_bbw:
            v = _bbw_pct(bars)
            if v is not None:
                out["bbw_pct"] = v
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("1h fetch %s failed: %s", sym, exc)
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


# ── 双档判定 ────────────────────────────────────────────────

def _band_new(atr_pct):
    """新进池：落在哪一档？死亡区返回 None。"""
    if atr_pct is None:
        return None
    if atr_pct < ATR_LO_MAX:
        return "lo"
    if atr_pct >= ATR_HI_MIN:
        return "hi"
    return None


def _band_keep(atr_pct, band: str) -> bool:
    """已在池：还在这档的缓冲带内就留下。"""
    if atr_pct is None:
        return True   # 数据缺失时不让它掉出去（fail-open）
    if band == "lo":
        return atr_pct < ATR_LO_HOLD
    if band == "hi":
        return atr_pct >= ATR_HI_HOLD
    return False


def _band_force_exit(atr_pct, band: str) -> bool:
    """硬底线：跌穿就立刻踢，无视最短停留。"""
    if atr_pct is None:
        return False
    if band == "lo":
        return atr_pct >= ATR_LO_EXIT
    if band == "hi":
        return atr_pct < ATR_HI_EXIT
    return True


def _judge(sym: str) -> tuple[bool, str]:
    """单币的日线门槛 + BBW（ATR 双档另算）。"""
    r = _rows.get(sym) or {}
    why = []
    qv = r.get("qv")
    if qv is not None and qv < MIN_QUOTE_VOL:
        why.append("量小")
    spike = r.get("spike")
    if spike is not None and spike > MAX_SPIKE:
        why.append("易插针")
    bbw = r.get("bbw_pct")
    if bbw is not None and bbw < BBW_PCT_MIN:
        why.append("无趋势")
    return (not why), ",".join(why)


# ── 刷新流程 ────────────────────────────────────────────────

async def _gather(syms, fn):
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def _one(s):
        async with sem:
            r = await fn(s)
            await asyncio.sleep(0.03)   # 轻微错峰，避免瞬时打满权重
            return s, r

    return await asyncio.gather(*[_one(s) for s in syms])


async def _refresh_atr(universe, only=None) -> int:
    """刷 5m ATR。only=None 表示扫全部候选币，否则只刷指定名单。"""
    global _ts_atr_pool, _ts_atr_scan
    targets = list(universe) if only is None else list(only)
    if not targets:
        return 0
    res = await _gather(targets, _fetch_atr)
    n = 0
    for sym, v in res:
        if v is None:
            continue
        row = _rows.setdefault(sym, {})
        row["atr_pct"] = v
        row["ts_atr"] = time.time()
        n += 1
    # 2026-11-06：整轮失败（n==0）不能更新时间戳，否则会把失败当成成功、
    # 静默空窗一整个周期（池外 15 分钟 / 池内 3 分钟）。但又不能完全不更新
    # 时间戳——那样每个 tick（0.5s）都会重试，直接打爆权重。折中：退避 10 分钟。
    if n == 0:
        retry_at = time.time() - (SCAN_SEC if only is None else REFRESH_SEC) + _RETRY_SEC
        if only is None:
            _ts_atr_scan = retry_at
        else:
            _ts_atr_pool = retry_at
        log.warning("quality atr: 本轮 %d 个全部失败（%s），%ds 后重试",
                    len(targets), "全池" if only is None else "池内", _RETRY_SEC)
    elif only is None:
        _ts_atr_scan = time.time()
    else:
        _ts_atr_pool = time.time()
    return n


async def _refresh_bbw(syms) -> int:
    global _ts_bbw
    if not syms:
        return 0
    res = await _gather(list(syms), lambda s: _fetch_1h(s, need_spike=False, need_bbw=True))
    n = 0
    for sym, d in res:
        if not d:
            continue
        row = _rows.setdefault(sym, {})
        row.update(d)
        row["ts_bbw"] = time.time()
        n += 1
    if n == 0:
        _ts_bbw = time.time() - BBW_REFRESH_SEC + _RETRY_SEC
        log.warning("quality bbw: 本轮 %d 个全部失败，%ds 后重试", len(syms), _RETRY_SEC)
    else:
        _ts_bbw = time.time()
    return n


async def _refresh_daily(universe) -> int:
    """日线插针：全候选池每天一次。"""
    global _ts_daily
    res = await _gather(list(universe),
                        lambda s: _fetch_1h(s, need_spike=True, need_bbw=False))
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


# ── 池子重算（双档 + 缓冲带 + 容量）─────────────────────────

def recompute(universe) -> list[str]:
    """根据当前数据重算监控池名单。返回新名单。"""
    global _monitor, _ts
    now = time.time()
    now_set = set(universe)

    # ① 已在池的：按缓冲带判断留不留
    kept: list[str] = []
    for sym in _monitor:
        if sym not in now_set:
            continue                       # 掉出候选池，自动出局
        # 缺失进池时间时按「现在」补上：否则 now - 0 是个巨大值，
        # 最短停留保护会反向失效（刚进池就被踢，3 分钟级反复进出）。
        _in_since.setdefault(sym, now)
        r = _rows.get(sym) or {}
        atr = r.get("atr_pct")
        # 档位缺失时按当前 ATR 推断，别无条件默认 "lo" ——
        # 高档币被当成低档判断会被硬底线瞬间踢出。
        band = _band_of.get(sym) or _band_new(atr) or "lo"
        _band_of[sym] = band
        if _band_force_exit(atr, band):
            continue                       # 硬底线，立刻踢
        if _band_keep(atr, band) or now - _in_since[sym] < MIN_DWELL_SEC:
            kept.append(sym)               # 缓冲带内 or 最短停留保护

    # 容量下调时 kept 也要截断，否则 size 从 23 改成 20 后
    # 页面会出现 monitor_n=23 / monitor_size=20 的自相矛盾。
    if len(kept) > MONITOR_SIZE:
        log.info("quality: 监控池容量下调 %d → %d，裁掉末位", len(kept), MONITOR_SIZE)
        kept = kept[:MONITOR_SIZE]

    # ② 未在池的：按双档门槛找新的
    fresh_lo: list[tuple[str, float]] = []
    fresh_hi: list[tuple[str, float]] = []
    in_pool = set(kept)
    for sym in universe:
        if sym in in_pool:
            continue
        r = _rows.get(sym) or {}
        atr = r.get("atr_pct")
        if atr is None:
            continue                       # 没数据不进池（宁缺毋滥）
        ok, _why = _judge(sym)
        if not ok:
            continue
        if BBW_PCT_MIN > 0 and r.get("bbw_pct") is None:
            continue                       # BBW 门槛启用时，必须先采到数据才放行
        band = _band_new(atr)
        if band == "lo":
            fresh_lo.append((sym, atr))
        elif band == "hi":
            fresh_hi.append((sym, atr))

    # ③ 排序：低档取 ATR 最低的（最稳），高档取 ATR 最高的（最野）
    fresh_lo.sort(key=lambda x: x[1])
    fresh_hi.sort(key=lambda x: -x[1])

    # ④ 容量：已留下的优先，剩余名额两档交替补（保证两头都有人）
    picked = list(kept)
    i = j = 0
    while len(picked) < MONITOR_SIZE and (i < len(fresh_lo) or j < len(fresh_hi)):
        if i < len(fresh_lo):
            sym = fresh_lo[i][0]
            picked.append(sym)
            _band_of[sym] = "lo"
            _in_since.setdefault(sym, now)
            i += 1
        if len(picked) >= MONITOR_SIZE:
            break
        if j < len(fresh_hi):
            sym = fresh_hi[j][0]
            picked.append(sym)
            _band_of[sym] = "hi"
            _in_since.setdefault(sym, now)
            j += 1

    # ⑤ 清理已出局的状态
    keep_set = set(picked)
    for s in list(_in_since):
        if s not in keep_set:
            _in_since.pop(s, None)
            _band_of.pop(s, None)

    _monitor = picked
    _ts = now
    if picked:
        READY.set()      # 池子有内容了，扫描循环可以开工
    log.info("monitor recomputed: %d 个（低档 %d / 高档 %d），容量 %d",
             len(picked),
             sum(1 for s in picked if _band_of.get(s) == "lo"),
             sum(1 for s in picked if _band_of.get(s) == "hi"),
             MONITOR_SIZE)
    return picked


async def refresh(universe) -> int:
    """兼容旧接口：全量刷一次（ATR + BBW + 日线）后重算名单。"""
    await _update_quote_vol(universe)
    await _refresh_atr(universe, only=None)
    ok_syms = [s for s in universe
               if _band_new(_rows.get(s, {}).get("atr_pct")) is not None]
    if BBW_PCT_MIN > 0:
        await _refresh_bbw(ok_syms)
    await _refresh_daily(universe)
    recompute(universe)
    return len(_rows)


# ── 对外接口 ────────────────────────────────────────────────

def get(symbol: str):
    """取单币质量明细。未采集到时返回 None。"""
    r = _rows.get(symbol)
    if not r:
        return None
    ok, why = _judge(symbol)
    return {"qv": r.get("qv"), "atr_pct": r.get("atr_pct"),
            "spike": r.get("spike"), "bbw_pct": r.get("bbw_pct"),
            "band": _band_of.get(symbol), "ok": ok, "why": why}


def monitor_list() -> list[str]:
    """当前监控池。策略扫描以它为准，不扫全候选池。"""
    return list(_monitor)


def is_tradable(symbol: str):
    """(是否放行, 原因)。数据缺失时放行，避免因刷新失败误停整个系统。"""
    ok, why = _judge(symbol)
    if not ok:
        return False, why
    if _monitor and symbol not in _monitor:
        return False, "未进监控池"
    return True, ""


def snapshot() -> dict:
    data = {}
    for s, r in _rows.items():
        ok, why = _judge(s)
        data[s] = {**r, "band": _band_of.get(s), "ok": ok, "why": why}
    return {
        "ts": _ts,
        "min_quote_vol": MIN_QUOTE_VOL,
        "max_spike": MAX_SPIKE,
        "atr_band_lo_max": ATR_LO_MAX,
        "atr_band_hi_min": ATR_HI_MIN,
        "atr_interval": ATR_INTERVAL,
        "bbw_interval": BBW_INTERVAL,
        "bbw_pct_min": BBW_PCT_MIN,
        "size": MONITOR_SIZE,
        "refresh_sec": REFRESH_SEC,
        "scan_sec": SCAN_SEC,
        "data": data,
        "monitor": list(_monitor),
        "monitor_size": MONITOR_SIZE,
        "monitor_n": len(_monitor),
        "bands": {s: _band_of.get(s) for s in _monitor},
    }


# ── 主循环（分级刷新）──────────────────────────────────────

async def loop(universe_fn, cfg_fn=None):
    """后台循环。不同数据源按各自节奏刷新。"""
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

            # 池外 5m ATR：15 分钟一次
            if now - _ts_atr_scan >= SCAN_SEC:
                await _refresh_atr(universe, only=None)

            # 池内 5m ATR：3 分钟一次
            if now - _ts_atr_pool >= REFRESH_SEC and _monitor:
                await _refresh_atr(universe, only=list(_monitor))

            # BBW 采集（2026-11-06 起默认关闭：门槛已归策略层 15m，
            # 池子不再采集能省 11MB/天）。
            # 双开关：bbw_pct_min>0 且 bbw_interval 不是 "off" 才采集。
            if (BBW_PCT_MIN > 0 and str(BBW_INTERVAL).lower() != "off"
                    and now - _ts_bbw >= BBW_REFRESH_SEC):
                cand = [s for s in universe
                        if _band_new(_rows.get(s, {}).get("atr_pct")) is not None]
                await _refresh_bbw(cand)

            # 重算名单
            recompute(universe)

        except Exception as exc:  # noqa: BLE001
            log.warning("quality loop failed: %s", exc)
        await asyncio.sleep(TICK_SEC)
