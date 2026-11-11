# -*- coding: utf-8 -*-
"""保本移损（be_at_r）回归验证 —— 2026-11-11 浩哥定 1.5R

验三件事，**都走真代码**（不复制逻辑自证）：
  A. strategies.check_exit 在峰顶 ≥ be_at_r 时置 be_on=1 并落库
  B. engine._hard_stop_reason 在 be_on=1 时用 entry_price 当触发价（reason=保本止损）
  C. 回归：be_at_r=0（关闭）时不置位；be_on=0 时判定行为与改动前完全一致

★ 用临时库，绝不碰 quantbot.db。

用法：.venv/Scripts/python.exe scripts/verify_breakeven.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

OK = FAIL = 0


def chk(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  PASS  {name}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"   {extra}" if extra else ""))


class FakeCtx:
    """最小上下文：只实现 price()，_refresh_peak 只用到它。"""

    def __init__(self, px):
        self.px = px
        self.now = 0

    def price(self, sym):
        return self.px.get(sym) if isinstance(self.px, dict) else self.px


def main():
    from backend import config, db, engine, strategies

    # ── 临时库：复制 schema，不碰生产库
    tmpdir = tempfile.mkdtemp(prefix="be_verify_")
    db_path = os.path.join(tmpdir, "t.db")
    src = sqlite3.connect(os.path.join(ROOT, "quantbot.db"))
    schema = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL")]
    src.close()
    t = sqlite3.connect(db_path)
    for _sql in schema:
        if _sql.lower().startswith("create table sqlite_"):
            continue
        t.execute(_sql)
    t.commit()
    t.close()

    orig_path = config.DB_PATH
    config.DB_PATH = db_path
    db.init()

    print("\n" + "=" * 72)
    print("A · check_exit 置 be_on（真调 strategy.check_exit）")
    print("=" * 72)

    strat = strategies.S15CeZlsma({
        "params": {"be_at_r": 1.5, "half_at_r": 1.2, "full_at_r": 2.0,
                   "right_exit": True, "right_exit_strict": True},
        "max_hold_hours": 0,
    })

    # 多单：入 100，止损 98 → risk=2，1.5R = 103
    pid = db.open_position(
        strategy="S15_CE_ZLSMA", symbol="TESTUSDT", side="long",
        entry_price=100.0, stop_price=98.0, qty=1.0, leverage=6.0,
        stop_src="key", max_hold_sec=0,
    )
    pos = dict(db.get_position(pid))
    print(f"  造多单 id={pid} entry=100 stop=98 risk=2 → 1.5R 价 = 103")

    # 第一轮：现价 102 → 1.0R，不该触发
    r = asyncio.run(strat.check_exit(FakeCtx({"TESTUSDT": 102.0}), pos))
    row = sqlite3.connect(db_path).execute(
        "SELECT be_on FROM positions WHERE id=?", (pid,)).fetchone()
    chk("1.0R 时 be_on 仍为 0", row[0] == 0, f"be_on={row[0]} reason={r}")
    chk("1.0R 时不平仓也不平半", r is None, f"reason={r}")

    # 第二轮：现价 103.1 → 1.55R，应置 be_on=1
    r = asyncio.run(strat.check_exit(FakeCtx({"TESTUSDT": 103.1}), pos))
    row = sqlite3.connect(db_path).execute(
        "SELECT be_on FROM positions WHERE id=?", (pid,)).fetchone()
    chk("1.55R 时 be_on 置 1（并已落库）", row[0] == 1, f"be_on={row[0]}")
    chk("pos 内存对象同步 be_on=1", int(pos.get("be_on") or 0) == 1, f"pos.be_on={pos.get('be_on')}")
    # 1.55R 同时越过平半 1.2R，应当平半
    chk("1.55R 同时触发平半（HALF:）", str(r).startswith("HALF:") if r else False, f"reason={r}")

    # 重启模拟：重新从库里读，be_on 仍在
    db.init()   # 模拟重启：重新建立连接（be_on 必须还在库里）
    pos2 = dict(db.get_position(pid))
    chk("重启后从库读仍是 be_on=1", int(pos2.get("be_on") or 0) == 1, f"be_on={pos2.get('be_on')}")
    chk("stop_price 未被改写（仍是 98，R 的分母还在）",
        abs(float(pos2["stop_price"]) - 98.0) < 1e-9, f"stop={pos2['stop_price']}")

    print("\n" + "=" * 72)
    print("B · engine._hard_stop_reason 用入场价当触发价")
    print("=" * 72)
    p_be = dict(side="long", entry_price=100.0, stop_price=98.0, be_on=1)
    p_raw = dict(side="long", entry_price=100.0, stop_price=98.0, be_on=0)

    r1, b1 = engine._hard_stop_reason(p_be, 100.0)
    chk("多单 be_on=1，价回到入场价 100.0 → 触发", r1 == "保本止损", f"{r1}")
    r2, b2 = engine._hard_stop_reason(p_be, 100.5)
    chk("多单 be_on=1，价 100.5（仍赚）→ 不触发", r2 is None, f"{r2}")
    r3, b3 = engine._hard_stop_reason(p_raw, 100.0)
    chk("多单 be_on=0，价 100.0 → 不触发（反向回归：没保本就不能在入场价平）",
        r3 is None, f"{r3}")
    r4, b4 = engine._hard_stop_reason(p_raw, 97.9)
    chk("多单 be_on=0，价跌破 98 → 触发「止损触发」", r4 == "止损触发", f"{r4}")

    p_be_s = dict(side="short", entry_price=100.0, stop_price=102.0, be_on=1)
    r5, _ = engine._hard_stop_reason(p_be_s, 100.0)
    chk("空单 be_on=1，价回到 100.0 → 触发保本止损", r5 == "保本止损", f"{r5}")
    r6, _ = engine._hard_stop_reason(p_be_s, 99.5)
    chk("空单 be_on=1，价 99.5（仍赚）→ 不触发", r6 is None, f"{r6}")

    print("\n" + "=" * 72)
    print("C · 关闭开关时行为与改动前完全一致")
    print("=" * 72)
    strat_off = strategies.S15CeZlsma({
        "params": {"be_at_r": 0.0, "half_at_r": 1.2, "full_at_r": 2.0,
                   "right_exit": True, "right_exit_strict": True},
        "max_hold_hours": 0,
    })
    pid2 = db.open_position(
        strategy="S15_CE_ZLSMA", symbol="TEST2USDT", side="long",
        entry_price=100.0, stop_price=98.0, qty=1.0, leverage=6.0,
        stop_src="key", max_hold_sec=0,
    )
    pos3 = dict(db.get_position(pid2))
    asyncio.run(strat_off.check_exit(FakeCtx({"TEST2USDT": 105.0}), pos3))
    row = sqlite3.connect(db_path).execute(
        "SELECT be_on FROM positions WHERE id=?", (pid2,)).fetchone()
    chk("be_at_r=0 时涨到 2.5R 也不置 be_on", row[0] == 0, f"be_on={row[0]}")

    print("\n" + "=" * 72)
    print("D · strategies.yaml 实际参数")
    print("=" * 72)
    import yaml
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    P = cfg["strategies"]["S15_CE_ZLSMA"]["params"]
    chk("yaml be_at_r = 1.5", float(P.get("be_at_r") or 0) == 1.5, f"={P.get('be_at_r')}")
    chk("yaml min_stop_dist_pct = 0.60", abs(float(P.get("min_stop_dist_pct") or 0) - 0.60) < 1e-9,
        f"={P.get('min_stop_dist_pct')}")

    config.DB_PATH = orig_path
    # 清理临时库
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass

    print("\n" + "=" * 72)
    print(f"结果：通过 {OK} 项，失败 {FAIL} 项")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
