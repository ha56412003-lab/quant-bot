"""监控池（三层池的第 2 层）—— 高波动准入（5m ATR > 1.20%）。

2026-11-06 架构改造（浩哥定稿，见 docs/架构_S15三模块.md）：

【为什么改】
  旧版用 1h ATR > 0.6% 单一门槛，选出来的全是大币慢币；
  而 S15 策略要 15m ATR > 2.5%，两套门槛打架 → 8 小时只出 1 个信号。
  实测（120天/14币/1150笔）发现 ATR 与收益不是线性关系：
      0~1.0%(15m)  赚钱（大币靠趋势，止损窄但胜率高）
      1.25~3.0%    亏钱（死亡区：波动够打穿止损，不够走到止盈）
      3.0~4.5%     大赚（野币靠波段）
  双档筛选（<1.0% 或 >=3.0%）净 +31.74R，且是唯一「前半段不亏」的方案。

【三个门槛各看什么周期】
  成交额      日线口径（走全市场 ticker，零额外成本）
  插针        日线口径（96 根 1h 内最大振幅 ÷ ATR）
  ATR         5 分钟（提前发现波动）

  布林带分位不在这里 —— 2026-11-06 移交给策略层按 15m 算（见 strategies.py
  的 _bollinger_width / _pct_rank）。池子层按 1h 算的那套已整套删除：
  1h BBW 近 100 根 = 100 小时（4 天），等它确认趋势早走完了，实测 -36.96R。

【分级刷新（约 138MB/天）】
  5m ATR  · 池内         每 5 分钟
  5m ATR  · 池外         每 15 分钟
  日线插针               每天
  24h 成交额             每轮（走 ticker）

【ATR 准入门槛（2026-11-06 晚 浩哥定，当日两次调整 1.20 → 0.40）】
  进池：5m ATR > 0.40%
  出池：进池满 30 分钟 + 期间没被选进持仓池 + ATR < 0.30% → 移除
  已进持仓池的币不受 ATR 影响（出场交给策略层）
  另外成交额 <1000万 或 插针 >4.0 也会被清退（与进池同一把尺子）
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
MONITOR_SIZE = 200      # 监控池容量（最多，不凑数）
# 2026-11-06 浩哥定：23 → 69。实测池子「合格但被容量挡」的有 47 个
# （pass_n=69 / monitor_n=22），这些币系统根本不扫，里面确实有 CE 翻转的信号
# （INIT / LIT / FLUID / FOGO 四个正在翻转却被挡在门外）。
# 代价：15m 扫描的 K 线流量 51MB/天 → 160MB/天（+109MB）。

# ATR 准入门槛（2026-11-06 晚 浩哥定）
#   进池：5m ATR > 1.20%  才进监控池
#   出池：进池满 30 分钟 + 期间没被选进持仓池 + ATR < 0.80% → 移除
# 已进持仓池的币不受 ATR 影响（策略层自己有出场规则，池子不干预）
ATR_ENTER_MIN = 0.40    # 进池线（%）：2026-11-06 晚 浩哥定 1.20 → 0.40
ATR_EXIT_MAX = 0.30     # 清退线（%）：必须低于进池线，否则刚进池就被踢
MIN_DWELL_SEC = 1800    # 进池后最短停留 30 分钟

ATR_INTERVAL = "5m"     # ATR 周期
ATR_N = 14              # ATR 回看根数
_RETRY_SEC = 600         # 整轮刷新全失败时的重试间隔（秒）
SPIKE_LOOKBACK = 100    # 插针：拉 100 根 1h（实际用最后 96 根）

# 分级刷新
REFRESH_SEC = 300          # 池内 5m ATR：5 分钟
SCAN_SEC = 900             # 池外 5m ATR：15 分钟
DAILY_REFRESH_SEC = 86400  # 日线插针：每天
TICK_SEC = 60              # 主循环节拍

FETCH_CONCURRENCY = 8   # 并发数（200 币串行要 200 秒，并发 8 约 25 秒）

# ── 状态 ────────────────────────────────────────────────────
_rows: dict[str, dict] = {}       # sym -> {atr_pct, spike, qv, ts_*}
_monitor: list[str] = []          # 当前监控池
_in_since: dict[str, float] = {}  # sym -> 进池时间戳

_ts_atr_pool = 0.0   # 池内 ATR 上次刷新
_ts_atr_scan = 0.0   # 池外 ATR 上次扫描
_ts_daily = 0.0      # 日线插针上次刷新
_ts = 0.0            # 监控池上次重算


def configure(cfg: dict | None = None) -> None:
    """从 strategies.yaml 的 pools.monitor 段热更新阈值（缺项保留原值）。"""
    global MIN_QUOTE_VOL, MAX_SPIKE, MONITOR_SIZE
    global MIN_DWELL_SEC, ATR_ENTER_MIN, ATR_EXIT_MAX
    global REFRESH_SEC, SCAN_SEC, DAILY_REFRESH_SEC, TICK_SEC
    cfg = cfg or {}

    def _f(k, cur):
        return float(cfg[k]) if k in cfg else cur

    def _i(k, cur):
        return int(cfg[k]) if k in cfg else cur

    MIN_QUOTE_VOL = _f("min_quote_vol", MIN_QUOTE_VOL)
    MAX_SPIKE = _f("max_spike", MAX_SPIKE)
    MONITOR_SIZE = _i("size", MONITOR_SIZE)
    MIN_DWELL_SEC = _i("min_dwell_sec", MIN_DWELL_SEC)
    ATR_ENTER_MIN = _f("atr_enter_min", ATR_ENTER_MIN)
    ATR_EXIT_MAX = _f("atr_exit_max", ATR_EXIT_MAX)
    REFRESH_SEC = _i("refresh_sec", REFRESH_SEC)
    SCAN_SEC = _i("scan_sec", SCAN_SEC)
    DAILY_REFRESH_SEC = _i("daily_refresh_sec", DAILY_REFRESH_SEC)
    TICK_SEC = _i("tick_sec", TICK_SEC)
    # atr_interval — ATR 用的 K 线周期
    # （bbw_interval / bbw_pct_min / bbw_refresh_sec 三个键 2026-11-06 已删除：
    #   BBW 整套移交策略层 15m，池子层不再采集也不再判断）
    global ATR_INTERVAL
    _atr_iv = cfg.get("atr_interval")
    if _atr_iv is not None:
        ATR_INTERVAL = str(_atr_iv)


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


# ── 准入判定 ────────────────────────────────────────────────
# 2026-11-06 晚：ATR 双档整套删除。
# 原意是「波动太小赚不到钱不要、波动太大容易插针不要」，实测两条都不成立：
#   · 波动小反而最赚钱：ATR<0.33% 那档每笔 +0.133R，是三档里最高的
#   · 插针已经有 max_spike 专门管，不需要 ATR 再管一遍
# 而且双档还带来一个真 bug：recompute 的出池判断只看 ATR、不看成交额/插针，
# 币一旦进池，成交额掉到 100 万也不会被踢出去（只要 ATR 不越线）。
# 现在池子的准入和出池都用同一把尺子：成交额 + 插针（_judge）。
# ATR 仍然照常采集（_refresh_atr 保留），只是不再决定进出池。


def _judge(sym: str) -> tuple[bool, str]:
    """单币的池子资格：成交额 + 插针。进出池用同一把尺子。

    （2026-11-06 前的 ATR 双档与 1h BBW 判断已删除：ATR 改由
     ATR_ENTER_MIN / ATR_EXIT_MAX 单线判断，BBW 移交策略层 15m。）
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


# ── 池子重算（双档 + 缓冲带 + 容量）─────────────────────────

def _held_symbols() -> set:
    """当前持仓池里的币。拿不到就当空集（fail-open：不因读库失败而误踢）。"""
    try:
        from . import db
        return {p["symbol"] for p in db.open_positions()}
    except Exception as exc:  # noqa: BLE001
        log.debug("quality: 读取持仓失败，按空集处理: %s", exc)
        return set()


def recompute(universe) -> list[str]:
    """根据当前数据重算监控池名单。返回新名单。"""
    global _monitor, _ts
    now = time.time()
    now_set = set(universe)

    # ① 已在池的：判断留不留
    held = _held_symbols()                 # 当前持仓池里的币
    kept: list[str] = []
    for sym in _monitor:
        if sym not in now_set:
            continue                       # 掉出候选池，自动出局
        # 缺失进池时间时按「现在」补上：否则 now - 0 是个巨大值，
        # 最短停留保护会反向失效（刚进池就被踢，3 分钟级反复进出）。
        _in_since.setdefault(sym, now)
        # 已被选进持仓池 → 池子不干预（出场规则交给策略层）
        if sym in held:
            kept.append(sym)
            continue
        # 进池未满 30 分钟 → 保护期内不因「变差」被踢
        if now - _in_since[sym] < MIN_DWELL_SEC:
            kept.append(sym)
            continue
        # —— 满 30 分钟且没被选进持仓池，才做清退判断 ——
        r = _rows.get(sym) or {}
        atr = r.get("atr_pct")
        # 清退规则：ATR 掉到 ATR_EXIT_MAX（0.30%）以下 → 移除
        if atr is not None and atr < ATR_EXIT_MAX:
            continue
        # 成交额 / 插针不达标 → 移除
        ok, _why = _judge(sym)
        if not ok:
            continue
        kept.append(sym)

    # 容量下调时 kept 也要截断，否则 size 从 23 改成 20 后
    # 页面会出现 monitor_n=23 / monitor_size=20 的自相矛盾。
    if len(kept) > MONITOR_SIZE:
        log.info("quality: 监控池容量下调 %d → %d，裁掉末位", len(kept), MONITOR_SIZE)
        kept = kept[:MONITOR_SIZE]

    # ② 未在池的：必须 5m ATR > ATR_ENTER_MIN（0.40%）+ 过 _judge，按成交额降序
    fresh: list[tuple[str, float]] = []
    in_pool = set(kept)
    for sym in universe:
        if sym in in_pool:
            continue
        r = _rows.get(sym) or {}
        atr = r.get("atr_pct")
        if atr is None or atr <= ATR_ENTER_MIN:
            continue                       # 没数据、或波动不够 → 不进池
        ok, _why = _judge(sym)
        if not ok:
            continue
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

    _monitor = picked
    _ts = now
    if picked:
        READY.set()      # 池子有内容了，扫描循环可以开工
    log.info("monitor recomputed: %d 个，容量 %d（成交额+插针 双门槛）",
             len(picked), MONITOR_SIZE)
    return picked


async def refresh(universe) -> int:
    """兼容旧接口：全量刷一次（ATR + 日线插针）后重算名单。"""
    await _update_quote_vol(universe)
    await _refresh_atr(universe, only=None)
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
            "spike": r.get("spike"), "ok": ok, "why": why}


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
        data[s] = {**r, "ok": ok, "why": why}
    return {
        "ts": _ts,
        "min_quote_vol": MIN_QUOTE_VOL,
        "max_spike": MAX_SPIKE,
        "atr_interval": ATR_INTERVAL,
        "atr_enter_min": ATR_ENTER_MIN,
        "atr_exit_max": ATR_EXIT_MAX,
        "min_dwell_sec": MIN_DWELL_SEC,
        "size": MONITOR_SIZE,
        "refresh_sec": REFRESH_SEC,
        "scan_sec": SCAN_SEC,
        "data": data,
        "monitor": list(_monitor),
        "monitor_size": MONITOR_SIZE,
        "monitor_n": len(_monitor),
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

            # 重算名单
            recompute(universe)

        except Exception as exc:  # noqa: BLE001
            log.warning("quality loop failed: %s", exc)
        await asyncio.sleep(TICK_SEC)
