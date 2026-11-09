"""验证风控参数的硬边界（2026-11-09）。

背景：/api/risk/global 原来只做类型转换，填什么写什么。
  risk_per_trade_pct=0     → 仓位 = 风险预算/0 → 无穷大
  risk_per_trade_pct=-5    → 负仓位
  max_concurrent_coins=0   → 一次都开不出来（看起来像「策略坏了」）
  daily_loss_halt_pct=1000 → 熔断等于关掉
前端 InputNumber 的 min/max 只影响体验，接口可以被 curl 直接调用。

本测试**不写 strategies.yaml**：用 monkeypatch 把 load/save 换成内存字典。
"""
import copy
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}")
    if detail:
        print(f"      {detail}")


def main():
    from backend import config

    real_cfg = config.load_strategies()
    mem = copy.deepcopy(real_cfg)
    saved = []

    # 打桩：绝不碰磁盘上的 strategies.yaml
    config.load_strategies = lambda: mem
    config.save_strategies = lambda c: saved.append(copy.deepcopy(c))

    base = {
        "risk_per_trade_pct": 1.5,
        "max_gross_leverage": 6,
        "max_position_pct_per_coin": 32.0,
        "max_concurrent_coins": 8,
        "max_same_side": 6,
        "daily_loss_halt_pct": 10.0,
        "event_quiet_minutes": 30,
    }

    print("══ 场景 1：越界值必须被拒，且一个字节都不写 ══")
    bad_cases = [
        ("risk_per_trade_pct", 0, "除以 0 → 仓位无穷大"),
        ("risk_per_trade_pct", -5, "负风险"),
        ("risk_per_trade_pct", 999, "远超上限"),
        ("max_gross_leverage", 0, "杠杆 0"),
        ("max_gross_leverage", 500, "杠杆 500 倍"),
        ("max_concurrent_coins", 0, "同时 0 个币 = 停摆"),
        ("max_same_side", 0, "同方向 0 笔"),
        ("daily_loss_halt_pct", 100, "熔断线 100% = 关掉"),
        ("daily_loss_halt_pct", 0.01, "熔断线太低，一开仓就停"),
        ("max_position_pct_per_coin", -1, "负上限"),
        ("event_quiet_minutes", 10000, "静默一辈子"),
    ]
    for k, v, why in bad_cases:
        saved.clear()
        err = None
        try:
            config.set_global_risk({k: v})
        except ValueError as e:
            err = e
        except Exception as e:      # noqa: BLE001
            err = e
        ok = err is not None and not saved
        check(f"拒绝 {k}={v}", ok,
              f"{why}" + (f" → 已拒绝：{str(err)[:64]}" if ok else " → ★ 被写进去了"))

    print("\n══ 场景 2：边界内的合法值必须能写 ══")
    good_cases = [
        ("risk_per_trade_pct", 1.5), ("risk_per_trade_pct", 0.1),
        ("risk_per_trade_pct", 10), ("max_gross_leverage", 1),
        ("max_gross_leverage", 20), ("max_concurrent_coins", 8),
        ("max_same_side", 6), ("daily_loss_halt_pct", 10),
        ("max_position_pct_per_coin", 32), ("event_quiet_minutes", 0),
        ("event_quiet_minutes", 240),
    ]
    for k, v in good_cases:
        mem["global"] = dict(base)
        saved.clear()
        err = None
        try:
            _c, changed = config.set_global_risk({k: v})
        except Exception as e:      # noqa: BLE001
            err = e
            changed = {}
        ok = err is None and len(saved) == 1 and k in changed
        check(f"接受 {k}={v}", ok, "" if ok else f"★ 被拒了：{err}")

    print("\n══ 场景 3：非数字必须被拒 ══")
    for bad in ("abc", None, [1], {"a": 1}):
        mem["global"] = dict(base)
        saved.clear()
        err = None
        try:
            config.set_global_risk({"risk_per_trade_pct": bad})
        except Exception as e:      # noqa: BLE001
            err = e
        # None 会被上层跳过（视为「不改」），这是既有语义，不算越界
        if bad is None:
            check("None 视为不改（既有语义）", not saved, "没有写入 ✓")
        else:
            check(f"拒绝 risk_per_trade_pct={bad!r}",
                  err is not None and not saved,
                  "" if (err and not saved) else f"★ 异常={err} saved={len(saved)}")

    print("\n══ 场景 4：混合提交（一好一坏）必须整体拒绝 ══")
    mem["global"] = dict(base)
    saved.clear()
    err = None
    try:
        config.set_global_risk({"max_same_side": 5, "risk_per_trade_pct": 0})
    except ValueError as e:
        err = e
    check("一好一坏整体拒绝，不做部分写入", err is not None and not saved,
          f"异常={'有' if err else '无'} 写盘次数={len(saved)}")
    check("坏值未被写入内存配置", mem["global"].get("risk_per_trade_pct") == 1.5,
          f"内存里 risk_per_trade_pct={mem['global'].get('risk_per_trade_pct')}")

    print("\n" + "═" * 60)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("\n  失败项：")
        for f in FAIL:
            print(f"    · {f}")
    print("═" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
