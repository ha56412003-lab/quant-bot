# -*- coding: utf-8 -*-
"""验证实盘平仓的安全性问题（不碰生产库，纯打桩）。

问题：executor.close_position 的 live 分支
    await binance.place_market_order(...)     ← 返回值被丢弃
    fill = price                              ← 用本地价记账
如果交易所拒单 / 返回空 / 部分成交，代码仍然往下走，把仓位标记为已平。

本脚本用打桩证明这个行为，不产生任何真实下单，也不写生产库。
"""
import os
import sys
import tempfile
import asyncio

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# ── 换成临时库，绝不碰生产 ──────────────────────────────
_TMP = os.path.join(tempfile.gettempdir(), f"exec_audit_{os.getpid()}.db")
os.environ["QB_DB_PATH"] = _TMP
if os.path.exists(_TMP):
    os.remove(_TMP)

from backend import config, db, executor, binance  # noqa: E402

db.init()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}")
    if detail:
        print(f"      {detail}")


async def main():
    print("DB:", _TMP)
    print()

    executor.set_mode("live")
    db.set_meta("executor_exchange", "binance")

    # 造一笔仓位（临时库）—— 必须在切到 live 模式之后造，
    # 否则 db.open_positions() 会按当前模式 paper 过滤，查不到这笔
    pid = db.open_position(
        strategy="S15_CE_ZLSMA", symbol="TESTUSDT", side="long", qty=10.0,
        entry_price=100.0, stop_price=95.0, take_profit=None, trail_atr=None,
        max_hold_sec=16 * 3600, time_exit_at=None, entry_factors="{}",
        reason="审计测试", leverage=6.0, fee_rate=0.0005, slip_pct=0.0)
    print(f"造仓 pid={pid}")

    # ══ 打桩：模拟交易所拒单（返回 None，代表下单没成功）══
    calls = {"n": 0}

    async def fake_step(sym):
        return 0.001

    async def fake_hedge():
        return False

    async def fake_place_reject(*a, **kw):
        """模拟拒单：交易所返回 None（或异常都被外层吞了）"""
        calls["n"] += 1
        return None

    async def fake_place_partial(*a, **kw):
        """模拟只成交 1/10 数量"""
        calls["n"] += 1
        return {"avgPrice": "100.0", "executedQty": "1.0",
                "cummulativeQuoteQty": "100.0", "status": "PARTIALLY_FILLED"}

    binance.qty_step = fake_step
    executor._is_hedge = fake_hedge
    binance.place_market_order = fake_place_reject

    pos = db.open_positions()[0]

    print("\n══ 场景 1：交易所拒单（place_market_order 返回 None）══")
    ok = await executor.close_position(pos, 100.5, "审计测试平仓")
    check("close_position 应该返回 False（拒单不能算成功）",
          ok is False,
          f"实际返回 {ok}  → {'★ 系统以为平掉了，实际交易所有仓' if ok else '正确拒绝'}")
    still = db.open_positions()
    check("仓位应该还在（拒单后不能标记已平）",
          len(still) == 1,
          f"实际剩余仓位 {len(still)} 个 → {'★ 仓位被错误标记为已平' if len(still)==0 else '正确保留'}")
    print(f"     （收盘调用次数 {calls['n']}）")

    # 重置状态再测第二个场景
    calls["n"] = 0
    if not db.open_positions():
        print("\n  [仓位已被误平，重建一笔再测场景 2]")
        db.open_position(
            strategy="S15_CE_ZLSMA", symbol="TEST2USDT", side="long", qty=10.0,
            entry_price=100.0, stop_price=95.0, take_profit=None, trail_atr=None,
            max_hold_sec=16 * 3600, time_exit_at=None, entry_factors="{}",
            reason="审计测试2", leverage=6.0, fee_rate=0.0005, slip_pct=0.0)

    print("\n══ 场景 2：只成交 1/10（部分成交）══")
    binance.place_market_order = fake_place_partial
    pos = db.open_positions()[0]
    ok = await executor.close_position(pos, 100.5, "审计测试平仓2")
    remaining = db.open_positions()
    check("部分成交时仓位不应被标记为全平",
          len(remaining) == 1,
          f"实际剩余 {len(remaining)} 个 → "
          f"{'★ 交易所有 9/10 的仓，本地记成已全平 → 裸仓' if len(remaining)==0 else '正确保留'}")

    print("\n══ 场景 3：记账价用的是本地价还是成交价 ══")
    # 让成交价故意偏离 5%
    async def fake_place_far(*a, **kw):
        return {"avgPrice": "105.0", "executedQty": "10.0",
                "cummulativeQuoteQty": "1050.0", "status": "FILLED"}

    # 场景 2 会留下 TEST2USDT 的仓位，所以这里必须按 symbol 精确定位，
    # 否则 [0] 取到的是上一场景的仓位（这就是首版测试误报的原因）。
    _p3 = [p for p in db.open_positions() if p["symbol"] == "TEST3USDT"]
    if not _p3:
        db.open_position(
            strategy="S15_CE_ZLSMA", symbol="TEST3USDT", side="long", qty=10.0,
            entry_price=100.0, stop_price=95.0, take_profit=None, trail_atr=None,
            max_hold_sec=16 * 3600, time_exit_at=None, entry_factors="{}",
            reason="审计测试3", leverage=6.0, fee_rate=0.0005, slip_pct=0.0)
        _p3 = [p for p in db.open_positions() if p["symbol"] == "TEST3USDT"]
    binance.place_market_order = fake_place_far
    pos = _p3[0]
    # 本地价 100.5，交易所实际成交 105.0
    await executor.close_position(pos, 100.5, "审计测试平仓3")
    closed = [c for c in db.closed_positions_today(0, "live")
              if c["symbol"] == "TEST3USDT"]
    real_pnl = (105.0 - 100.0) * 10.0 - 105.0 * 10.0 * 0.0005   # 真实应为 +49.48
    booked = closed[0]["pnl"] if closed else None
    check("记账 PnL 应该接近真实成交价算出的 +49.48",
          booked is not None and abs(booked - real_pnl) < 1.0,
          f"账上 {booked}  vs 真实 {real_pnl:.2f}  → "
          f"{'★ 用本地价 100.5 记成了 %s，偏差 %.2f U' % (booked, real_pnl - (booked or 0)) if booked is not None and abs(booked-real_pnl)>=1.0 else ''}")

    print()
    print("═" * 60)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("\n  失败项（都是真问题）：")
        for f in FAIL:
            print(f"    · {f}")
    print("═" * 60)

    executor.set_mode("paper", force=True)
    try:
        os.remove(_TMP)
    except OSError:
        pass
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
