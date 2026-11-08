"""S15 全参数生效审计（2026-11-08）。

三方对齐：strategies.yaml 写的值 → 代码里有没有读取点 → 服务运行时实际值。

判定分类：
    ✅ 生效      有代码引用 + 运行时值 == yaml 值
    ⚠️ 半死      有引用但不影响结果（如 rr_ratio 只参与合法性校验）
    ❌ 死配置    有引用但永不触发 / 无引用
    🔶 继承语义  写 0 表示「用全局值」，是设计而非 bug

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/audit_s15_params.py
"""
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

API = "http://127.0.0.1:8899"

PASS = WARN = DEAD = 0
rows = []


def get(path):
    with urllib.request.urlopen(API + path, timeout=10) as r:
        return json.loads(r.read())


def count_refs(key):
    """统计 python 代码里读这个 key 的处数。"""
    pat = re.compile(r"""["']""" + re.escape(key) + r"""["']""")
    n = 0
    where = []
    for f in (ROOT / "backend").glob("*.py"):
        try:
            txt = f.read_text(encoding="utf-8")
        except Exception:
            continue
        c = len(pat.findall(txt))
        if c:
            n += c
            where.append(f"{f.name}×{c}")
    return n, ",".join(where)


def row(section, key, yaml_val, runtime_val, verdict, note="", refs=None):
    global PASS, WARN, DEAD
    if verdict.startswith("✅"):
        PASS += 1
    elif verdict.startswith("❌"):
        DEAD += 1
    else:
        WARN += 1
    rows.append({
        "section": section, "key": key, "yaml": yaml_val,
        "runtime": runtime_val, "refs": refs if refs is not None else count_refs(key)[0],
        "where": count_refs(key)[1] if refs is None else "",
        "verdict": verdict, "note": note,
    })


def same(a, b):
    """宽松比较：数字容差、bool/str 归一。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a) == str(b)


def main():
    import yaml
    cfg = yaml.safe_load((ROOT / "strategies.yaml").read_text(encoding="utf-8"))
    g = cfg["global"]
    sp = cfg["strategies"]["S15_CE_ZLSMA"]
    p = sp["params"]
    pools = cfg["pools"]

    # ── 运行时 ──
    try:
        rt_strat = get("/api/strategies")
        rt_pool = get("/api/pool")
        ONLINE = True
    except Exception as exc:
        print(f"⚠️ 服务未响应（{exc}），只做静态检查\n")
        rt_strat, rt_pool, ONLINE = {}, {}, False

    rt_global = (rt_strat.get("global") or {})
    rt_s15 = next((s for s in (rt_strat.get("strategies") or [])
                   if s.get("id") == "S15_CE_ZLSMA"), {})
    rt_params = rt_s15.get("params") or {}

    print("=" * 92)
    print("S15 全参数生效审计".center(80))
    print(f"服务状态：{'在线 ' + API if ONLINE else '离线（仅静态检查）'}")
    print("=" * 92)

    # ══════════════════════════════════════════════════════════
    print("\n【一】全局风控 global（risk.py 读取）")
    print("-" * 92)
    for k in ("risk_per_trade_pct", "max_position_pct_per_coin",
              "max_gross_leverage", "max_concurrent_coins", "max_same_side",
              "daily_loss_halt_pct", "event_quiet_minutes"):
        y = g.get(k)
        r = rt_global.get(k) if ONLINE else None
        ok = same(y, r) if ONLINE else None
        v = "✅ 生效" if ok else ("❌ 运行时不一致" if ONLINE else "?")
        row("global", k, y, r, v, refs=count_refs(k)[0])
        print(f"  {k:<30} yaml={str(y):<10} 运行时={str(r):<10} {v}")

    for k in ("greed", "fear", "size_mult"):
        y = g["fear_greed_extreme"].get(k)
        r = (rt_global.get("fear_greed_extreme") or {}).get(k) if ONLINE else None
        ok = same(y, r) if ONLINE else None
        v = "✅ 生效" if ok else "❌ 不一致"
        row("global.fear_greed_extreme", k, y, r, v)
        print(f"  fear_greed.{k:<19} yaml={str(y):<10} 运行时={str(r):<10} {v}")

    # ══════════════════════════════════════════════════════════
    print("\n【二】S15 策略参数 params（strategies.py 读取）")
    print("-" * 92)

    SEMI = {
        "rr_ratio": ("⚠️ 半死", "只用于算 tp 变量做合法性校验（tp>0），"
                              "实际 take_profit=0.0 落库，不参与平仓"),
        "min_atr_pct": ("✅ 生效(关闭)", "yaml=0 关闭策略层 ATR 门槛；"
                                       "ATR 判断已移到池子层 5m 双档"),
        "slip_pct": ("✅ 生效(0)", "滑点 0，只影响记账成本口径"),
    }

    for k, y in p.items():
        r = rt_params.get(k) if ONLINE else None
        ok = same(y, r) if ONLINE else None
        if k in SEMI:
            v, note = SEMI[k]
        elif not ok and ONLINE:
            v, note = "❌ 运行时不一致", ""
        else:
            v, note = "✅ 生效", ""
        refs, where = count_refs(k)
        row("S15.params", k, y, r, v, note, refs=refs)
        flag = v.split()[0]
        print(f"  {k:<20} yaml={str(y):<10} 运行时={str(r):<10} 引用{refs:>2}处 {flag}")
        if note:
            print(f"    └ {note}")

    # ══════════════════════════════════════════════════════════
    print("\n【三】S15 风控 risk 段（risk.py 读取，0 = 继承全局）")
    print("-" * 92)
    INH = {
        "capital_usd": "0 → 用账户实时净值 equity",
        "max_position_usd": "0 → 用 capital × leverage",
        "risk_per_trade_pct": "0 → 用 global.risk_per_trade_pct = 1.5%",
        "leverage": "6 → 直接生效",
    }
    for k, y in (sp.get("risk") or {}).items():
        r = (rt_s15.get("risk") or {}).get(k) if ONLINE else None
        ok = same(y, r) if ONLINE else None
        if y == 0:
            v = "🔶 继承语义"
            note = INH.get(k, "")
        else:
            v = "✅ 生效" if ok else "❌ 不一致"
            note = INH.get(k, "")
        row("S15.risk", k, y, r, v, note)
        print(f"  {k:<20} yaml={str(y):<6} 运行时={str(r):<6} {v}")
        if note:
            print(f"    └ {note}")

    # max_hold_hours 不在 params 层，单独查
    mh = sp.get("max_hold_hours")
    refs = count_refs("max_hold_hours")[0]
    row("S15", "max_hold_hours", mh, "由 Base 读取", "✅ 生效" if refs else "❌ 无引用",
        "不在 params 层，Base.__init__ 直接读", refs=refs)
    print(f"  {'max_hold_hours':<20} yaml={str(mh):<6} 引用{refs}处 "
          f"{'✅ 生效' if refs else '❌ 无引用'}")
    print("    └ 注意：不在 params 层，Base 从策略配置顶层读，写进 params 会失效")

    # ══════════════════════════════════════════════════════════
    print("\n【四】池子配置 pools.monitor（quality.py 读取）")
    print("-" * 92)
    MAP_POOL = {
        "size": "MONITOR_SIZE", "min_quote_vol": "MIN_QUOTE_VOL",
        "max_spike": "MAX_SPIKE", "min_dwell_sec": "MIN_DWELL_SEC",
        "atr_enter_min": "ATR_ENTER_MIN", "atr_exit_max": "ATR_EXIT_MAX",
        "refresh_sec": "REFRESH_SEC", "scan_sec": "SCAN_SEC",
        "daily_refresh_sec": "DAILY_REFRESH_SEC", "tick_sec": "TICK_SEC",
        "atr_interval": "ATR_INTERVAL",
    }
    # /api/pool 的返回键名和 yaml 不完全一致：
    #   yaml "size"            → 返回 "monitor_size"
    #   yaml daily_refresh_sec → 根本不返回（只在 quality.py 内部用，无从远程读）
    #   yaml tick_sec          → 同上
    # 这三个不能因为「取不到」就判不一致，要按静态引用 + 常量默认值确认。
    POOL_RT_KEY = {"size": "monitor_size"}
    POOL_STATIC_ONLY = {"daily_refresh_sec", "tick_sec"}
    for k, y in (pools.get("monitor") or {}).items():
        const = MAP_POOL.get(k, "?")
        qtxt = (ROOT / "backend" / "quality.py").read_text(encoding="utf-8")
        # 该常量在 quality.py 里除了定义/全局声明外，还有没有实际使用点
        uses = [ln for ln in qtxt.splitlines()
                if const in ln and "global " not in ln
                and not ln.strip().startswith(const + " =")
                and not ln.strip().startswith("#")]
        refs = len(uses)

        if k in POOL_STATIC_ONLY:
            r = "（接口不返回）"
            v = "✅ 生效(静态确认)" if refs else "❌ 无使用点"
            note = "只在 quality.loop 内部使用，前端接口不暴露"
        else:
            r = rt_pool.get(POOL_RT_KEY.get(k, k)) if ONLINE else None
            ok = same(y, r) if ONLINE else None
            v = "✅ 生效" if ok else ("❌ 运行时不一致" if ONLINE else "?")
            note = f"对应常量 {const}"

        row("pools.monitor", k, y, r, v, note, refs=refs)
        print(f"  {k:<20} yaml={str(y):<10} 运行时={str(r):<12} 常量{const:<18} {v}")
        if k in POOL_STATIC_ONLY:
            print(f"    └ {note}")

    print("\n【五】候选池候选 pools.candidate（pool.py，main.py 启动时注入）")
    print("-" * 92)
    for k, y in (pools.get("candidate") or {}).items():
        if k == "whitelist":
            print(f"  {k:<20} = {y}")
            continue
        r = rt_pool.get("candidate_size") if k == "size" else None
        refs = count_refs(k)[0] + len(re.findall(
            {"size": "CANDIDATE_SIZE", "refresh_hour": "REFRESH_HOUR",
             "min_amp_pct": "MIN_AMP_PCT", "max_amp_pct": "MAX_AMP_PCT",
             "vol_days": "VOL_DAYS"}.get(k, "@@"), 
            (ROOT / "backend" / "main.py").read_text(encoding="utf-8")))
        v = "✅ 生效" if refs else "❌ 无引用"
        if k != "size":
            r = None
        row("pools.candidate", k, y, r, v, "main.py lifespan 注入", refs=refs)
        print(f"  {k:<20} yaml={str(y):<10} 注入处{refs}次 {v}")

    print("\n【六】每日目标 daily_target（daily_target.py）")
    print("-" * 92)
    for k, y in (g.get("daily_target") or {}).items():
        r = (rt_global.get("daily_target") or {}).get(k) if ONLINE else None
        ok = same(y, r) if ONLINE else None
        v = "✅ 生效" if ok else "❌ 不一致"
        note = ""
        if k == "auto_close_sec" and y and y <= 10:
            note = "⚠️ 提醒后 5 秒就强制全平，来不及反应（浩哥未改）"
        row("global.daily_target", k, y, r, v, note)
        print(f"  {k:<20} yaml={str(y):<10} 运行时={str(r):<10} {v}")
        if note:
            print(f"    └ {note}")

    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 92)
    print(f"统计：✅ 生效 {PASS} 项 / ⚠️ 半死或需注意 {WARN} 项 / ❌ 死配置 {DEAD} 项")
    print("=" * 92)

    out = ROOT / "reports" / "audit_s15_params.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
