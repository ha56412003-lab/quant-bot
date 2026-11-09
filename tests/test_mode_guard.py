"""验证「切回纸面模式」的闸门（2026-11-09）。

背景：db.open_positions() 按当前 mode 过滤，engine.manage_positions 正是用它
遍历持仓做止损/出场判断。切到 paper 的那一刻，实盘仓位立刻失去全部管理，
而交易所侧没有止损单 → 永久裸奔；页面还按当前模式查询，显示「0 持仓」骗人。

本测试用临时库 + 打桩，不碰生产数据。
"""
import asyncio
import importlib
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


async def main():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="mode_guard_")
    os.close(fd)
    os.environ["QB_DB_PATH"] = path
    for m in ("backend.db", "backend.executor"):
        if m in sys.modules:
            del sys.modules[m]
    db = importlib.import_module("backend.db")
    executor = importlib.import_module("backend.executor")
    db.init()

    try:
        # ── 场景 1：paper → paper（no-op，不该有任何限制）──
        print("══ 场景 1：纸面切纸面（no-op）══")
        executor.set_mode("paper")
        executor.set_mode("paper")          # 不该抛
        check("纸面切纸面不报错", executor.mode() == "paper", f"mode={executor.mode()}")

        # ── 场景 2：live 有持仓 → 切 paper 必须被拒 ──
        print("\n══ 场景 2：实盘有 2 个持仓时切回纸面 ══")
        executor.set_mode("live")
        db.set_meta("executor_exchange", "binance")
        db.open_position(
            strategy="S15_CE_ZLSMA", symbol="GUARD1USDT", side="long", qty=1.0,
            entry_price=100.0, stop_price=95.0, take_profit=None, trail_atr=None,
            max_hold_sec=16 * 3600, time_exit_at=None, entry_factors="{}",
            reason="闸门测试", leverage=6.0, fee_rate=0.0005, slip_pct=0.0)
        db.open_position(
            strategy="S15_CE_ZLSMA", symbol="GUARD2USDT", side="short", qty=2.0,
            entry_price=50.0, stop_price=55.0, take_profit=None, trail_atr=None,
            max_hold_sec=16 * 3600, time_exit_at=None, entry_factors="{}",
            reason="闸门测试", leverage=6.0, fee_rate=0.0005, slip_pct=0.0)
        check("实盘仓位已建好", len(db.open_positions(mode="live")) == 2,
              f"实盘 {len(db.open_positions(mode='live'))} 条")

        err = None
        try:
            executor.set_mode("paper")
        except RuntimeError as e:
            err = e
        check("实盘有持仓时切 paper 必须报错", err is not None,
              f"{err}" if err else "★ 静默切过去了 —— 实盘仓位将裸奔")
        check("报错后模式必须仍是 live", executor.mode() == "live",
              f"mode={executor.mode()}")
        check("报错信息里要能看出是哪个币",
              err is not None and "GUARD1USDT" in str(err),
              f"{err}" if err else "")

        # ── 场景 3：force=True 可以强制切（测试清理通道）──
        print("\n══ 场景 3：force=True 强制切换（仅供清理）══")
        executor.set_mode("paper", force=True)
        check("force=True 能切过去", executor.mode() == "paper", f"mode={executor.mode()}")
        executor.set_mode("live")

        # ── 场景 4：把实盘仓位平干净后，正常切换应当放行 ──
        print("\n══ 场景 4：实盘仓位清零后切回纸面 ══")
        for p in db.open_positions(mode="live"):
            db.close_position(p["id"], 100.0, 0.0, "清理")
        check("实盘已无持仓", len(db.open_positions(mode="live")) == 0,
              f"剩余 {len(db.open_positions(mode='live'))}")
        executor.set_mode("paper")
        check("清仓后可以正常切回纸面", executor.mode() == "paper", f"mode={executor.mode()}")

        # ── 场景 5：纸面持仓不影响切回纸面 ──
        print("\n══ 场景 5：纸面自己的持仓不该被这道闸拦住 ══")
        db.open_position(
            strategy="S15_CE_ZLSMA", symbol="PAPER1USDT", side="long", qty=1.0,
            entry_price=10.0, stop_price=9.0, take_profit=None, trail_atr=None,
            max_hold_sec=16 * 3600, time_exit_at=None, entry_factors="{}",
            reason="闸门测试", leverage=6.0, fee_rate=0.0005, slip_pct=0.0)
        executor.set_mode("paper")           # 当前就是 paper，no-op
        check("纸面有仓时重复切纸面不报错", executor.mode() == "paper", f"mode={executor.mode()}")

    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    print("\n" + "═" * 60)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("\n  失败项：")
        for f in FAIL:
            print(f"    · {f}")
    print("═" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
