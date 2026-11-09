"""Configuration: .env credentials + strategies.yaml (runtime-editable).

strategies.yaml is the single source of truth for strategy switches / params /
per-strategy symbols / per-strategy risk + the global monitored universe.
Credentials live in .env and are editable at runtime via the Config page.
"""
import os
import threading
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
STRATEGIES_PATH = ROOT / "strategies.yaml"
DB_PATH = os.environ.get("QB_DB_PATH", str(ROOT / "quantbot.db"))

_lock = threading.Lock()
_env_lock = threading.Lock()


def _load_env():
    if not ENV_PATH.exists():
        return
    # 必须显式 utf-8：.env 里有中文注释，Windows 默认 GBK 会 UnicodeDecodeError 直接崩启动
    try:
        raw = ENV_PATH.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raw = ENV_PATH.read_text(encoding="utf-8", errors="replace")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

# credentials — module globals; read at call-time by clawby/binance so runtime
# edits (via save_credentials) take effect without restart
CLAWBY_API_KEY = os.environ.get("CLAWBY_API_KEY", "")
CLAWBY_BASE = os.environ.get("CLAWBY_BASE", "https://api.openclawby.com")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

DEFAULT_UNIVERSE = [s.strip() for s in os.environ.get(
    "QB_UNIVERSE", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT").split(",") if s.strip()]
UNIVERSE = list(DEFAULT_UNIVERSE)   # reassigned from strategies.yaml on load

PAPER_START_BALANCE = float(os.environ.get("QB_PAPER_BALANCE", "10000"))

# ── API 访问控制 ─────────────────────────────────────────────────
# Host 白名单：挡 DNS rebinding —— 攻击者把自己的域名解析到 127.0.0.1，
# 浏览器认为同源（Sec-Fetch-Site: same-origin），但 Host 头是攻击者域名。
# 上云服务器时必须把公网域名/IP 加进 QB_API_ALLOWED_HOSTS，否则全部 403。
API_ALLOWED_HOSTS = [h.strip().lower() for h in os.environ.get(
    "QB_API_ALLOWED_HOSTS", "127.0.0.1,localhost,::1").split(",") if h.strip()]

# 写操作 token：留空则不校验（本机用）。暴露公网时必须设置。
API_TOKEN = os.environ.get("QB_API_TOKEN", "").strip()
PAPER_FEE_RATE = 0.0005      # taker
PAPER_SLIPPAGE = 0.0003

ENGINE_TICK_SEC = 30  # legacy, loops now run at 0.5s (engine.LOOP_SEC)

# ── 测试取样起点（影子模式 / 观察期统计的时间下界）─────────────────
# 浩哥 2026-11-09 定：之前攒的样本受多轮参数改动污染（池子 ATR、风控、两闸
# 都在这期间改过），跨版本比较没有意义。从这一刻起重新起算，样本干净。
# 改这里 → 影子面板、观察期报告同时生效，不用改两处。
# 格式 "%Y-%m-%d %H:%M"；留空表示不设下界（统计全部历史）。
SAMPLE_START_STR = os.environ.get("QB_SAMPLE_START", "2026-11-09 12:00")

def _parse_sample_start(s):
    if not s:
        return None
    try:
        import datetime as _dt
        return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        return None

SAMPLE_START = _parse_sample_start(SAMPLE_START_STR)
SAMPLE_START_TS = SAMPLE_START.timestamp() if SAMPLE_START else None

# common futures symbols offered in the universe picker (extend freely)
KNOWN_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "TONUSDT", "TRXUSDT", "DOTUSDT",
    "POLUSDT", "LTCUSDT", "BCHUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "SUIUSDT", "INJUSDT", "SEIUSDT", "TIAUSDT", "1000PEPEUSDT",
    "WIFUSDT", "1000SHIBUSDT", "ORDIUSDT", "FILUSDT", "ATOMUSDT", "UNIUSDT",
]

DEFAULT_STRATEGY_RISK = {
    "capital_usd": 0,        # 0 = 用全账户净值参与计算
    "leverage": 3,           # 该策略下单杠杆
    "max_position_usd": 0,   # 单笔名义上限,0 = capital×leverage
    "risk_per_trade_pct": 0, # 单笔风险占资金 %,0 = 用全局
}


# -- strategies.yaml (with migration) ---------------------------------------

def _migrate(cfg):
    """Ensure global.universe + per-strategy symbols/risk exist. Returns
    (cfg, changed)."""
    changed = False
    g = cfg.setdefault("global", {})
    if "daily_target" not in g:
        g["daily_target"] = {
            "enabled": False, "target_usd": 200, "sustain_sec": 300,
            "auto_close_sec": 1800, "pause_new": True, "notify": True}
    if "universe" not in g or not g["universe"]:
        g["universe"] = list(DEFAULT_UNIVERSE)
        changed = True
    for sid, scfg in cfg.get("strategies", {}).items():
        if "symbols" not in scfg:
            scfg["symbols"] = []        # empty = follow global universe
            changed = True
        risk = scfg.setdefault("risk", {})
        for k, v in DEFAULT_STRATEGY_RISK.items():
            if k not in risk:
                risk[k] = v
                changed = True
    return cfg, changed


def load_strategies():
    with _lock:
        cfg = yaml.safe_load(STRATEGIES_PATH.read_text(encoding="utf-8"))
    cfg, changed = _migrate(cfg)
    if changed:
        _write(cfg)
    # keep the runtime universe in sync with persisted config
    global UNIVERSE
    uni = cfg.get("global", {}).get("universe")
    if uni:
        UNIVERSE = list(uni)
    # 候选池优先：pool.load() 已把每天筛出的 100 只读到内存，有则用它替换 universe
    # （延迟导入，避免 config ← binance ← config 循环）
    try:
        from . import pool as _pool
        cand = _pool.symbols()
        if cand:
            UNIVERSE = list(cand)
    except Exception:  # noqa: BLE001
        pass
    return cfg


def pools() -> dict:
    """三层池配置（strategies.yaml 的 pools 段），缺段时返回空字典。"""
    try:
        with _lock:
            cfg = yaml.safe_load(STRATEGIES_PATH.read_text(encoding="utf-8")) or {}
        return cfg.get("pools") or {}
    except Exception:  # noqa: BLE001
        return {}


def _write(cfg):
    with _lock:
        STRATEGIES_PATH.write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


def save_strategies(cfg):
    _write(cfg)


def _update_strategy(sid, mutate):
    cfg = load_strategies()
    if sid not in cfg.get("strategies", {}):
        raise KeyError(sid)
    mutate(cfg["strategies"][sid])
    save_strategies(cfg)
    return cfg


def set_strategy_enabled(sid, enabled):
    return _update_strategy(sid, lambda s: s.update(enabled=bool(enabled)))


def set_strategy_params(sid, params):
    """Merge param edits (numbers/bools/strings only) into the instance."""
    def mutate(s):
        blk = s.setdefault("params", {})
        for k, v in (params or {}).items():
            if isinstance(v, (int, float, bool, str)):
                blk[str(k)] = v
    return _update_strategy(sid, mutate)


def set_strategy_display_name(sid, name):
    return _update_strategy(sid, lambda s: s.update(display_name=str(name)[:60]))


def create_strategy_instance(base, name=""):
    """Clone a template's config into a new instance (enabled=false)."""
    cfg = load_strategies()
    strategies = cfg.setdefault("strategies", {})
    template = None
    for iid, scfg in strategies.items():
        if (scfg.get("base") or iid) == base:
            template = scfg
            break
    if template is None:
        raise KeyError(base)
    n = 2
    while f"{base}__{n}" in strategies:
        n += 1
    iid = f"{base}__{n}"
    import copy
    inst = copy.deepcopy(template)
    inst["base"] = base
    inst["enabled"] = False
    if name:
        inst["display_name"] = str(name)[:60]
    strategies[iid] = inst
    save_strategies(cfg)
    return iid


def delete_strategy_instance(iid):
    cfg = load_strategies()
    if iid not in cfg.get("strategies", {}):
        raise KeyError(iid)
    del cfg["strategies"][iid]
    save_strategies(cfg)


def set_strategy_interval(sid, interval_sec):
    return _update_strategy(sid, lambda s: s.update(scan_interval=max(1, int(interval_sec))))


def set_strategy_symbols(sid, symbols):
    syms = [str(x).upper() for x in (symbols or [])]
    allowed = set(UNIVERSE) | set(KNOWN_SYMBOLS)
    bad = [s for s in syms if s not in allowed]
    if bad:
        raise ValueError(f"未知交易币种: {','.join(bad[:5])}(需在全局监控或已知币列表内)")
    return _update_strategy(sid, lambda s: s.update(symbols=syms))


def set_strategy_risk(sid, risk):
    def mutate(s):
        blk = s.setdefault("risk", dict(DEFAULT_STRATEGY_RISK))
        for k in DEFAULT_STRATEGY_RISK:
            if k in risk and risk[k] is not None:
                blk[k] = float(risk[k]) if k != "leverage" else int(risk[k])
    return _update_strategy(sid, mutate)


_GLOBAL_INT_KEYS = ("max_gross_leverage", "max_concurrent_coins", "max_same_side",
                    "event_quiet_minutes")
_GLOBAL_FLOAT_KEYS = ("daily_loss_halt_pct", "risk_per_trade_pct", "max_position_pct_per_coin",
                      "unrealized_halt_pct")

# 风控参数硬边界（2026-11-09）。前端 InputNumber 的 min/max 只影响体验，
# 接口可以被 curl 直接调用 / 也可被脚本写坏，所以必须在写盘前挡。
# 下限都不是 0：0 在多数参数上等于「关掉这道风控」或「除以 0」。
_RISK_BOUNDS = {
    "risk_per_trade_pct": (0.1, 10.0),      # 每单亏净值百分比
    "max_gross_leverage": (1, 20),          # 总杠杆上限
    "max_position_pct_per_coin": (1.0, 100.0),  # 单币名义上限
    "max_concurrent_coins": (1, 20),        # 同时持仓币数
    "max_same_side": (1, 20),               # 同方向最多几笔
    "daily_loss_halt_pct": (1.0, 50.0),     # 日内熔断线（已实现）
    "event_quiet_minutes": (0, 240),        # 事件静默窗口（0 = 不静默，允许）
    # 浮亏闸（第一级）：只停开新仓，不动已有仓位。0 = 关闭这一级。
    # 必须小于 daily_loss_halt_pct，否则永远轮不到它出手。
    "unrealized_halt_pct": (0.0, 50.0),
}
_DAILY_TARGET_KEYS = ("enabled", "target_usd", "sustain_sec", "auto_close_sec",
                      "pause_new", "notify")


def set_global_risk(updates):
    """把全局风控参数写回 strategies.yaml。

    ⚠ 2026-11-05 修复：这个函数原来是个空壳 —— 第一个 for 循环只有 `pass`，
    第二个 for 循环遍历空元组 `()`，一次都不执行。结果前端风控页无论改什么
    （熔断线、每单风险、并发数、杠杆），接口都返回 {"ok": true}，
    yaml 里却一个字节都没变。改风控等于没改。

    ⚠ 2026-11-09：范围校验必须放在**改 cfg 之前**。
    load_strategies() 返回的是缓存对象，先写进去再校验的话，抛异常时
    虽然没落盘，但内存里已经是脏值，下一次读就直接拿到 0。实测：
    提交 {max_same_side:5, risk_per_trade_pct:0} 被拒后，
    内存里 risk_per_trade_pct 已经是 0.0。
    """
    # ① 先对入参做纯校验，一个字节都不碰 cfg
    bad = []
    for k, (lo, hi) in _RISK_BOUNDS.items():
        if k not in updates or updates[k] is None:
            continue
        try:
            got = float(updates[k])
        except (TypeError, ValueError):
            bad.append(f"{k}={updates[k]!r} 不是数字")
            continue
        if not (lo <= got <= hi):
            bad.append(f"{k}={got:g} 超出允许范围 {lo:g} ~ {hi:g}")
    if bad:
        raise ValueError("风控参数不合法，已拒绝写入：" + "；".join(bad))

    # ② 两级熔断的交叉约束：浮亏闸必须小于日熔断线。
    # 反过来的话（浮亏 12% 才停、已实现 10% 就熔断）第一级永远不会先出手，
    # 等于白加一个参数，而且会让使用者误以为有提前刹车。
    _cur_g = (load_strategies().get("global") or {})
    _uh = float(updates.get("unrealized_halt_pct", _cur_g.get("unrealized_halt_pct") or 0) or 0)
    _dh = float(updates.get("daily_loss_halt_pct", _cur_g.get("daily_loss_halt_pct") or 0) or 0)
    if _uh > 0 and _dh > 0 and _uh >= _dh:
        raise ValueError(
            f"浮亏闸（{_uh:g}%）必须小于日熔断线（{_dh:g}%），"
            f"否则第一级永远不会先出手，等于没加这道闸")

    # ③ 校验通过，才开始改
    cfg = load_strategies()
    g = cfg.setdefault("global", {})
    changed = {}
    for k in _GLOBAL_INT_KEYS:
        if k in updates and updates[k] is not None:
            try:
                g[k] = int(float(updates[k]))
                changed[k] = g[k]
            except (TypeError, ValueError):
                continue
    for k in _GLOBAL_FLOAT_KEYS:
        if k in updates and updates[k] is not None:
            try:
                g[k] = float(updates[k])
                changed[k] = g[k]
            except (TypeError, ValueError):
                continue
    if isinstance(updates.get("daily_target"), dict):
        blk = g.setdefault("daily_target", {})
        for k in _DAILY_TARGET_KEYS:
            if k in updates["daily_target"] and updates["daily_target"][k] is not None:
                blk[k] = updates["daily_target"][k]
                changed[f"daily_target.{k}"] = blk[k]
    # ⚠ 2026-11-09：范围校验已经移到函数开头（必须在改 cfg 之前）。
    if not changed:
        raise ValueError("没有可更新的字段（或值非法）")
    save_strategies(cfg)
    return cfg, changed


def set_universe(symbols):
    syms = [str(x).upper() for x in (symbols or []) if str(x).strip()]
    if not syms:
        raise ValueError("universe cannot be empty")
    cfg = load_strategies()
    cfg["global"]["universe"] = syms
    save_strategies(cfg)
    global UNIVERSE
    UNIVERSE = list(syms)
    return syms


# -- credentials (.env, runtime-editable) -----------------------------------

_CRED_KEYS = ("CLAWBY_API_KEY", "CLAWBY_BASE", "BINANCE_API_KEY",
              "BINANCE_SECRET_KEY", "BINANCE_API_ENV",
              "BITGET_API_KEY", "BITGET_SECRET_KEY", "BITGET_PASSPHRASE",
              "OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")


def _mask(v):
    if not v:
        return ""
    if len(v) <= 10:
        return v[:2] + "…" + v[-2:]
    return v[:6] + "…" + v[-4:]


def credentials_masked():
    return {
        "clawby_api_key": _mask(CLAWBY_API_KEY),
        "clawby_base": CLAWBY_BASE,
        "binance_api_key": _mask(BINANCE_API_KEY),
        "binance_secret_key": _mask(BINANCE_SECRET_KEY) if BINANCE_SECRET_KEY else "",
        "binance_env": os.environ.get("BINANCE_API_ENV", "prod"),
        "bitget_api_key": _mask(BITGET_API_KEY),
        "okx_api_key": _mask(OKX_API_KEY),
        "has_clawby": bool(CLAWBY_API_KEY),
        "has_binance": bool(BINANCE_API_KEY and BINANCE_SECRET_KEY),
        "has_bitget": bool(BITGET_API_KEY and BITGET_SECRET_KEY and BITGET_PASSPHRASE),
        "has_okx": bool(OKX_API_KEY and OKX_SECRET_KEY and OKX_PASSPHRASE),
    }


def save_credentials(updates):
    """Update .env (preserving other lines) and hot-swap module globals.
    updates keys: clawby_api_key / binance_api_key / binance_secret_key / clawby_base."""
    global CLAWBY_API_KEY, CLAWBY_BASE, BINANCE_API_KEY, BINANCE_SECRET_KEY, \
        BITGET_API_KEY, BITGET_SECRET_KEY, BITGET_PASSPHRASE, \
        OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE
    mapping = {
        "clawby_api_key": "CLAWBY_API_KEY",
        "clawby_base": "CLAWBY_BASE",
        "binance_api_key": "BINANCE_API_KEY",
        "binance_secret_key": "BINANCE_SECRET_KEY",
        "binance_env": "BINANCE_API_ENV",
        "bitget_api_key": "BITGET_API_KEY",
        "bitget_secret_key": "BITGET_SECRET_KEY",
        "bitget_passphrase": "BITGET_PASSPHRASE",
        "okx_api_key": "OKX_API_KEY",
        "okx_secret_key": "OKX_SECRET_KEY",
        "okx_passphrase": "OKX_PASSPHRASE",
    }
    to_set = {}
    for k, envk in mapping.items():
        if k in updates and updates[k] not in (None, ""):
            to_set[envk] = str(updates[k]).strip()

    with _env_lock:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
        seen = set()
        out = []
        for line in lines:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                key = s.split("=", 1)[0].strip()
                if key in to_set:
                    out.append(f"{key}={to_set[key]}")
                    seen.add(key)
                    continue
            out.append(line)
        for key, val in to_set.items():
            if key not in seen:
                out.append(f"{key}={val}")
        ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
        ENV_PATH.chmod(0o600)

    for envk, val in to_set.items():
        os.environ[envk] = val
    CLAWBY_API_KEY = os.environ.get("CLAWBY_API_KEY", "")
    CLAWBY_BASE = os.environ.get("CLAWBY_BASE", "https://api.openclawby.com")
    BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
    BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
    BITGET_SECRET_KEY = os.environ.get("BITGET_SECRET_KEY", "")
    BITGET_PASSPHRASE = os.environ.get("BITGET_PASSPHRASE", "")
    OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
    OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
    OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
    return credentials_masked()
