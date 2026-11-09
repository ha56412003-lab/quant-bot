# -*- coding: utf-8 -*-
"""验证实盘开仓的安全性（不碰生产库，纯打桩）。

问题：executor.open_position 的 live 分支
    await binance.place_market_order(...)     ← 第 145 行，钱已经进去了
    ...
    pid = db.open_position(...)               ← 第 192 行，在 try 外面

如果第 145 行下单成功、第 192 行写库失败（库被锁 / 磁盘满 / 字段超长），
异常会一路抛到 engine.py 的 try，被当成「本轮扫描失败」记一笔日志。
结果：交易所有仓，本地零记录 → 没人管这个仓，没有止损、没有出场。
"""
import os
import sys
import tempfile
import asyncio

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMP = os.path.join(tempfile.gettempdir(), f"exec_open_audit_{os.getpid()}.db")
os.environ["QB_DB_PATH"] = _TMP
if os.path.exists(_TMP):
    os.remove(_TMP)

from backend import config, db, executor, binance, factors  # noqa: E402
from backend.strategies import Signal  # noqa: E402

db.init()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}")
    if detail:
        print(f"      {detail}")


async def main():
    executor.set_mode("live")
    db.set_meta("executor_exchange", "binance")

    order_log = []

    async def fake_step(sym):
        return 0.001

    async def fake_hedge():
        return False

    async def fake_margin(sym):
        return "isolated"

    async def fake_place_ok(*a, **kw):
        order_log.append({"args": a, "kwargs": kw})
        return {"avgPrice": "100.5", "executedQty": "15.0",
                "cummulativeQuoteQty": "1507.5", "status": "FILLED"}

    binance.qty_step = fake_step
    binance.place_market_order = fake_place_ok
    binance.margin_of = fake_margin
    binance.set_margin_type = lambda *a, **kw: asyncio.sleep(0)
    binance.set_leverage = lambda *a, **kw: asyncio.sleep(0)
    executor._is_hedge = fake_hedge
    factors.mark_price_of = lambda sym: 100.0

    sig = Signal(
        strategy="S15_CE_ZLSMA", symbol="AUDITUSDT", side="long",
        reason="开仓审计", stop_price=95.0, take_profit=None,
        trail_dist=None, max_hold_sec=16 * 3600, time_exit_at=None,
        size_mult=1.0, fee_rate=0.0005, slip_pct=0.0,
    )

    print("══ 场景 A：下单成功 + 写库成功（正常路径）══")
    pid = await executor.open_position(sig, 15.0, 100.0, leverage=6)
    check("正常路径应该返回 position id", pid is not None, f"pid={pid}")
    check("订单确实发出去了", len(order_log) == 1, f"下单次数 {len(order_log)}")

    # 清干净，进入场景 B
    for p in db.open_positions():
        db.close_position(p["id"], 100.0, 0.0, "清理")
    order_log.clear()

    print("\n══ 场景 B：下单成功，但写库失败 ══")
    real_open = db.open_position

    def boom(**kw):
        raise RuntimeError("database is locked（模拟写库失败）")

    db.open_position = boom
    order_log.clear()
    exc = None
    try:
        await executor.open_position(sig, 15.0, 100.0, leverage=6)
    except Exception as e:  # noqa: BLE001
        exc = e
    db.open_position = real_open

    check("写库失败时，订单已经真实发出去了",
          len(order_log) == 1,
          f"下单次数 {len(order_log)} → "
          f"{'★ 钱已经进了交易所' if len(order_log) >= 1 else '未下单'}")
    local = db.open_positions()
    check("本地应该有这条仓位的记录（否则无人管）",
          len(local) == 1,
          f"本地仓位数 {len(local)} → "
          f"{'★ 交易所有仓、本地零记录 → 裸仓，无止损无出场' if len(local) == 0 else '有记录'}")
    check("写库失败应该被显式处理（返回 None + 告警），不该裸抛异常",
          False,   # 当前实现一定会抛
          f"实际抛出：{type(exc).__name__}: {exc}" if exc else "未抛异常")

    print("\n══ 场景 C：qty 被 round_step 归零 ══")
    order_log.clear()

    async def tiny_step(sym):
        return 10.0        # 步长 10，qty=15 → round 后 10；qty=5 → round 后 0

    binance.qty_step = tiny_step
    pid2 = await executor.open_position(sig, 5.0, 100.0, leverage=6)
    check("数量小于最小步长时不应下单",
          len(order_log) == 0,
          f"下单次数 {len(order_log)} → "
          f"{'★ 下了个 0 数量的单，币安会拒' if len(order_log) > 0 else '正确拦截'}")
    check("此时应返回 None", pid2 is None, f"返回 {pid2}")

    print()
    print("═" * 60)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("\n  失败项：")
        for f in FAIL:
            print(f"    · {f}")
    print("═" * 60)

    executor.set_mode("paper")
    try:
        os.remove(_TMP)
    except OSError:
        pass
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
