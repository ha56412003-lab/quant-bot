# -*- coding: utf-8 -*-
"""VS15-1.5 两道新规则的代码级验证。

第④条：持仓中 CE 翻转 + 峰顶 < be_at_r → 硬止损收紧到「关键K线端点 ∓ 0.2×ATR20」
第⑦道：abs(HA收盘 − ZLSMA50) / ATR20 ≥ zlsma_ext_max(1.2) → 拒绝开仓

跑法：cd E:/quant-bot && .venv/Scripts/python.exe tests/test_v15_rules.py
"""
import asyncio
import datetime
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS = 0
FAIL = 0
FAILS = []


def chk(cond, name, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL  " + name + ("  <- " + detail if detail else ""))


# ══════════════════════════════════════════════════════════════════
# A. 第④条：engine._hard_stop_reason 的有效止损选择
# ══════════════════════════════════════════════════════════════════
def test_hard_stop_selection():
    from backend import engine

    print("\n[A] 硬止损有效价选择（engine._hard_stop_reason）")

    def pos(**kw):
        d = dict(symbol="TESTUSDT", side="long", entry_price=100.0,
                 stop_price=95.0, be_on=0, stop_tight=None, stop_tight_on=0)
        d.update(kw)
        return d

    # A1 未武装 → 用原止损
    chk(engine._hard_stop_reason(pos(), 94.0) == ("止损触发", False),
        "A1 未武装 → 原止损 95 触发"
        " 实际 %s" % (engine._hard_stop_reason(pos(), 94.0),))

    # A2 未武装且没破 → 不触发
    chk(engine._hard_stop_reason(pos(), 96.0) == (None, False),
        "A2 未武装、价 96 在 95 上方 → 不触发")

    # A3 已武装 tight=97 → 96.9 就触发（比原止损 95 早）
    p = pos(stop_tight=97.0, stop_tight_on=1)
    r = engine._hard_stop_reason(p, 96.9)
    chk(r == ("收紧止损", False), "A3 已武装 → 96.9 触发「收紧止损」 实际 %s" % (r,))

    # A4 已武装但价在 tight 上方 → 不触发
    chk(engine._hard_stop_reason(p, 97.5) == (None, False),
        "A4 已武装、价 97.5 在 97 上方 → 不触发")

    # A5 be_on 优先于第④条（理论上互斥，但历史数据可能都=1）→ 用入场价
    p2 = pos(be_on=1, stop_tight=97.0, stop_tight_on=1)
    r = engine._hard_stop_reason(p2, 99.5)
    chk(r == ("保本止损", True), "A5 be_on=1 优先 → 保本止损 实际 %s" % (r,))

    # A6 空头方向：止损在上方，收紧价必须更高才叫「更紧」
    ps = pos(side="short", stop_price=105.0, stop_tight=103.0, stop_tight_on=1)
    chk(engine._hard_stop_reason(ps, 103.5) == ("收紧止损", False),
        "A6 空头已武装 → 103.5 触发收紧止损")
    chk(engine._hard_stop_reason(ps, 102.5) == (None, False),
        "A7 空头已武装、价 102.5 在 103 下方 → 不触发")

    # A8 武装标记在但 tight 落空（老数据迁移）→ 回落原止损，不许报错
    p3 = pos(stop_tight=0, stop_tight_on=1)
    chk(engine._hard_stop_reason(p3, 94.0) == ("止损触发", False),
        "A8 stop_tight_on=1 但 stop_tight=0 → 回落原止损（不崩）")

    # A9 stop_price 缺失 → 直接不判（老行为不变）
    chk(engine._hard_stop_reason(pos(stop_price=None), 1.0) == (None, False),
        "A9 stop_price 缺失 → 不触发（老行为不变）")


# ══════════════════════════════════════════════════════════════════
# B. Signal 默认值 + 数据库落库字段
# ══════════════════════════════════════════════════════════════════
def test_signal_and_db():
    print("\n[B] Signal 字段 + positions 表两列")
    from backend import strategies as S

    sig = S.Signal(strategy="S15_CE_ZLSMA", symbol="X", side="long",
                   reason="", stop_price=1.0)
    chk(getattr(sig, "stop_tight", "MISSING") == 0.0,
        "B1 Signal.stop_tight 默认 0.0（没有第④条的单）")

    # 用临时库跑一遍迁移，再 PRAGMA 看列真的加上了
    tmpdb = os.path.join(r"E:/Hermes/cache/scratch", "v15_rules_test.db")
    try:
        if os.path.exists(tmpdb):
            os.remove(tmpdb)
    except OSError:
        pass
    os.environ["QB_DB_PATH"] = tmpdb
    for m in [k for k in list(sys.modules) if k.startswith("backend")]:
        del sys.modules[m]
    from backend import db
    db.init()
    con = sqlite3.connect(tmpdb)
    cols = {r[1]: r[2] for r in con.execute("PRAGMA table_info(positions)")}
    con.close()
    chk("stop_tight" in cols, "B2 positions 表有 stop_tight 列", str(cols))
    chk(cols.get("stop_tight_on", "").upper() == "INTEGER",
        "B3 positions 表有 stop_tight_on 列（INTEGER）", str(cols.get("stop_tight_on")))
    chk("stop_tight" in db._POSITION_WRITABLE and "stop_tight_on" in db._POSITION_WRITABLE,
        "B4 两个新列都在 update_position 白名单里（否则运行时 ValueError）")
    try:
        os.remove(tmpdb)
    except OSError:
        pass
    os.environ.pop("QB_DB_PATH", None)
    for m in [k for k in list(sys.modules) if k.startswith("backend")]:
        del sys.modules[m]


# ══════════════════════════════════════════════════════════════════
# C. 第⑦道闸：用 QNTUSDT 11-13 21:30 那根真数据做回归
# ══════════════════════════════════════════════════════════════════
class FakeCtx:
    """最小可用 ctx：只有一个币、只喂一段 K 线。"""

    def __init__(self, sym, ks):
        self.universe = [sym]
        self.monitor = None
        self.sym = sym
        self.ks = ks

    async def klines(self, sym, iv, need):
        return self.ks

    def open_side(self, sym, sid):
        return None

    def price(self, sym):
        return float(self.ks[-1]["close"])


def test_gate7_qnt():
    print("\n[C] 第⑦道闸回归：QNTUSDT 2026-11-13 21:30（偏离 2.66 ATR）")
    from backend import strategies as S

    path = os.path.join(HERE, "fixtures", "qnt_20261113_2130.json")
    if not os.path.exists(path):
        chk(False, "C0 找不到 QNTUSDT 夹具", path)
        return
    bars = json.load(open(path, encoding="utf-8"))

    want_ts = int(datetime.datetime(2026, 11, 13, 21, 30,
                                    tzinfo=datetime.timezone(datetime.timedelta(hours=8))
                                    ).timestamp())
    idx = None
    for k, b in enumerate(bars):
        if int(b["ts"]) == want_ts:
            idx = k
            break
    if idx is None:
        chk(False, "C0 数据里找不到 2026-11-13 21:30 那根", "want_ts=%d" % want_ts)
        return
    print("      目标 K 线位置 %d / 共 %d 根" % (idx, len(bars)))

    base = {"max_hold_hours": 0, "symbols": ["QNTUSDT"], "params": {}}
    base["params"].update({
        "key_bar": True, "key_mult": 0.9, "key_closepos": 0.6, "key_bodyfrac": 0.5,
        "stop_key": True, "stop_key_atr_buf": 0.3, "stop_key_atr_buf_tight": 0.2,
        "ce_mult": 2.0, "ce_len": 1, "zlsma_len": 50,
        "bbw_pct_min": 60.0, "bbw_len": 20, "bbw_pct_win": 100,
        "eff_min_signal": 0.30, "eff_win_signal": 48,
        "min_stop_dist_pct": 0.6, "max_risk_pct": 5.0, "dual_stop_min_pct": 0.5,
        "swing_lookback": 8, "use_close": True, "use_ha": True,
        "fee_rate": 0.0005, "slip_pct": 0.0,
    })

    def run(zl_max, tight_max=0.2):
        cfg = {"_iid": "TEST", "params": dict(base["params"]),
               "symbols": ["QNTUSDT"], "max_hold_hours": 0}
        cfg["params"]["zlsma_ext_max"] = zl_max
        cfg["params"]["stop_key_atr_buf_tight"] = tight_max
        inst = S.S15CeZlsma(cfg)
        # 清模块级缓存（复现铁律：_LAST_BAR / _LEG_ENTERED）
        try:
            S._LAST_BAR.clear()
            S._LEG_ENTERED.clear()
        except Exception:  # noqa: BLE001
            pass
        ks = bars[max(0, idx - 298): idx + 2]      # len=300 → i = len-2 = idx
        ctx = FakeCtx("QNTUSDT", ks)
        out = asyncio.run(inst.evaluate(ctx))
        st = dict(S._GATE_STATE.get("QNTUSDT") or {})
        return out, st

    out0, st0 = run(0.0)
    chk(len(out0) == 1, "C1 zlsma_ext_max=0（关闸）→ 这根照样出信号（基线不为空）",
        "signals=%d why=%s" % (len(out0), st0.get("why")))
    if out0:
        s0 = out0[0]
        print("      基线信号：side=%s stop=%.6f stop_tight=%s reason=%s"
              % (s0.side, s0.stop_price, getattr(s0, "stop_tight", "?"), s0.reason))
        chk(float(getattr(s0, "stop_tight", 0) or 0) > 0
            or st0.get("zld") is None,
            "C2 基线信号带了第④条收紧价（或该根 CE 与持仓反向 → 不带，属正常）")

    out1, st1 = run(1.2)
    chk(len(out1) == 1, "C3 zlsma_ext_max=1.2 → 这根偏离 0.91 < 1.2，仍然通过"
        "（★ 线上用 HA 收盘，不是原始收盘）", "signals=%d zd=%s" % (len(out1), st1.get("zld")))
    zld = st1.get("zld")
    chk(zld is not None and abs(float(zld) - 0.91) < 0.03,
        "C4 量出来的偏离 ≈ 0.91 ATR（HA 收盘 165.94 口径）实际 %s" % (zld,))

    out2, st2 = run(0.8)
    chk(len(out2) == 0, "C5 zlsma_ext_max=0.8 → 0.91 ≥ 0.8，这根被拒", "signals=%d" % len(out2))
    chk("ZLSMA偏离" in str(st2.get("why") or ""),
        "C6 拒绝理由写的是「ZLSMA偏离…」 实际 %s" % (st2.get("why"),))

    # C7 关闸必须真的关：zlsma_ext_max=0 时不出现偏离拦截
    chk("ZLSMA偏离" not in str(st0.get("why") or ""),
        "C7 zlsma_ext_max=0 时不会出现偏离拦截（关得掉）")

    # C8 第④条参数 = 0 → 这笔根本不带收紧价（关得掉）
    #    ★ 这是上线后补的漏洞：曾经只靠「算不出 tight」来关 ④，
    #      但重启前开的仓（当时默认 0.2）已经带着 tight 落库 → 关参数也照样武装。
    out3, _ = run(0.0, 0.0)
    if out3:
        _t3 = float(getattr(out3[0], "stop_tight", -1) or 0)
        chk(abs(_t3) < 1e-12,
            "C8 stop_key_atr_buf_tight=0 → stop_tight=0（第④条不存在）",
            "实际 %s" % getattr(out3[0], "stop_tight", "?"))
    else:
        chk(False, "C8 stop_key_atr_buf_tight=0 的基线信号丢了（不该）")

    # C9 现网门槛 1.5 → 0.91 通过
    out4, _st4 = run(1.5, 0.0)
    chk(len(out4) == 1, "C9 zlsma_ext_max=1.5（现网值）→ 0.91 < 1.5 通过",
        "signals=%d" % len(out4))


def main():
    test_hard_stop_selection()
    test_signal_and_db()
    test_gate7_qnt()
    print("\n" + "=" * 70)
    if FAIL:
        print("结果：%d 通过 / %d 失败" % (PASS, FAIL))
        print("失败项：" + "、".join(FAILS))
    else:
        print("结果：%d 项全过（VS15-1.5 两道规则逻辑验证）" % PASS)
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
