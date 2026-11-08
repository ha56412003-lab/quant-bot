"""监控池新闸门单测（2026-11-08 重构）。

覆盖：
  ① 止损距离算法（与 strategies.py 同口径）
  ② EFF 算法（与 scripts/pool_gate_analysis.py 同口径）
  ③ 平滑（中位数、忽略 None、窗口裁剪）
  ④ 滞回进出池（进线/出线/中间地带/保护期/容量）
  ⑤ 增量 K 线合并（去重、同 ts 覆盖、裁剪）

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe test_quality_gate.py
"""
import sys
import time

sys.path.insert(0, ".")
from backend import quality as _q  # noqa: E402  占位，确保包能导入
import importlib  # noqa: E402

q = importlib.import_module("backend.quality")


PASS = FAIL = 0


def check(name, got, want, tol=None):
    global PASS, FAIL
    if tol is not None:
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if ok:
        PASS += 1
        print(f"  ✅ {name}: {got}")
    else:
        FAIL += 1
        print(f"  ❌ {name}: got={got} want={want}")


def mk(n, base_low=90.0, base_close=95.0, step=0.5):
    """造 n 根 K 线：low / high / close 同步上移。"""
    return [{"ts": 1000 + i * 900,
             "high": base_close + 5 + i * step,
             "low": base_low + i * step,
             "close": base_close + i * step} for i in range(n)]


print("=" * 78)
print("① 止损距离 _stop_dist")
print("=" * 78)

# 30 根，最后 20 根：low 从 90+10*0.5=95.0 到 90+29*0.5=104.5
# close 最后 = 95+29*0.5 = 109.5
# high 最后 20 根：从 100+10*0.5=105.0 到 100+29*0.5=114.5
bars = mk(30)
# 多头：(109.5 - 95.0)/109.5 = 13.24%
# 空头：(114.5 - 109.5)/109.5 = 4.57%
# max = 13.24%
check("多头止损距离", q._stop_dist(bars), 13.242, tol=0.01)

# K 线不够 21 根 → None
check("K线不足返回None", q._stop_dist(mk(15)), None)

# 现价恰在最低点 → 多头 0%，空头最大
flat = [{"ts": i, "high": 110.0, "low": 100.0, "close": 100.0} for i in range(30)]
check("现价在低点时取空头方向", q._stop_dist(flat), 10.0, tol=0.01)

print()
print("=" * 78)
print("② 趋势效率 _eff")
print("=" * 78)

# 纯单边上涨：close 100→199，high/low 同步 → 净位移 99%，振幅也是 99%
up = [{"ts": i, "high": 101 + i, "low": 100 + i, "close": 100 + i} for i in range(96)]
# p0=100, p1=195, hi=196, lo=100
# amp = (196-100)/100 = 0.96
# net = |195/100 - 1| = 0.95
# eff = 0.95/0.96 = 0.9896
check("单边上涨 EFF≈0.99", q._eff(up), 0.9896, tol=0.001)

# 纯震荡：close 在 100/110 来回，high/low 覆盖 → net≈0
zig = []
for i in range(96):
    c = 100.0 if i % 2 == 0 else 110.0
    zig.append({"ts": i, "high": max(c, 110.0), "low": min(c, 100.0), "close": c})
# p0=100, p1=110 (i=95 奇数), net=0.1, hi=110, lo=100, amp=0.1
# eff = 0.1/0.1 = 1.0 ← 两点的净位移正好等于振幅
# 换个构造：96 根里涨了又跌回原点
zig2 = []
for i in range(96):
    x = 100 + 10 * (1 - abs((i % 20) - 10) / 10.0)   # 锯齿，每 20 根回原点
    zig2.append({"ts": i, "high": x + 0.5, "low": x - 0.5, "close": x})
# p0: i=0 → 100+10*(1-1)=100 ; p1: i=95 → (95%20=15) → 1-|15-10|/10=0.5 → 105
# 振幅：min low=99.5, max high=110.5, amp=(110.5-99.5)/99.5=0.1106
# net=|105/100-1|=0.05 → eff=0.05/0.1106=0.452
check("锯齿 EFF≈0.452", q._eff(zig2), 0.452, tol=0.005)

check("K线不足返回None", q._eff(mk(50)), None)

print()
print("=" * 78)
print("③ 平滑 _median / _push_hist")
print("=" * 78)

check("中位数-奇数", q._median([1, 5, 3]), 3)
check("中位数-偶数", q._median([1, 2, 3, 4]), 2.5)
check("中位数忽略None", q._median([None, 1, 3, None]), 2.0)
check("中位数全None", q._median([None, None]), None)
check("中位数空集", q._median([]), None)

# _push_hist 容量：SMOOTH_SEC / TICK_SEC
q.SMOOTH_SEC, q.TICK_SEC = 300, 60
q._hist.clear()
for i in range(20):
    q._push_hist("TEST", float(i), float(i) / 10)
h = q._hist["TEST"]
check("队列裁剪到5条", len(h["stop"]), 5)
check("保留最新5个 stop", h["stop"], [15.0, 16.0, 17.0, 18.0, 19.0])

print()
print("=" * 78)
print("④ 滞回进出池 recompute")
print("=" * 78)

# 准备假 universe + 打桩 _judge
q._monitor.clear()
q._in_since.clear()
q._hist.clear()
q._rows.clear()
q.MONITOR_SIZE = 40
q.MIN_DWELL_SEC = 900
q.STOP_MIN_PCT, q.STOP_EXIT_PCT = 1.00, 0.85
q.EFF_MIN, q.EFF_EXIT = 0.30, 0.25
q.MIN_QUOTE_VOL, q.MAX_SPIKE = 5e6, 8.0

_q_judge = q._judge
q._judge = lambda s: (True, "")     # 所有币成交额/插针都过

U = ["A", "B", "C", "D", "E"]
for s in U:
    q._rows[s] = {"qv": 1e8}

def set_val(sym, stop, eff, n=5):
    q._hist[sym] = {"stop": [stop] * n, "eff": [eff] * n, "ts": [time.time()] * n}

# A: 稳定达标 → 应进池
set_val("A", 1.5, 0.5)
# B: 止损刚好 1.00 → 应进池（>=）
set_val("B", 1.00, 0.30)
# C: 止损 0.99 → 不进池
set_val("C", 0.99, 0.50)
# D: EFF 0.29 → 不进池
set_val("D", 2.0, 0.29)
# E: 止损 0.87（在滞回带内 0.85~1.00）→ 不进池（因为未在池）
set_val("E", 0.87, 0.50)

pool = q.recompute(U)
check("进池数量", len(pool), 2)
check("A 进池", "A" in pool, True)
check("B 边界1.00进池", "B" in pool, True)
check("C 止损0.99不进", "C" in pool, False)
check("D EFF0.29不进", "D" in pool, False)
check("E 滞回带内不进", "E" in pool, False)

# 现在 A 已进池。把 A 的止损降到 0.84（低于出池线）但保护期内（刚进）→ 仍留
set_val("A", 0.84, 0.50)
pool = q.recompute(U)
check("保护期内不被踢", "A" in pool, True)

# 把 A 的进池时间往前拨 20 分钟，过保护期
q._in_since["A"] = time.time() - 1200
pool = q.recompute(U)
check("过保护期+止损0.84 → 踢出", "A" in pool, False)

# A 重新达标 → 能再进
set_val("A", 1.5, 0.5)
pool = q.recompute(U)
check("重新达标再进池", "A" in pool, True)

# 中间地带：A 现在在池内，止损 0.90（0.85~1.00）→ 不动，留在池
q._in_since["A"] = time.time() - 1200
set_val("A", 0.90, 0.40)
pool = q.recompute(U)
check("中间地带不踢", "A" in pool, True)

# EFF 中间地带 0.27 → 也不踢
set_val("A", 1.2, 0.27)
pool = q.recompute(U)
check("EFF中间地带不踢", "A" in pool, True)

# EFF 掉到 0.24 → 踢
set_val("A", 1.2, 0.24)
pool = q.recompute(U)
check("EFF跌破0.25踢出", "A" in pool, False)

print()
print("=" * 78)
print("⑤ 容量上限")
print("=" * 78)
q._monitor.clear(); q._in_since.clear(); q._hist.clear()
q.MONITOR_SIZE = 3
big = [f"S{i:02d}" for i in range(10)]
for s in big:
    q._rows[s] = {"qv": 1e8}
    set_val(s, 2.0, 0.5)
pool = q.recompute(big)
check("容量截断到3", len(pool), 3)

print()
print("=" * 78)
print("⑥ 增量 K 线合并")
print("=" * 78)
q._kl.clear()
q.KL_NEED = 96
b1 = [{"ts": i, "high": 1.0, "low": 0.5, "close": 0.8} for i in range(100)]
q._merge_kl("X", b1)
check("首次合并后长度", len(q._kl["X"]), 100)
# 再来 2 根新的（ts 100, 101）
b2 = [{"ts": 100, "high": 2.0, "low": 1.0, "close": 1.5},
      {"ts": 101, "high": 2.2, "low": 1.2, "close": 1.8}]
q._merge_kl("X", b2)
check("增量后长度", len(q._kl["X"]), 102)
check("新根已加入", q._kl["X"][-1]["ts"], 101)
# 同 ts 覆盖（未收盘那根变成收盘版）
b3 = [{"ts": 101, "high": 9.9, "low": 9.0, "close": 9.5}]
q._merge_kl("X", b3)
check("同ts覆盖不增长度", len(q._kl["X"]), 102)
check("同ts覆盖了内容", q._kl["X"][-1]["close"], 9.5)
# 裁剪：超过 KL_NEED*2=192 才裁
b4 = [{"ts": 1000 + i, "high": 1, "low": 0.5, "close": 0.8} for i in range(150)]
q._merge_kl("X", b4)
check("裁剪到192", len(q._kl["X"]), 192)

print()
print("=" * 78)
q._judge = _q_judge   # 还原
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 78)
sys.exit(0 if FAIL == 0 else 1)
