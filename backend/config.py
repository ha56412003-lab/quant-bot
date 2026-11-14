"""Configuration: .env credentials + strategies.yaml (runtime-editable).

strategies.yaml is the single source of truth for strategy switches / params /
per-strategy symbols / per-strategy risk + the global monitored universe.
Credentials live in .env and are editable at runtime via the Config page.
"""
import io
import os
import threading
from pathlib import Path

import yaml

try:  # ★ 保留注释的 yaml 写入器；装不上就退回 safe_dump（只在保存时丢注释，不影响读）
    from ruamel.yaml import YAML as _RTYAML

    _RT_YAML = _RTYAML()
    _RT_YAML.preserve_quotes = True
    _RT_YAML.width = 4096
    _RT_YAML.indent(mapping=2, sequence=2, offset=0)
except Exception:  # noqa: BLE001
    _RT_YAML = None

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
STRATEGIES_PATH = ROOT / "strategies.yaml"
DB_PATH = os.environ.get("QB_DB_PATH", str(ROOT / "quantbot.db"))

# ── 版本号：全系统唯一真源 ────────────────────────────────────────────
# 只改这一处；/api/status 会带给前端顶栏，文档与代码就不会再对不上。
# 版本含义见 F:\obsidian笔记库\QUANT-BOT_VS15-1.3版本\VS15-1.3定版.md
APP_VERSION = "VS15-1.5"

_lock = threading.Lock()
_env_lock = threading.Lock()

# ══════════════════════════════════════════════════════════════════
# 配置变更审计（2026-11-11）
#
# 为什么用钩子而不是直接 import db：
#   backend/db.py 第 7 行是 `from . import config`，config 反向 import db
#   会形成循环。所以这里只留一个可注入的钩子，由 main.py 启动时把
#   「写 db + 带上 actor/ip」的实现塞进来。
#
# 审计永远不能影响主流程：钩子内部异常一律吞掉，只留一条日志。
# ══════════════════════════════════════════════════════════════════
_AUDIT_HOOK = None


def set_audit_hook(fn):
    """main.py 启动时注入。签名 fn(action, target, detail, ok, err)。"""
    global _AUDIT_HOOK
    _AUDIT_HOOK = fn


def _audit(action, target="", detail=None, ok=True, err=""):
    """记一条配置变更。detail 是 dict（钩子负责序列化）。"""
    if _AUDIT_HOOK is None:
        return
    try:
        _AUDIT_HOOK(action, str(target or ""), detail, bool(ok), str(err or ""))
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("audit hook failed", exc_info=True)


def _audit_old(sid, key):
    """取某策略某参数当前值，给审计的 old 字段用。读不到返回 None。"""
    try:
        cfg = load_strategies()
        p = ((cfg.get("strategies", {}).get(sid) or {}).get("params") or {})
        return p.get(key)
    except Exception:  # noqa: BLE001
        return None


def _audit_old_global(key, sub=None):
    """取全局配置当前值（key 可以是 'risk_per_trade_pct' 或 ('daily_target','target_usd')）。"""
    try:
        cfg = load_strategies()
        g = cfg.get("global", {}) or {}
        if sub is None:
            return g.get(key)
        return (g.get(key) or {}).get(sub)
    except Exception:  # noqa: BLE001
        return None


def audit_rejected(action, target, detail, err):
    """记一条**被拒绝**的写入。

    为什么连失败也要记：光有「成功改了什麼」只能回答「现在为什么是这个值」，
    回答不了「昨天是不是有人试图把杠杆改成 999 被挡下来了」——
    后者恰恰是排查误操作和外部试探最需要的证据。
    """
    _audit(action, target, detail, ok=False, err=str(err)[:400])


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

# ★ 令牌轮换宽限期（2026-11-11）：轮换后旧 token 再活一段时间，
#   避免自己还没拿到新 token 就被锁在门外。
#   轮换脚本会写这个值；到期后手工清空即可。
API_TOKEN_PREV = os.environ.get("QB_API_TOKEN_PREV", "").strip()

# ★ 观战专用 key（2026-11-11）：只给读权限，不能写。
#   留空 → GET 不鉴权（向后兼容，等于现在的行为）。
#   配了 → 所有 /api/* 的 GET 必须带 x-api-token（完整权限）
#          或 x-view-key（只读观战），否则 401。
#   ★ 设计要点：没配就退化回旧行为 → 不会因为忘配而把自己锁在外面。
VIEW_KEY = os.environ.get("QB_VIEW_KEY", "").strip()
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


def _merge_roundtrip(node, plain):
    """把 plain(dict) 灌进 node（ruamel 的 CommentedMap），文件里的注释原样保留。

    为什么需要它（2026-11-12，浩哥拍板）：
      yaml.safe_dump 是「按 dict 重新生成整份文件」——它只认识数据、不认识注释。
      从界面保存一次参数，strategies.yaml 里 127 行「为什么定这个数」的说明会全部消失，
      下次想改回去就看不到当初的依据了。
      ruamel.yaml 的 round-trip 模式先带注释把文件读进来，再把新值灌进去，注释不动。
    """
    for k in list(node.keys()):
        if k not in plain:
            del node[k]
    for k, v in plain.items():
        cur = node.get(k) if hasattr(node, "get") else None
        if isinstance(v, dict) and isinstance(cur, dict):
            _merge_roundtrip(cur, v)
            continue
        # ★ 值没变就绝不赋值：ruamel 把注释挂在「键值对」对象上，
        #   就算赋的是同一个数，也会把这个键上面的注释整块丢掉（踩过：
        #   max_position_pct_per_coin 上面 3 行注释就是这么没的）。
        #   顺带也保住了原来的写法（0.30 不会被改写成 0.3）。
        if cur is not None and cur == v:
            continue
        node[k] = v


def _write(cfg):
    with _lock:
        text = None
        if _RT_YAML is not None:
            try:
                doc = None
                if STRATEGIES_PATH.exists():
                    with io.open(STRATEGIES_PATH, encoding="utf-8") as f:
                        doc = _RT_YAML.load(f)
                if isinstance(doc, dict):
                    _merge_roundtrip(doc, cfg)
                    buf = io.StringIO()
                    _RT_YAML.dump(doc, buf)
                    text = buf.getvalue()
            except Exception:  # noqa: BLE001 - 写不进去也不能让保存接口挂掉
                text = None
        if text is None:
            # 兜底：ruamel 不可用时退回 PyYAML（这一条会丢注释，但绝不丢配置）
            text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
        # ★ newline="\n" 必须显式写：Windows 下默认文本模式会把换行翻成 CRLF，
        #   结果整份文件每一行都算改动（踩过），也和仓库里的 LF 对不上。
        with io.open(STRATEGIES_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


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
    try:
        _old = (load_strategies().get("strategies", {}).get(sid) or {}).get("enabled")
    except Exception:  # noqa: BLE001
        _old = None
    r = _update_strategy(sid, lambda s: s.update(enabled=bool(enabled)))
    _audit("set_strategy_enabled", sid,
           {"enabled": {"old": _old, "new": bool(enabled)}})
    return r


# ══════════════════════════════════════════════════════════════════
# 策略参数硬边界（2026-11-11）
#
# 29 个键全部有据可查 —— 每个键在 backend/*.py 都有真实读取点，无死键：
#   strategies.py:496-532 读 22 个 / quality.py:224 / engine.py:365 /
#   main.py:611-612,803 读出场与 EFF。
#
# 边界怎么定的（改边界前先看那一行代码在干什么）：
#   · 做除数或窗口长度的键（ce_len / zlsma_len / ce_mult / eff_win_signal）
#     必须 ≥ 下限，0 会除零或让指标失去意义。
#   · 0 在这几个键上是「关闭该闸」的**合法语义**，所以下限取 0：
#     min_atr_pct / dual_stop_min_pct /
#     min_stop_dist_pct / slip_pct / be_at_r / half_at_r / full_at_r。
#   · 比例类（key_closepos / key_bodyfrac / eff_min_signal / bbw_pct_min）
#     天然 0~1 或 0~100，超出去不是「更严」而是恒真/恒假。
# ══════════════════════════════════════════════════════════════════

# 币安 U 本位合约支持的 K 线周期（分钟数）。写死枚举而不是范围：
# 填 "20m" 这种不存在的周期，klines 请求会永远 400 → 该币静默不交易。
_KLINE_SECONDS = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480,
    "12h": 720, "1d": 1440,
}

# None = 该键是布尔（只校验类型，不校验范围）
_STRAT_PARAM_BOUNDS = {
    # ── K 线周期 ────────────────────────────────────────────────
    "kline_interval":     ("enum", _KLINE_SECONDS),
    # ── CE 通道（Chandelier Exit）────────────────────────────────
    "ce_len":             (1, 100),      # ATR 窗口，0 → 除零
    "ce_mult":            (0.1, 10.0),   # 0.1 止损贴脸 / >10 几乎不触发
    # ── ZLSMA 均线 ─────────────────────────────────────────────
    "zlsma_len":          (5, 300),      # <5 无意义 / >300 要 600 根 K 线
    "use_close":          None,
    "use_ha":             None,
    # ── 止损 ────────────────────────────────────────────────────
    "swing_lookback":     (2, 100),      # 左侧摆动根数
    "min_stop_dist_pct":  (0.0, 50.0),   # 下限，0=不设
    "max_risk_pct":       (0.1, 50.0),   # 上限，0=永不成交
    "dual_stop_min_pct":  (0.0, 50.0),   # 两档阈值，0=关闭两档
    "stop_key": None,
    # ★ 同上：界面有框（止损算法「再往外让一点」），这里漏登记 → 存不进去。
    "stop_key_atr_buf": (0.0, 5.0),
    # ★ VS15-1.5 第④条（时段翻转平）：独立气垫（比 stop_key_atr_buf 紧）。
    #   0 = 关闭第④条（现网值 0；旧「双样本变差」结论已作废，等正确实现重跑）。
    "stop_key_atr_buf_tight": (0.0, 5.0),
    # ★ 2026-11-12 夜拆出来的「右侧止盈」独立三键。原来右侧止盈直接复用入场
    #   的 key_mult / key_closepos / key_bodyfrac —— 调入场会连带改出场，
    #   而这两件事的风险方向完全不同（入场放宽=多开单；出场放宽=早止盈）。
    #   拆开后各调各的，互不影响。
    "right_key_mult": (0.0, 10.0),
    "right_key_closepos": (0.0, 1.0),
    "right_key_bodyfrac": (0.0, 1.0),
    # ── 进场闸 ──────────────────────────────────────────────────
    "bbw_pct_min": (0.0, 100.0),
    # ★ 2026-11-12 夜：豁免三键补登记。之前界面给了输入框、strategies.py 也在读，
    #   但这里漏了 → 网页上填完点保存直接报「不是已知策略参数」，改不进去。
    "bbw_exempt_mult": (0.0, 10.0),
    "bbw_exempt_closepos": (0.0, 1.0),
    "bbw_exempt_bodyfrac": (0.0, 1.0),  # 分位数，天然 0~100
    "min_atr_pct":        (0.0, 50.0),   # 0=不启用
    # 偏离闸的两个键（min_dev_pct / dev_atr_mult）已于 VS15-1.2 删除 —— 闸门本身早就没了，
    # 留着它们只会出现「界面上能改、代码不读」的死键，比没有更糟。
    "eff_min_signal":     (0.0, 1.0),    # 效率比 0~1
    "eff_win_signal":     (5, 500),      # 窗口
    "key_bar":            None,
    "key_mult":           (0.0, 10.0),   # 关键K线实体 ATR 倍数
    "key_closepos":       (0.0, 1.0),    # 收盘位置比例
    "key_bodyfrac":       (0.0, 1.0),    # 实体占比
    # ★ VS15-1.5 第⑦道闸：abs(HA收盘 − ZLSMA50)/ATR20 的上限（0 = 关闭）。
    #   0.05 下限防止「填个 0.001」把全部信号都拦掉还以为是 bug。
    "zlsma_ext_max":      (0.0, 30.0),
    # ── 出场 ────────────────────────────────────────────────────
    "be_at_r":            (0.0, 20.0),   # 保本，0=关闭
    "half_at_r":          (0.0, 20.0),   # 半平，0=关闭
    "full_at_r":          (0.0, 20.0),   # 全平，0=关闭
    "right_exit":         None,
    "right_exit_strict":  None,
    "flip_exit_early":    (0, 1),        # 信号翻转提前到平半前（0=关=默认；80币回测否决）
    # ── 同根限笔 / 成本 ─────────────────────────────────────────
    "max_same_bar_side":  (1, 20),       # 0=禁止所有开仓
    "fee_rate":           (0.0, 0.01),   # 1% 封顶（币安 taker 0.05%）
    "slip_pct":           (0.0, 5.0),    # 0=无滑点
    "reverse_enabled":    None,          # bool：反手开关（默认 false，回测已证伪）
}

# 必须是整数的键（其余按 float 走）。int 在这里不是小事：
# zlsma_len=49.5 传进去会被 int() 截成 49，用户以为改了实际没改。
_STRAT_PARAM_INT_KEYS = frozenset({
    "ce_len", "zlsma_len", "swing_lookback", "eff_win_signal", "max_same_bar_side",
})

# ★★★ 定版参数默认值（「一键恢复默认」恢复的就是这张表）
#
# 来源：2026-11-12 深夜晚逐项核对并跑过数据的定版值，逐条抄自 strategies.yaml 的
#      S15_CE_ZLSMA.params。不是拍脑袋填的，改动任何一项都必须同时改 yaml 和这里。
#
# 铁律：
#   ① 这张表只做「回到已知好状态」的参照，不参与运行时计算 —— 运行时永远读 yaml；
#   ② 前端不许自己写死这些数字，必须从 /api/strategies 的 defaults 字段读，
#      否则以后改定版值要改两处、必然漏一处；
#   ③ tests/test_new_gates.py 的 H 组守卫会检查「yaml 定版值 == 这张表」，
#      任何一边单独改都会红。
_STRAT_PARAM_DEFAULTS = {
    "S15_CE_ZLSMA": {
    "ce_len": 1, "ce_mult": 2.0, "zlsma_len": 50, "swing_lookback": 8,
    "use_ha": True, "use_close": True, "kline_interval": "15m",
    "key_bar": True, "key_mult": 1.0, "key_closepos": 0.66, "key_bodyfrac": 0.5,
    "bbw_pct_min": 70.0,
    "bbw_exempt_mult": 1.0, "bbw_exempt_closepos": 0.8, "bbw_exempt_bodyfrac": 0.8,
    "stop_key": True, "stop_key_atr_buf": 0.2,
    "min_stop_dist_pct": 0.6, "max_risk_pct": 5.0, "dual_stop_min_pct": 0.5,
    "min_atr_pct": 0.0,
    "eff_min_signal": 0.30, "eff_win_signal": 48,
    "be_at_r": 1.5, "half_at_r": 1.2, "full_at_r": 2.5,
    # ★ VS15-1.5 第④条「时段翻转平」：0 = 关（定版值），>0 = 往信号K线端点外让几倍 ATR20
    #   前端策略页的「启用」开关就是认这个键：开 = 写 0.2，关 = 写回 0。
    "stop_key_atr_buf_tight": 0.0,
    # ★ VS15-1.5 第⑦道闸（ZLSMA 偏离）：定版值 1.5 倍 ATR20，0 = 关闸
    "zlsma_ext_max": 1.5,
    "right_exit": True, "right_exit_strict": True, "flip_exit_early": 0,
    "reverse_enabled": False,
    # 右侧止盈用的关键K线门槛（独立于入场，取历史定版值 1.0/0.66/0.5）
    "right_key_mult": 1.0, "right_key_closepos": 0.66, "right_key_bodyfrac": 0.5,
    "max_same_bar_side": 2, "fee_rate": 0.0005, "slip_pct": 0.0,
    },
}


def strat_param_defaults(sid):
    """某策略的定版默认参数副本（给前端「恢复默认」用）。"""
    return dict(_STRAT_PARAM_DEFAULTS.get(sid) or {})




def _validate_params(sid, params):
    """纯校验，一个字节都不碰 cfg。返回可以直接写盘的 clean dict。

    原则与 set_global_risk / set_strategy_risk 一致：
      ① 先校验后写盘 —— 有一条不合法就整批拒绝，绝不写一半；
      ② 报错文案带键名、实际值、允许范围，前端直接展示给用户；
      ③ 未知键直接拒 —— 以前 set_strategy_params 是零校验 foreach，
         拼错一个键名（zlsma_lne）会静默写进 yaml，永远不生效也永远不报错。
    """
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象")

    # 交叉约束要按「合并后的最终值」判：本次只传 half_at_r 时，
    # full_at_r 得去 yaml 取现值，否则永远判不出来。
    cfg_now = load_strategies()
    cur = dict(((cfg_now.get("strategies", {}).get(sid) or {}).get("params") or {}))
    merged = dict(cur)
    merged.update(params)

    clean, bad = {}, []
    for raw_k, v in params.items():
        k = str(raw_k)
        if k not in _STRAT_PARAM_BOUNDS:
            bad.append(f"{k} 不是已知策略参数")
            continue
        if v is None:
            continue                       # None = 不改这个键
        spec = _STRAT_PARAM_BOUNDS[k]

        # ① 布尔键：只认真布尔。"false" 字符串在 Python 里是真值，
        #    写成字符串会让 use_ha="false" 变成开启 HA —— 静默反向。
        if spec is None:
            if not isinstance(v, bool):
                bad.append(f"{k}={v!r} 必须是 true / false")
                continue
            clean[k] = v
            continue

        # ② K 线周期：枚举
        if isinstance(spec, tuple) and spec and spec[0] == "enum":
            s = str(v).strip().lower()
            if s not in spec[1]:
                bad.append(f"kline_interval={v!r} 必须是 "
                           f"{'/'.join(spec[1])} 之一")
                continue
            clean[k] = s
            continue

        # ③ 数字键：先转类型再判范围
        lo, hi = spec
        is_int = k in _STRAT_PARAM_INT_KEYS
        try:
            if is_int:
                f = float(v)
                if f != int(f):
                    bad.append(f"{k}={v!r} 必须是整数（不能带小数）")
                    continue
                num = int(f)
            else:
                num = float(v)
        except (TypeError, ValueError):
            bad.append(f"{k}={v!r} 不是{'整数' if is_int else '数字'}")
            continue
        if not (lo <= num <= hi):
            bad.append(f"{k}={num:g} 超出允许范围 {lo:g} ~ {hi:g}")
            continue
        clean[k] = num

    # ── 交叉约束：单键都合法、组合起来却跑不动 ────────────────────
    if not bad:
        try:
            lo_d = float(merged.get("min_stop_dist_pct") or 0.0)
            hi_d = float(merged.get("max_risk_pct") or 0.0)
            if hi_d > 0 and lo_d > hi_d:
                bad.append(
                    f"min_stop_dist_pct({lo_d:g}) 不能大于 max_risk_pct({hi_d:g})："
                    f"止损距离无法同时满足上下限，会一笔都开不出来")
        except (TypeError, ValueError):
            pass
        try:
            h = float(merged.get("half_at_r") or 0.0)
            f = float(merged.get("full_at_r") or 0.0)
            if h > 0 and f > 0 and h >= f:
                bad.append(
                    f"half_at_r({h:g}) 必须小于 full_at_r({f:g})："
                    f"否则全平先触发，半平永远不会执行")
        except (TypeError, ValueError):
            pass
        # ── 豁免门槛必须「不低于」普通门槛 ──────────────────────────
        # 为什么不加这条会出事：代码顺序是「先判普通档过③ → 再判豁免档免BBW」。
        # 如果豁免档的门槛比普通档还低（例如 key_closepos=0.75 但
        # bbw_exempt_closepos=0.7），收盘位 0.72 的 K 线在③闸就被拦掉了，
        # **永远走不到豁免判定** —— 豁免功能静默失效，不报错、日志也没有。
        # 只在两个前提同时成立时才判：
        #   ① key_bar 开着（否则普通档本来就不生效，无从比较）
        #   ② bbw_exempt_mult > 0（否则豁免是刻意关闭的，不该拦）
        try:
            if bool(merged.get("key_bar", True)) and float(merged.get("bbw_exempt_mult") or 0) > 0:
                for _ex, _base, _label in (
                    ("bbw_exempt_mult", "key_mult", "力度"),
                    ("bbw_exempt_closepos", "key_closepos", "收盘位置"),
                    ("bbw_exempt_bodyfrac", "key_bodyfrac", "实体占比"),
                ):
                    _a = float(merged.get(_ex) or 0.0)
                    _b = float(merged.get(_base) or 0.0)
                    if _a < _b - 1e-9:
                        bad.append(
                            f"{_ex}({_a:g}) 不能小于 {_base}({_b:g})："
                            f"豁免{_label}门槛低于普通门槛，这条豁免永远走不到"
                            f"（普通档会先把 K 线拦掉），等于静默关闭")
        except (TypeError, ValueError):
            pass

    if bad:
        raise ValueError("策略参数不合法，已拒绝写入：" + "；".join(bad))
    return clean


def set_strategy_params(sid, params):
    """写入策略参数。先校验再合并（2026-11-11 前是零校验 foreach）。"""
    clean = _validate_params(sid, params)
    if not clean:
        return load_strategies()

    def mutate(s):
        blk = s.setdefault("params", {})
        blk.update(clean)
    _audit("set_strategy_params", sid, {
        "changed": {k: {"old": _audit_old(sid, k), "new": v} for k, v in clean.items()},
    })
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
    try:
        _old = (load_strategies().get("strategies", {}).get(sid) or {}).get("scan_interval")
    except Exception:  # noqa: BLE001
        _old = None
    _new = max(1, int(interval_sec))
    r = _update_strategy(sid, lambda s: s.update(scan_interval=_new))
    _audit("set_strategy_interval", sid,
           {"scan_interval": {"old": _old, "new": _new}})
    return r


def set_strategy_symbols(sid, symbols):
    syms = [str(x).upper() for x in (symbols or [])]
    allowed = set(UNIVERSE) | set(KNOWN_SYMBOLS)
    bad = [s for s in syms if s not in allowed]
    if bad:
        raise ValueError(f"未知交易币种: {','.join(bad[:5])}(需在全局监控或已知币列表内)")
    try:
        _old = list((load_strategies().get("strategies", {}).get(sid) or {}).get("symbols") or [])
    except Exception:  # noqa: BLE001
        _old = []
    r = _update_strategy(sid, lambda s: s.update(symbols=syms))
    _audit("set_strategy_symbols", sid, {
        "count": {"old": len(_old), "new": len(syms)},
        "added": sorted(set(syms) - set(_old))[:20],
        "removed": sorted(set(_old) - set(syms))[:20],
    })
    return r


# 策略级风控硬边界（2026-11-11）。与全局风控同一原则：
#   前端 InputNumber 的 min/max 只影响体验，接口可以被 curl 直接调用，
#   所以必须在写盘前挡。
# 三个 0 都是**合法语义**，不是「关掉风控」，所以下限取 0：
#   capital_usd 0        = 用全账户净值
#   max_position_usd 0   = capital × leverage
#   risk_per_trade_pct 0 = 用全局值
# leverage 上限取 125 —— 币安 U 本位永续合约的最高档。
_STRAT_RISK_BOUNDS = {
    "capital_usd": (0.0, 1e9),
    "leverage": (1, 125),
    "max_position_usd": (0.0, 1e9),
    "risk_per_trade_pct": (0.0, 100.0),
}


def set_strategy_risk(sid, risk):
    # ① 先纯校验，一个字节都不碰 cfg（与 set_global_risk 同原则）。
    # 2026-11-11 前这里是零校验，只做 float/int 转换：实测 leverage=999、
    # capital_usd=-500 都能 200 成功落盘。
    if not isinstance(risk, dict):
        raise ValueError("risk 必须是对象")
    bad = []
    for k, (lo, hi) in _STRAT_RISK_BOUNDS.items():
        if k not in risk or risk[k] is None:
            continue
        try:
            got = float(risk[k])
        except (TypeError, ValueError):
            bad.append(f"{k}={risk[k]!r} 不是数字")
            continue
        if not (lo <= got <= hi):
            bad.append(f"{k}={got:g} 超出允许范围 {lo:g} ~ {hi:g}")
    if bad:
        raise ValueError("策略风控参数不合法，已拒绝写入：" + "；".join(bad))

    try:
        _cur = dict((load_strategies().get("strategies", {}).get(sid) or {}).get("risk") or {})
    except Exception:  # noqa: BLE001
        _cur = {}

    def mutate(s):
        blk = s.setdefault("risk", dict(DEFAULT_STRATEGY_RISK))
        for k in DEFAULT_STRATEGY_RISK:
            if k in risk and risk[k] is not None:
                blk[k] = float(risk[k]) if k != "leverage" else int(risk[k])
    r = _update_strategy(sid, mutate)
    _audit("set_strategy_risk", sid, {
        "changed": {
            k: {"old": _cur.get(k),
                "new": (int(risk[k]) if k == "leverage" else float(risk[k]))}
            for k in DEFAULT_STRATEGY_RISK
            if k in risk and risk[k] is not None
        },
    })
    return r


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

# 日目标子键的硬边界（2026-11-11）。这块以前是**原样写入、连类型都不转**：
#   sustain_sec=-1、target_usd=-999、auto_close_sec=-50 实测都能 200 落盘。
#   target_usd 为负 = 永远达不到目标；auto_close_sec 为负 = 达成瞬间就砍仓。
_DAILY_TARGET_NUM_BOUNDS = {
    "target_usd": (10.0, 1_000_000.0),   # 赚够多少 U 就停手
    "sustain_sec": (0.0, 86400.0),       # 需持续顶住多久才确认（0=立即）
    "auto_close_sec": (0.0, 86400.0),    # 提醒后多久强制平仓（0=只提醒）
}
_DAILY_TARGET_BOOL_KEYS = ("enabled", "pause_new", "notify")


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

    # ①b daily_target 的子键也要挡（2026-11-11）。
    #     这块以前是原样写入、连类型都不转：sustain_sec=-1、target_usd=-999
    #     都能 200 落盘。target_usd 为负 = 永远达不到目标；auto_close_sec 为负
    #     = 达成瞬间就砍仓 —— 都属「一次误触废掉系统」。
    _dt_in = updates.get("daily_target")
    if isinstance(_dt_in, dict):
        for _k, (_lo, _hi) in _DAILY_TARGET_NUM_BOUNDS.items():
            if _k not in _dt_in or _dt_in[_k] is None:
                continue
            try:
                _got = float(_dt_in[_k])
            except (TypeError, ValueError):
                bad.append(f"daily_target.{_k}={_dt_in[_k]!r} 不是数字")
                continue
            if not (_lo <= _got <= _hi):
                bad.append(f"daily_target.{_k}={_got:g} 超出允许范围 {_lo:g} ~ {_hi:g}")
        for _k in _DAILY_TARGET_BOOL_KEYS:
            if _k not in _dt_in or _dt_in[_k] is None:
                continue
            if not isinstance(_dt_in[_k], bool):
                bad.append(f"daily_target.{_k}={_dt_in[_k]!r} 必须是 true/false")
    elif _dt_in is not None:
        bad.append("daily_target 必须是对象")

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

    # ②b 日目标的组合约束（2026-11-11）：auto_close_sec>0 + pause_new=false
    #     是自相矛盾的一对 —— 「N 秒后自动清空持仓」意味着「今天到此为止」，
    #     而 pause_new=false 又表示「清完还要继续开新仓」，于是清完立刻重建
    #     一个一模一样的仓位，白送一趟手续费和滑点。
    #     ★ 这不是「合并两个参数」，是「禁止一个说不通的组合」：
    #       enabled 是总开关、pause_new 是达标后的子行为，两者不重叠（见文档）。
    if isinstance(_dt_in, dict):
        _blk_now = (_cur_g.get("daily_target") or {})
        try:
            _ac = float(_dt_in.get("auto_close_sec",
                                   _blk_now.get("auto_close_sec") or 0) or 0)
        except (TypeError, ValueError):
            _ac = 0.0
        _pn_raw = _dt_in.get("pause_new", _blk_now.get("pause_new"))
        _pn = True if _pn_raw is None else bool(_pn_raw)
        if _ac > 0 and not _pn:
            raise ValueError(
                f"daily_target.auto_close_sec={_ac:g} 表示达标后自动清空持仓，"
                f"但 pause_new=false 表示清完还要继续开新仓 —— 清完会立刻重建同样的仓位。"
                f"要么 pause_new 设 true（清完停手），"
                f"要么 auto_close_sec 设 0（只提醒不动手）")

    # ③ 校验通过，才开始改
    cfg = load_strategies()
    g = cfg.setdefault("global", {})
    changed = {}
    _old = {}                      # 审计用：改之前的值（改完就取不到了）
    for k in _GLOBAL_INT_KEYS:
        if k in updates and updates[k] is not None:
            try:
                _old[k] = g.get(k)
                g[k] = int(float(updates[k]))
                changed[k] = g[k]
            except (TypeError, ValueError):
                continue
    for k in _GLOBAL_FLOAT_KEYS:
        if k in updates and updates[k] is not None:
            try:
                _old[k] = g.get(k)
                g[k] = float(updates[k])
                changed[k] = g[k]
            except (TypeError, ValueError):
                continue
    if isinstance(updates.get("daily_target"), dict):
        blk = g.setdefault("daily_target", {})
        for k in _DAILY_TARGET_KEYS:
            if k in updates["daily_target"] and updates["daily_target"][k] is not None:
                _old[f"daily_target.{k}"] = (g.get("daily_target") or {}).get(k)
                blk[k] = updates["daily_target"][k]
                changed[f"daily_target.{k}"] = blk[k]
    # ⚠ 2026-11-09：范围校验已经移到函数开头（必须在改 cfg 之前）。
    if not changed:
        raise ValueError("没有可更新的字段（或值非法）")
    save_strategies(cfg)
    _audit("set_global_risk", "global", {
        "changed": {k: {"old": _old.get(k), "new": v} for k, v in changed.items()},
    })
    return cfg, changed


def set_universe(symbols):
    syms = [str(x).upper() for x in (symbols or []) if str(x).strip()]
    if not syms:
        raise ValueError("universe cannot be empty")
    try:
        _old = list((load_strategies().get("global") or {}).get("universe") or [])
    except Exception:  # noqa: BLE001
        _old = []
    cfg = load_strategies()
    cfg["global"]["universe"] = syms
    save_strategies(cfg)
    global UNIVERSE
    UNIVERSE = list(syms)
    _audit("set_universe", "global", {
        "count": {"old": len(_old), "new": len(syms)},
        "added": sorted(set(syms) - set(_old))[:20],
        "removed": sorted(set(_old) - set(syms))[:20],
    })
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
        # ★ 2026-11-11：改原子写。原来是 ENV_PATH.write_text(...) 直接覆盖，
        #   写到一半崩溃/断电 → .env 被截断 → 所有 API key 丢失、服务起不来。
        #   改成「同目录临时文件 → os.replace()」：同分区 rename 是原子操作，
        #   结果只可能是完整的旧版或完整的新版，不存在半截文件。
        _tmp = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
        _tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        try:
            _tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(_tmp, ENV_PATH)

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
    # ★ 审计：只记「哪些键被更新了」+ 每个值的长度与前 4 位，**绝不落明文**。
    #   凭据一旦写进审计表 = 二次泄露，而且审计表通常不做加密。
    _audit("save_credentials", "credentials", {
        "keys": {k: {"len": len(v), "head": (v[:4] + "…") if len(v) > 4 else "…"}
                 for k, v in to_set.items()},
    })
    return credentials_masked()
