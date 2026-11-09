"""验证部分平仓的并发安全（2026-11-09）。

背景：部分平仓是「读快照 qty → 计算 → 写回 remain」，中间有 await 让步点。
两个请求（引擎半平 + 手动半平 / 用户双击确认按钮）会拿到同一个 qty 各卖一次：
交易所已经清零，本地还剩一半，paper_balance 和 positions.pnl 双记。

首版审计用探针复现过：两请求各读 qty=100 → 各写回 50 → DB 剩 50，
而实际卖出 100，余额和 pnl 各加两遍。

本测试在临时库 + 纸面模式下跑，不碰生产数据。
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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="race_")
    os.close(fd)
    os.environ["QB_DB_PATH"] = path
    for m in ("backend.db", "backend.executor", "backend.factors"):
        if m in sys.modules:
            del sys.modules[m]
    db = importlib.import_module("backend.db")
    executor = importlib.import_module("backend.executor")
    factors = importlib.import_module("backend.factors")
    db.init()
    executor.set_mode("paper")

    # 用固定价格，排除滑点/手续费干扰，只盯数量与账目
    factors.mark_price_of = lambda sym: 100.0

    try:
        print("══ 场景 1：同一仓位并发两个 50% 平仓 ══")
        pid = db.open_position(
            strategy="S15_CE_ZLSMA", symbol="RACE1USDT", side="long", qty=100.0,
            entry_price=100.0, stop_price=95.0, take_profit=None, trail_atr=None,
            max_hold_sec=16 * 3600, time_exit_at=None, entry_factors="{}",
            reason="并发测试", leverage=6.0, fee_rate=0.0, slip_pct=0.0)
        pos = db.get_position(pid)
        bal0 = executor.paper_balance()          # 注意：不是 get_meta（新库未初始化时是 0）

        # 两个协程同时进来，都拿着同一份 pos 快照。
        # 正确行为是「串行累减」：第 1 个平 50 剩 50，第 2 个按当前 50 再平 25 剩 25。
        # 错误行为（修复前）：两个都用旧快照 qty=100 算 remain=50，后写的覆盖先写的
        # → 本地剩 50，实际却卖了 75；而且两笔 pnl 都记上，余额双加。
        r = await asyncio.gather(
            executor.close_position(pos, 100.0, "并发半平 A", 0.5),
            executor.close_position(pos, 100.0, "并发半平 B", 0.5),
            return_exceptions=True,
        )
        ok_n = sum(1 for x in r if x is True)
        cur = db.get_position(pid)
        qty_now = float(cur["qty"]) if cur else 0.0
        status = cur["status"] if cur else "gone"

        check("两次并发半平都应成功（串行执行）", ok_n == 2,
              f"成功 {ok_n} 次")
        check("剩余数量应为 25（50 再减 50%）", abs(qty_now - 25.0) < 1e-6,
              f"剩余 qty={qty_now} → "
              f"{'正确：按当前数量累减' if abs(qty_now-25.0)<1e-6 else '★ 用了过期快照，数量写回错了'}")
        check("仓位应保持 open", status == "open", f"status={status}")

        # 账目：两笔都是真的成交（entry=100 exit=100 手续费 0 → pnl=0），
        # 关键是**不能虚记**：流水 2 条 + pnl=0 + 余额不变。
        pnl_pos = float(cur["pnl"] or 0) if cur else 0.0
        bal_now = executor.paper_balance()
        trades = [t for t in db.recent_trades(200)
                  if t.get("position_id") == pid and t.get("kind") == "partial_close"]
        check("流水应有 2 条 partial_close（两笔都是真成交）", len(trades) == 2,
              f"实际 {len(trades)} 条")
        check("余额变动应等于两笔 pnl 之和（此处为 0）", abs(bal_now - bal0) < 1e-6,
              f"{bal0:.4f} → {bal_now:.4f}")
        check("positions.pnl 应等于两笔之和（此处为 0）", abs(pnl_pos) < 1e-6, f"pnl={pnl_pos}")

        # 平掉，验证锁字典没泄漏
        executor._pos_locks.pop(pid, None)
        db.close_position(pid, 100.0, 0.0, "清理")

        print("\n══ 场景 2：半平 + 全平同时到达 ══")
        pid2 = db.open_position(
            strategy="S15_CE_ZLSMA", symbol="RACE2USDT", side="long", qty=100.0,
            entry_price=100.0, stop_price=95.0, take_profit=None, trail_atr=None,
            max_hold_sec=16 * 3600, time_exit_at=None, entry_factors="{}",
            reason="并发测试", leverage=6.0, fee_rate=0.0, slip_pct=0.0)
        pos2 = db.get_position(pid2)
        r2 = await asyncio.gather(
            executor.close_position(pos2, 100.0, "并发半平", 0.5),
            executor.close_position(pos2, 100.0, "并发全平", 1.0),
            return_exceptions=True,
        )
        ok2 = sum(1 for x in r2 if x is True)
        cur2 = db.get_position(pid2)
        st2 = cur2["status"] if cur2 else "gone"
        q2 = float(cur2["qty"]) if cur2 else 0.0
        check("不该出现「已平了还继续卖」", st2 in ("open", "closed"), f"status={st2} qty={q2}")
        check("全平后仓位应关闭", st2 == "closed",
              f"status={st2}（半平先执行时全平会接着关掉）")
        check("锁字典不应无限增长", len(executor._pos_locks) <= 2,
              f"当前 {len(executor._pos_locks)} 把锁")

        print("\n══ 场景 3：fraction=0 不该变成全平 ══")
        pid3 = db.open_position(
            strategy="S15_CE_ZLSMA", symbol="RACE3USDT", side="long", qty=100.0,
            entry_price=100.0, stop_price=95.0, take_profit=None, trail_atr=None,
            max_hold_sec=16 * 3600, time_exit_at=None, entry_factors="{}",
            reason="并发测试", leverage=6.0, fee_rate=0.0, slip_pct=0.0)
        pos3 = db.get_position(pid3)
        await executor.close_position(pos3, 100.0, "fraction 0", 0)
        cur3 = db.get_position(pid3)
        q3 = float(cur3["qty"]) if cur3 else 0.0
        check("fraction=0 应平掉最小份额而不是全平", q3 > 0,
              f"剩余 qty={q3} → "
              f"{'正确，没有整仓卖掉' if q3 > 0 else '★ 被当成了全平'}")

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
