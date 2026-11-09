"""验证「浮亏闸」（决策 2 第一级，2026-11-09）。

背景：日熔断只看**已实现**亏损。8 个仓位各浮亏 1R(=150U) = 净值 12% 时，
系统依然认为「今天没亏」，继续开新仓；等这些止损全部落地，亏损已经发生，
熔断只是马后炮。

第一级只管「不再加新风险」：浮亏达阈值 → 停开新仓，已有仓位的
硬止损/快速失败/2R平半/3R/镜像/16h 全部照常运行。

本测试用临时库 + 打桩，不碰生产数据。
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}")
    if detail:
        print(f"      {detail}")


class _Sig:
    def __init__(self, symbol="TESTUSDT", side="long", strategy="S15_HA_CE"):
        self.symbol, self.side, self.strategy = symbol, side, strategy
        self.price = 100.0
        self.stop_price = 99.0
        self.reason = "test"


async def main():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="uhalt_")
    os.close(fd)
    os.environ["QB_DB_PATH"] = path
    for m in ("backend.db", "backend.executor", "backend.factors"):
        if m in sys.modules:
            del sys.modules[m]

    from backend import db, risk, factors, daily_target

    db.init()

    # 打桩：价格表 + 日目标不拦
    PRICES = {}
    factors.mark_price_of = lambda sym: PRICES.get(sym, 0.0)
    daily_target.blocked = lambda *a, **k: False

    eq = 10_000.0
    # 从真实配置起底再覆盖：size_multiplier / allow_entry 会读很多 global 键，
    # 手写 dict 漏一个就是 KeyError，而且漏掉的恰恰是"别的风控闸"。
    from backend import config as _cfgmod
    base_cfg = dict(_cfgmod.load_strategies()["global"])
    base_cfg.update({
        "risk_per_trade_pct": 1.5,
        "max_gross_leverage": 6,
        "max_position_pct_per_coin": 32.0,
        "max_concurrent_coins": 8,
        "max_same_side": 6,
        "daily_loss_halt_pct": 10.0,
        "event_quiet_minutes": 0,
        "unrealized_halt_pct": 8.0,
    })
    sig = _Sig()

    def clear():
        # 只对临时库操作
        db._conn.execute("DELETE FROM positions")
        db._conn.commit()

    def add(sym, side, entry, qty):
        db.open_position(strategy="S15_HA_CE", symbol=sym, side=side, qty=qty,
                         entry_price=entry, stop_price=entry * 0.99,
                         max_hold_sec=57600, mode="paper", fee_rate=0.0005)

    print("══ 场景 1：空仓（浮亏 0）必须放行 ══")
    clear()
    ok, why, _ = risk.allow_entry(sig, {}, eq, base_cfg)
    check("空仓放行", ok, f"reason={why or '(放行)'}")

    print("\n══ 场景 2：浮亏 4%（低于阈值 8%）必须放行 ══")
    clear()
    for i, s in enumerate(["AUSDT", "BUSDT", "CUSDT"]):
        add(s, "long", 100.0, 10.0)
        PRICES[s] = 80.0
    # 每笔亏 (100-80)*10 = 200U，3 笔 = 600U = 净值 6%
    PRICES["TESTUSDT"] = 100.0
    loss = risk.unrealized_loss_pct(eq)
    ok, why, _ = risk.allow_entry(sig, {}, eq, base_cfg)
    check(f"浮亏 {(loss if loss is not None else -1):.2f}% < 8% 时放行",
          ok and loss is not None and loss < 8,
          f"浮亏={loss if loss is None else round(loss, 2)}% reason={why or '(放行)'}")

    print("\n══ 场景 3：浮亏超 8% 必须拦住开新仓 ══")
    clear()
    for s in ["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"]:
        add(s, "long", 100.0, 10.0)
        PRICES[s] = 80.0          # 每笔 -200U，5 笔 = -1000U = 净值 10%
    PRICES["TESTUSDT"] = 100.0
    loss = risk.unrealized_loss_pct(eq)
    ok, why, _ = risk.allow_entry(sig, {}, eq, base_cfg)
    check("浮亏超阈值 → 拒绝", (not ok) and "浮亏" in why,
          f"浮亏={loss:.2f}% → {why}")

    print("\n══ 场景 4：盈利时必须放行（不能把盈利也算成亏损）══")
    clear()
    for s in ["AUSDT", "BUSDT"]:
        add(s, "long", 100.0, 10.0)
        PRICES[s] = 130.0         # 每笔 +300U
    PRICES["TESTUSDT"] = 100.0
    loss = risk.unrealized_loss_pct(eq)
    ok, why, _ = risk.allow_entry(sig, {}, eq, base_cfg)
    check("盈利时浮亏计为 0", loss == 0.0, f"loss_pct={loss}")
    check("盈利时放行", ok, f"reason={why or '(放行)'}")

    print("\n══ 场景 5：空头同样计算（镜像对称）══")
    clear()
    for s in ["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"]:
        add(s, "short", 100.0, 10.0)
        PRICES[s] = 120.0         # 空头涨了 = 亏损，每笔 -200U
    PRICES["TESTUSDT"] = 100.0
    loss = risk.unrealized_loss_pct(eq)
    ok, why, _ = risk.allow_entry(sig, {}, eq, base_cfg)
    check("空头浮亏同样触发", (not ok) and "浮亏" in why,
          f"空头浮亏={loss:.2f}% → {why}")

    print("\n══ 场景 6：unrealized_halt_pct=0 → 关闭这一级 ══")
    cfg_off = dict(base_cfg, unrealized_halt_pct=0)
    ok, why, _ = risk.allow_entry(sig, {}, eq, cfg_off)
    check("关闭后同样浮亏放行", ok, f"reason={why or '(放行)'}")

    print("\n══ 场景 7：缺价时不猜（不能当 0 处理）══")
    clear()
    add("AUSDT", "long", 100.0, 10.0)
    PRICES["AUSDT"] = 0.0          # 取不到价
    PRICES["TESTUSDT"] = 100.0
    upnl = risk.open_unrealized_pnl("paper")
    loss = risk.unrealized_loss_pct(eq, "paper")
    ok, why, _ = risk.allow_entry(sig, {}, eq, base_cfg)
    check("缺价 → open_unrealized_pnl 返回 None", upnl is None, f"upnl={upnl}")
    check("缺价 → 浮亏比例返回 None（不是 0）", loss is None, f"loss={loss}")
    check("缺价 → 走既有 missing 闸拦截", (not ok) and "取不到行情" in why,
          f"→ {why}")

    print("\n══ 场景 8：跨日不残留（浮亏是实时状态，不落库）══")
    rows = db.get_meta("halt_until", "")
    check("浮亏闸不写 halt_until", not rows, f"meta.halt_until={rows!r}")

    print("\n" + "═" * 60)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("\n  失败项：")
        for f in FAIL:
            print(f"    · {f}")
    print("═" * 60)

    try:
        os.unlink(path)
    except OSError:
        pass
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
