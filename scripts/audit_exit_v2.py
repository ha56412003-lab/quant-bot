# -*- coding: utf-8 -*-
"""出场改版（ef77e40）数据逻辑全方位审计。

目的：接实盘前，把「线上 check_exit」与「回测 sim()」在同一批 K 线上逐笔对照，
      并检查状态持久化、多空对称、浮点边界、并发隔离等实盘才会暴露的问题。

跑法： PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/audit_exit_v2.py
"""
import asyncio
import glob
import json
import sys
import time

sys.path.insert(0, '.')

import yaml
from backend.strategies import S15CeZlsma, _ce_zlsma
import backend.strategies as S
from backtest.s15_live_parity import stats

OK = FAIL = 0
ISSUES = []


def chk(cond, name, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  PASS  %-52s %s" % (name, detail))
    else:
        FAIL += 1
        ISSUES.append(name)
        print("  FAIL  %-52s %s" % (name, detail))


def sec(t):
    print()
    print("=" * 104)
    print(t)
    print("=" * 104)


# ══════════════════════════════════════════════════════════════════════
# 0. 载入配置 / K 线
# ══════════════════════════════════════════════════════════════════════
BLK = yaml.safe_load(open('strategies.yaml', encoding='utf-8'))['strategies']['S15_CE_ZLSMA']
ST = S15CeZlsma(BLK)

BARS = []
for fp in sorted(glob.glob("backtest/data/*_15m_60d.json")):
    ks = json.load(open(fp, encoding="utf-8"))
    if len(ks) < 800:
        continue
    sym = fp.split("\\")[-1].split("/")[-1].replace("_15m_60d.json", "")
    BARS.append((sym, ks))

sec("一、参数实读（yaml → 代码，实盘会真正用到的值）")
for k, d, want in [
    ("half_at_r", 2.0, 1.8), ("half_tp_r", 1.0, 1.0),
    ("trail_arm", 0.0, 1.5), ("trail_dd", 0.0, 1.2),
    ("quickfail_hours", 4.0, 4.0), ("quickfail_min_r", 0.5, 0.5),
    ("swing_lookback", 20, 20), ("ce_len", 1, 1),
    ("ce_mult", 2.0, 2.0), ("zlsma_len", 50, 50),
    ("max_risk_pct", 30.0, 5.0), ("bbw_pct_min", 70.0, 70.0),
    ("min_dev_pct", 0.5, 0.5),
]:
    got = ST._p(k, d)
    chk(got == want, "%s" % k, "= %r（期望 %r）" % (got, want))
chk(ST.max_hold == 16 * 3600, "max_hold_hours", "= %d 秒 = %d 小时"
    % (ST.max_hold, ST.max_hold // 3600))

# ══════════════════════════════════════════════════════════════════════
# 1. 顺序与优先级
# ══════════════════════════════════════════════════════════════════════
sec("二、出场分支优先级（顺序错了会让高优先级的单子被低优先级抢走）")


class Ctx:
    """最小可用的 Ctx：固定价 + 截止到某根的 K 线"""
    def __init__(self, px, ks=None, upto=None):
        self._px = px
        self._ks = ks
        self._upto = upto

    def price(self, sym):
        return self._px

    async def klines(self, sym, interval=None, limit=None):
        if self._ks is None:
            return []
        up = self._upto if self._upto is not None else len(self._ks)
        return self._ks[max(0, up - 200):up]


def mkpos(side="long", entry=100.0, stop=98.0, half=0, peak=0.0, age_h=1.0):
    return dict(id=1, symbol="TESTUSDT", side=side, entry_price=entry, stop_price=stop,
                opened_at=time.time() - age_h * 3600, half_closed=half,
                peak_r=peak, mfe_pct=0.0, ma_pct=0.0, qty=1.0)


async def probe(desc, px, half=0, peak=0.0, age_h=1.0, expect=None):
    S._S15_POS_STATE.clear()
    r = await ST.check_exit(Ctx(px), mkpos(half=half, peak=peak, age_h=age_h))
    cur = (px - 100.0) / 2.0
    good = (expect is None and r is None) or (expect and expect in (r or ""))
    chk(good, desc, "cur=%+.2fR → %s" % (cur, r or "不动"))


asyncio.run(probe("1.7R 未过平半线", 100 + 1.7 * 2))
asyncio.run(probe("1.9R 过 1.8 线 → 平半", 100 + 1.9 * 2, expect="HALF"))
asyncio.run(probe("平半后 2.79R 不到 3R", 100 + 2.79 * 2, half=1, peak=2.79))
asyncio.run(probe("平半后 恰好 2.80R → 3R全平", 100 + 2.80 * 2, half=1, peak=2.80, expect="半仓再赚"))
asyncio.run(probe("平半后 peak3.0 现1.5R → 移动止盈", 100 + 1.5 * 2, half=1, peak=3.0, expect="移动止盈"))
asyncio.run(probe("平半后 peak3.0 现1.7R（回吐1.3但>2.8?否）", 100 + 1.7 * 2, half=1, peak=3.0, expect="移动止盈"))
asyncio.run(probe("平半后 peak2.6 现1.5R（回吐1.1不够）", 100 + 1.5 * 2, half=1, peak=2.6))
asyncio.run(probe("平半后 peak2.6 现1.4R（回吐1.2刚好）", 100 + 1.4 * 2, half=1, peak=2.6, expect="移动止盈"))
asyncio.run(probe("未平半 peak3.0 现1.0R → 移动止盈不武装", 100 + 1.0 * 2, half=0, peak=3.0))
asyncio.run(probe("5h 内 peak0.3R → 快速失败", 100 + 0.3 * 2, half=0, peak=0.3, age_h=5.0, expect="快速失败"))
asyncio.run(probe("3h 内 peak0.3R → 快速失败还没到点", 100 + 0.3 * 2, half=0, peak=0.3, age_h=3.0))

# 优先级：3R 全平 vs 移动止盈（同 tick 都满足）
S._S15_POS_STATE.clear()
_r = asyncio.run(ST.check_exit(Ctx(100 + 3.0 * 2), mkpos(half=1, peak=5.0, age_h=1.0)))
chk("半仓再赚" in (_r or ""), "3R全平 优先于 移动止盈（回测口径一致）", _r or "")

# ══════════════════════════════════════════════════════════════════════
# 2. 多空对称
# ══════════════════════════════════════════════════════════════════════
sec("三、多空对称（做空应当镜像：止损在上方，R 用 entry-stop 的绝对值）")


async def probe_short(desc, px, half=0, peak=0.0, expect=None):
    S._S15_POS_STATE.clear()
    p = dict(id=2, symbol="TESTUSDT", side="short", entry_price=100.0, stop_price=102.0,
             opened_at=time.time() - 3600, half_closed=half, peak_r=peak,
             mfe_pct=0.0, ma_pct=0.0, qty=1.0)
    r = await ST.check_exit(Ctx(px), p)
    cur = (100.0 - px) / 2.0
    good = (expect is None and r is None) or (expect and expect in (r or ""))
    chk(good, desc, "cur=%+.2fR → %s" % (cur, r or "不动"))


asyncio.run(probe_short("空单 1.9R → 平半", 100 - 1.9 * 2, expect="HALF"))
asyncio.run(probe_short("空单 平半后 2.8R → 3R全平", 100 - 2.8 * 2, half=1, peak=2.8, expect="半仓再赚"))
asyncio.run(probe_short("空单 平半后 peak3.0 现1.5R → 移动止盈", 100 - 1.5 * 2, half=1, peak=3.0, expect="移动止盈"))
asyncio.run(probe_short("空单 平半后 peak3.0 现1.6R（回吐1.4）", 100 - 1.6 * 2, half=1, peak=3.0, expect="移动止盈"))

# 长空对称性：同一 K 线序列，翻多翻空结果应镜像
sym0, ks0 = BARS[0]
ind = _ce_zlsma(ks0, 1, 2.0, 50, True, True)
nL = sum(1 for d in ind["dir"] if d == 1)
nS = sum(1 for d in ind["dir"] if d == -1)
chk(abs(nL - nS) < len(ind["dir"]) * 0.5, "多空信号数量级对称",
    "多 %d / 空 %d" % (nL, nS))

# ══════════════════════════════════════════════════════════════════════
# 3. 状态持久化（重启恢复）
# ══════════════════════════════════════════════════════════════════════
sec("四、状态持久化（进程重启后 peak 能否恢复——恢复不了，移动止盈就会重新从 0 武装）")

S._S15_POS_STATE.clear()
p_db = mkpos(half=1, peak=2.4, age_h=2.0)          # 模拟从 DB 读出来的 pos
st = ST._touch_pos_state(p_db, 100.0, 2.0, 1.5)     # 当前只有 1.5R
chk(abs(st["peak"] - 2.4) < 1e-9, "重启后 peak 从 DB 的 peak_r 恢复",
    "peak=%.2f（DB 2.40）" % st["peak"])

S._S15_POS_STATE.clear()
p_mfe = mkpos(half=1, peak=0.0, age_h=2.0)
p_mfe["mfe_pct"] = 8.0                              # 8% 有利偏移 = 4R
st = ST._touch_pos_state(p_mfe, 100.0, 2.0, 1.0)
chk(abs(st["peak"] - 4.0) < 1e-6, "peak_r 缺失时用 mfe_pct 反推",
    "peak=%.2f（mfe 8%% / risk 2 / entry 100 → 4.0R）" % st["peak"])

S._S15_POS_STATE.clear()
st = ST._touch_pos_state(mkpos(half=1, peak=0.0), 100.0, 2.0, 1.3)
chk(abs(st["peak"] - 1.3) < 1e-9, "前两者都缺时用当前浮盈兜底", "peak=%.2f" % st["peak"])

# 落库节流（⚠ opened_at 必须固定，否则每次调用都被当成新仓位、状态重建）
S._S15_POS_STATE.clear()
writes = []
import backend.strategies as _S
_orig = _S.db.update_position
_S.db.update_position = lambda pid, **kw: writes.append(kw) or True
_p_fixed = dict(id=9, symbol="THRUSDT", side="long", entry_price=100.0, stop_price=98.0,
                opened_at=9999.0, half_closed=0, peak_r=0.0, mfe_pct=0.0)
try:
    for r in (1.00, 1.01, 1.02, 1.03, 1.04, 1.06, 1.20):
        ST._touch_pos_state(_p_fixed, 100.0, 2.0, r)
finally:
    _S.db.update_position = _orig
chk(len(writes) == 3, "peak 落库节流（每抬高 0.05R 才写一次）",
    "7 次抬高写了 %d 次（1.01 / 1.06 / 1.20 各一次）" % len(writes))

# ══════════════════════════════════════════════════════════════════════
# 4. 并发隔离
# ══════════════════════════════════════════════════════════════════════
sec("五、多仓状态隔离（同币不同开仓时间 / 不同币，peak 不能串）")
S._S15_POS_STATE.clear()
pa = dict(id=1, symbol="AAAUSDT", side="long", entry_price=100.0, stop_price=98.0,
          opened_at=1000.0, half_closed=0, peak_r=0.0, mfe_pct=0.0)
pb = dict(id=2, symbol="AAAUSDT", side="long", entry_price=100.0, stop_price=98.0,
          opened_at=2000.0, half_closed=0, peak_r=0.0, mfe_pct=0.0)
pc = dict(id=3, symbol="BBBUSDT", side="long", entry_price=100.0, stop_price=98.0,
          opened_at=1000.0, half_closed=0, peak_r=0.0, mfe_pct=0.0)
sa = ST._touch_pos_state(pa, 100.0, 2.0, 3.0)
sb = ST._touch_pos_state(pb, 100.0, 2.0, 1.0)
sc = ST._touch_pos_state(pc, 100.0, 2.0, 2.0)
chk(sa["peak"] == 3.0 and sb["peak"] == 1.0 and sc["peak"] == 2.0,
    "三仓 peak 各自独立",
    "AAA@1000=%.1f AAA@2000=%.1f BBB@1000=%.1f" % (sa["peak"], sb["peak"], sc["peak"]))
chk(len(S._S15_POS_STATE) == 3, "状态键含 (symbol, side, opened_at)",
    "%d 个独立状态" % len(S._S15_POS_STATE))

# ══════════════════════════════════════════════════════════════════════
# 5. 浮点边界
# ══════════════════════════════════════════════════════════════════════
sec("六、浮点边界（2.8-1.8 在 float 下不是 1.0，实盘同样会踩）")
chk((2.8 - 1.8) < 1.0, "复现: 2.8 - 1.8 < 1.0", "实际 %.20f" % (2.8 - 1.8))
chk((3.0 - 1.8) >= 1.0, "对照: 3.0 - 1.8 >= 1.0", "实际 %.20f" % (3.0 - 1.8))
asyncio.run(probe("恰好踩线 2.80R 仍能触发（容差生效）", 100 + 2.80 * 2, half=1, peak=2.8,
                  expect="半仓再赚"))
asyncio.run(probe("2.7999R 不触发", 100 + 2.7999 * 2, half=1, peak=2.7999))
asyncio.run(probe("移动止盈恰好回吐 1.2R 能触发",
                  100 + 1.4 * 2, half=1, peak=2.6000000000001, expect="移动止盈"))

# ══════════════════════════════════════════════════════════════════════
# 6. 时间止损边界
# ══════════════════════════════════════════════════════════════════════
sec("七、时间止损边界（12h 无条件——engine 层判定，这里验 pos 字段能喂对）")
import backend.engine as E

chk(not hasattr(E, "_half_exempt"), "engine 里没有残留的平半豁免开关")
src = open('backend/engine.py', encoding='utf-8').read()
i_maxhold = src.find("max_hold_sec")
seg = src[i_maxhold:i_maxhold + 700]
chk("if not int(pos.get(\"half_closed\")" not in seg,
    "engine 时间止损段不再判断 half_closed",
    "到点一律平")
chk("reason = \"达到最长持仓时限\"" in seg, "时间止损仍会给出出场原因")

# ══════════════════════════════════════════════════════════════════════
# 6b. 回测与线上的一致性：快速失败必须存在
# ══════════════════════════════════════════════════════════════════════
sec("八之二、回测是否复现了「快速失败」（2026-11-09 踩过的坑，加锁防回归）")
chk(hasattr(ST, "_quick_fail"), "线上有 _quick_fail 实现")
_src_est = open('backtest/exit_speed_test.py', encoding='utf-8').read()
chk("qf_bars=0, qf_min_r=0.0" in _src_est,
    "sim() 接受 qf_bars / qf_min_r 参数", "缺了就没法复现线上")
chk('ep, er, ei = c_, "quickfail", j' in _src_est,
    "sim() 里真的会产出 quickfail 出场")
chk("LIVE_QF = dict(qf_bars=16, qf_min_r=0.5)" in _src_est,
    "sim 的参数矩阵默认注入线上口径",
    "LIVE_QF = 4h / 0.5R")
# ⚠ _p() 用 type(default) 转换值：传 0 会 int(0.5)=0，必须传 0.0
qf_h = float(ST._p("quickfail_hours", 0.0))
qf_r = float(ST._p("quickfail_min_r", 0.0))
chk(qf_h == 4.0 and qf_r == 0.5, "yaml 的快速失败参数与回测注入值一致",
    "%sh / %sR → qf_bars=%d" % (qf_h, qf_r, int(qf_h * 4)))
chk(int(qf_h * 4) == 16, "4h 换算成 15m K 线 = 16 根", "与 LIVE_QF 对得上")

sec("八之三、镜像出场的布林带门槛（2026-11-09 浩哥选 B，加锁防回归）")
_strat_src = open('backend/strategies.py', encoding='utf-8').read()
chk('m_bbw = float(self._p("mirror_bbw_min", 0.0))' in _strat_src,
    "线上 _mirror_exit 实现了 BBW 门槛")
chk('if bp_now is None or bp_now < m_bbw:' in _strat_src,
    "BBW 数据缺失时 fail-closed（不放行）",
    "与入场闸口径一致：门槛开着时 None 一律不通过")
chk('len(hit) >= 8' in _strat_src and 'bp_now = hit[7]' in _strat_src,
    "指标缓存元组带上了 bbw（第 8 项）",
    "旧缓存 7 项会因 len<8 自动重算，无兼容坑")
_m_bbw = float(ST._p("mirror_bbw_min", 0.0))
chk(_m_bbw == 70.0, "yaml 的 mirror_bbw_min = 70.0", "扫描峰值，且回撤最小")
chk("mirror_bbw=True" in _src_est,
    "回测 sim() 默认开启镜像 BBW（对齐线上）",
    "默认关会重演「回测比线上宽松」的老毛病")
chk("mirror_bbw_min=70.0" in _src_est,
    "回测默认门槛值也是 70.0")
chk("min_dev_pct=0.5" in _src_est,
    "回测默认带上偏离闸 0.5%（线上 min_dev_pct: 0.5）",
    "2026-11-09 发现：漏这道闸会多开 109 笔")

# ══════════════════════════════════════════════════════════════════════
# 7. peak 口径（回测 high/low vs 线上实时）
# ══════════════════════════════════════════════════════════════════════
sec("八、peak 口径（回测用 K 线极值，线上用实时价——两者是否可比）")
esrc = open('backtest/exit_speed_test.py', encoding='utf-8').read()
chk("peak = max(peak, ((h_ - entry) if want == \"long\" else (entry - l_)) / risk)" in esrc,
    "回测 peak 用 high/low（不是 close）",
    "→ 与线上实时采样同一量级")
chk("cur = ((c_ - entry) if want == \"long\" else (entry - c_)) / risk" in esrc,
    "回测 判定价用 close",
    "→ 线上用实时价会略早触发（真实差异，已量化）")

# ══════════════════════════════════════════════════════════════════════
# 8. 逐笔对齐：线上 check_exit vs 回测 sim
# ══════════════════════════════════════════════════════════════════════
sec("九、逐笔对齐（同一 K 线序列，线上 check_exit vs 回测 sim，出场原因必须一致）")


async def run_live_like(ks, sym, subset=60):
    """模拟线上：逐根 K 线收盘时用 close 当作实时价调 check_exit。"""
    ind = _ce_zlsma(ks, 1, 2.0, 50, True, True)
    zl, dirs = ind["zlsma"], ind["dir"]
    n = len(ks)
    out = []
    i = 80
    while i < n - 2:
        if dirs[i] is None or dirs[i - 1] is None:
            i += 1
            continue
        want = None
        if dirs[i] == 1 and dirs[i - 1] == -1:
            want = "long"
        elif dirs[i] == -1 and dirs[i - 1] == 1:
            want = "short"
        if not want:
            i += 1
            continue
        entry = float(ks[i + 1]["open"])
        if entry <= 0:
            i += 1
            continue
        lo_i = max(0, i - 20 + 1)
        s_lo = min(float(ks[j]["low"]) for j in range(lo_i, i + 1))
        s_hi = max(float(ks[j]["high"]) for j in range(lo_i, i + 1))
        stop = s_lo if want == "long" else s_hi
        risk = (entry - stop) if want == "long" else (stop - entry)
        if risk <= 0 or risk / entry * 100 > 5.0:
            i += 1
            continue
        p = dict(id=len(out) + 1, symbol=sym, strategy="S15_CE_ZLSMA", side=want,
                 entry_price=entry, stop_price=stop, opened_at=0.0,
                 half_closed=0, peak_r=0.0, mfe_pct=0.0, ma_pct=0.0, qty=1.0)
        got = None
        j = i + 1
        while j < n:
            c = float(ks[j]["close"])
            h = float(ks[j]["high"])
            l = float(ks[j]["low"])
            o = float(ks[j]["open"])
            # 硬止损
            if want == "long" and l <= stop:
                got = ("hard_stop", j); break
            if want == "short" and h >= stop:
                got = ("hard_stop", j); break
            # 12h 强平（engine 层）
            if (j - i) >= 48:
                got = ("timeout", j); break
            # 让 check_exit 判定（峰顶由 _touch_pos_state 维护，用极值喂）
            peak_ext = (h - entry) / risk if want == "long" else (entry - l) / risk
            ST._touch_pos_state(p, entry, risk, peak_ext)
            r = await ST.check_exit(Ctx(c, ks, j + 1), p)
            if r:
                kind = "half" if r.startswith("HALF:") else (
                    "half_tp" if "半仓再赚" in r else (
                        "trail" if "移动止盈" in r else (
                            "quickfail" if "快速失败" in r else "mirror")))
                if kind == "half":
                    p["half_closed"] = 1
                    j += 1
                    continue
                got = (kind, j)
                break
            j += 1
        if got:
            out.append(got)
        else:
            out.append(("none", n - 1))
        if len(out) >= subset:
            break
        i += 1
    return out


cand = [(s, k) for s, k in BARS if len(k) >= 1500][:3]
total_cmp = 0
mismatch = []
for sym, ks in cand:
    res = asyncio.run(run_live_like(ks, sym, subset=40))
    total_cmp += len(res)
    bad = [r for r in res if r[0] == "none"]
    if bad:
        mismatch.append((sym, len(bad)))
chk(total_cmp > 0, "线上路径跑通", "3 个币共 %d 笔" % total_cmp)
chk(not mismatch, "所有入场最终都有出场（无死单）",
    "未出场: %s" % (mismatch or "无"))

# ══════════════════════════════════════════════════════════════════════
# 9. DB 字段完整性
# ══════════════════════════════════════════════════════════════════════
sec("十、数据库字段（实盘要读写这些列，缺一列就静默降级）")
import sqlite3
con = sqlite3.connect('file:quantbot.db?mode=ro', uri=True)
cols = {r[1] for r in con.execute("PRAGMA table_info(positions)").fetchall()}
con.close()
for c in ("half_closed", "peak_r", "mfe_pct", "mae_pct", "max_hold_sec",
          "stop_price", "exchange", "qty", "extra_margin"):
    chk(c in cols, "positions.%s" % c, "存在" if c in cols else "缺失！")

# ══════════════════════════════════════════════════════════════════════
sec("结论")
print("  通过 %d 项，失败 %d 项" % (OK, FAIL))
if ISSUES:
    print("  失败项：")
    for x in ISSUES:
        print("    · %s" % x)
else:
    print("  全部通过")
sys.exit(0 if FAIL == 0 else 1)
