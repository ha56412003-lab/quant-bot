"""风控参数生效验真（浩哥自己改，我验）。

验四件事：
  ① yaml 里写的值（文件实际内容）
  ② /api/risk 返回的值（服务运行时真实生效值）
  ③ 两者是否一致 —— 一致才叫「生效」，光改文件不算
  ④ 用新值复算影响：最大同时风险、总杠杆够不够、方向上限

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe check_risk_effective.py
"""
import json
import sys
import urllib.request

sys.path.insert(0, ".")
import yaml  # noqa: E402

API = "http://127.0.0.1:8899/api/risk"
YAML = "strategies.yaml"

KEYS = [
    ("max_concurrent_coins", "最大并发币数", "int"),
    ("max_same_side", "同方向上限", "int"),
    ("max_gross_leverage", "总杠杆上限", "float"),
    ("risk_per_trade_pct", "单笔风险 %", "float"),
    ("max_position_pct_per_coin", "单币名义上限 %", "float"),
    ("daily_loss_halt_pct", "日熔断 %", "float"),
]


def _num(v, default=0.0) -> float:
    """把 yaml / API 里可能为 None 的值安全转成 float。"""
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


BASELINE = {
    "max_concurrent_coins": 6,
    "max_same_side": 6,
    "max_gross_leverage": 6,
    "risk_per_trade_pct": 1.5,
    "max_position_pct_per_coin": 32.0,
    "daily_loss_halt_pct": 10.0,
}


def fetch(tries=3):
    last = None
    for _ in range(tries):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(API, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"取 /api/risk 失败: {last}")


def main():
    cfg = yaml.safe_load(open(YAML, encoding="utf-8"))
    g_file = cfg.get("global") or {}
    live = fetch()
    g_api = live.get("global") or {}
    equity = live.get("equity") or 0

    print("=" * 84)
    print("① 逐项对照：文件 vs 运行时 vs 基线")
    print("=" * 84)
    print(f"{'参数':<24}{'文件里':>10}{'运行时':>10}{'基线':>10}{'变化':>10}{'生效':>8}")
    print("-" * 84)

    bad = []
    changed = []
    for key, label, _t in KEYS:
        vf = g_file.get(key)
        va = g_api.get(key)
        vb = BASELINE.get(key)
        # 数值比较（yaml 里 6 和 6.0 视为相同）
        try:
            same = _num(vf) == _num(va)
        except (TypeError, ValueError):
            same = vf == va
        try:
            diff = "" if _num(vf) == _num(vb) else f"{vb}→{vf}"
        except (TypeError, ValueError):
            diff = "?"
        if diff:
            changed.append((label, vb, vf))
        flag = "✅" if same else "❌ 不一致"
        if not same:
            bad.append((key, vf, va))
        print(f"{label:<24}{str(vf):>10}{str(va):>10}{str(vb):>10}{diff:>10}{flag:>8}")

    print()
    print("=" * 84)
    print("② 结论")
    print("=" * 84)
    if bad:
        print("❌ 文件和服务不一致 —— 改了但没生效：")
        for k, vf, va in bad:
            print(f"   {k}: 文件={vf} 运行时={va}")
        print("   → 服务可能没重读配置，需要重启 8899")
    else:
        print("✅ 文件 = 运行时，改的东西真的生效了（不是只写了文件）")

    if changed:
        print()
        print("本次改动：")
        for label, vb, vf in changed:
            print(f"   {label}: {vb} → {vf}")
    else:
        print()
        print("⚠ 参数跟基线完全一样 —— 还没改，或者改回了原值")

    # ③ 影响复算
    print()
    print("=" * 84)
    print("③ 按新值复算影响（这步是判断改得对不对，不是判断生效没）")
    print("=" * 84)
    try:
        conc = int(g_api.get("max_concurrent_coins") or 0)
        same = int(g_api.get("max_same_side") or 0)
        lev = float(g_api.get("max_gross_leverage") or 0)
        rpt = float(g_api.get("risk_per_trade_pct") or 0)
        per_coin = float(g_api.get("max_position_pct_per_coin") or 0)
        halt = float(g_api.get("daily_loss_halt_pct") or 0)
    except (TypeError, ValueError) as exc:
        print(f"参数读取失败: {exc}")
        return

    eq = float(equity or 0)
    if eq <= 0:
        print("⚠ 净值取不到，跳过复算")
        return

    risk_total = conc * rpt
    cap = eq * lev
    print(f"净值 {eq:,.0f}U")
    print()
    print(f"最大同时风险   {conc} 仓 × {rpt}% = {risk_total:.1f}% 净值 = {eq*risk_total/100:,.0f}U")
    if risk_total > halt:
        print(f"  ⚠ 超过日熔断 {halt}% —— 熔断会比仓位先触发（未必是坏事，但要知道）")
    else:
        print(f"  ✅ 低于日熔断 {halt}%（{risk_total:.1f}% < {halt}%）")
    print()
    print(f"总敞口额度     {eq:,.0f}U × {lev}x = {cap:,.0f}U")
    print(f"每仓平均可用   {cap/conc:,.0f}U（这是上限，实际仓位 = 1R÷止损距离，会小得多）")
    print()
    # 实际仓位估算：按当前池子止损中位数
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:8899/api/pool", timeout=10) as r:
            pool = json.loads(r.read().decode("utf-8"))
        rows = {x["symbol"]: x for x in (pool.get("rows") or [])}
        stops = [rows[s]["stop_pct"] for s in (pool.get("monitor") or [])
                 if s in rows and rows[s].get("stop_pct")]
        if stops:
            med = sorted(stops)[len(stops)//2]
            one_r = eq * rpt / 100
            per_pos = one_r / (med / 100)
            total = per_pos * conc
            print(f"按池内止损中位 {med:.2f}% 估算:")
            print(f"  每仓名义 {per_pos:,.0f}U → {conc} 仓合计 {total:,.0f}U = {total/eq:.2f}x")
            print(f"  总杠杆 {lev}x 够不够: {'✅ 够（用掉 ' + f'{total/cap*100:.0f}%' + '）' if total <= cap else '❌ 不够，会被杠杆闸门截断'}")
    except Exception as exc:  # noqa: BLE001
        print(f"（池子数据取不到，跳过估算: {exc}）")

    print()
    print(f"方向上限       {same} 个（{conc} 仓里最多 {same} 个同向）")
    if same > conc:
        print(f"  ⚠ 同方向上限({same}) > 并发上限({conc})，等于没约束")
    auto = max(1, conc * 2 // 3)
    print(f"  参考值：自动档会取 {auto}（并发 × 2/3）")
    print()
    print("=" * 84)


if __name__ == "__main__":
    main()
