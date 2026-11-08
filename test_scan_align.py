"""测扫描对齐 K 线边界（engine._due）。

要验的六件事：
  ① 对齐后触发点落在「K 线边界 + 10 秒」（如 19:00:10、19:15:10）
  ② 同一根 K 线只扫一次，不会重复
  ③ 启动后立即扫一次（不能等 15 分钟才有第一个信号）
  ④ 老行为对比：确认以前确实晚 8 分多钟
  ⑤ scan_interval 比 grace 还短时，不会每 tick 都扫（grace 保护）
  ⑥ 服务卡住很久恢复后，只补扫一次，不会连扫一串

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe test_scan_align.py
"""
import datetime
import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from backend import engine  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def ts(h, m, s=0):
    """今天某个时刻的 unix 时间戳。"""
    return datetime.datetime(2026, 11, 8, h, m, s).timestamp()


def fmt(t):
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")


def run_clock(times, sid="S15_CE_ZLSMA", interval="15m", reset=True,
              warm=True):
    """把时钟依次拨到 times 里的每个时刻，返回「判定该扫」的那些时刻。

    warm=True 时先假装「服务已经跑过一轮」（_last_scan 有个近期值），
    这样观察窗口里只有「K 线边界」会触发，不会混进启动那一次。
    """
    if reset:
        engine._last_scan.clear()
        if warm and times:
            # 把上次扫描时刻设在观察窗口之前的一个对齐点上
            iv = engine._interval_sec(interval)
            grace = min(engine.ALIGN_GRACE, max(1, iv // 4))
            base = int(times[0] // iv) * iv
            # 假装「本轮的触发点刚刚扫过」，观察窗口内就只剩边界的触发
            engine._last_scan[sid] = base + grace
    fired = []
    for t in times:
        with patch.object(engine.time, "time", return_value=t):
            if engine._due(sid, interval):
                fired.append(t)
    return fired


def ticks(start, end, step=0.5):
    """生成 start~end 之间每 step 秒一个时刻。"""
    out = []
    t = start
    while t <= end:
        out.append(t)
        t += step
    return out


print("=" * 78)
print("① 对齐：19:00 这根收盘后，应该在 19:00:10 扫")
print("=" * 78)
fired = run_clock(ticks(ts(18, 58, 0), ts(19, 2, 0)))
print("  实际触发时刻:", [fmt(t) for t in fired])
check("触发了一次", len(fired) == 1, f"（{len(fired)} 次）")
check("触发时刻是 19:00:10 附近",
      bool(fired) and abs(fired[0] - ts(19, 0, 10)) < 1.0,
      f"实际 {fmt(fired[0]) if fired else '-'}")

print()
print("=" * 78)
print("② 同一根 K 线只扫一次（跨 19:00 到 19:14 全时段）")
print("=" * 78)
engine._last_scan.clear()
engine._last_scan["S15_CE_ZLSMA"] = ts(18, 45, 10)   # 上一根刚扫过
fired = run_clock(ticks(ts(19, 0, 0), ts(19, 14, 59)),
                  reset=False, warm=False)
check("整个 15 分钟只触发 1 次", len(fired) == 1, f"（{len(fired)} 次）")
if fired:
    check("触发在 19:00:10", abs(fired[0] - ts(19, 0, 10)) < 1.0, fmt(fired[0]))

print()
print("=" * 78)
print("③ 连续三根 K 线：19:00 / 19:15 / 19:30 各扫一次")
print("=" * 78)
fired = run_clock(ticks(ts(18, 59, 0), ts(19, 31, 0)))
print("  实际触发时刻:", [fmt(t) for t in fired])
check("正好 3 次", len(fired) == 3, f"（{len(fired)} 次）")
if len(fired) == 3:
    check("第一次 19:00:10", abs(fired[0] - ts(19, 0, 10)) < 1.0, fmt(fired[0]))
    check("第二次 19:15:10", abs(fired[1] - ts(19, 15, 10)) < 1.0, fmt(fired[1]))
    check("第三次 19:30:10", abs(fired[2] - ts(19, 30, 10)) < 1.0, fmt(fired[2]))

print()
print("=" * 78)
print("④ 启动后立即扫一次（服务 18:08:25 启动不能干等 15 分钟）")
print("=" * 78)
fired = run_clock([ts(18, 8, 25)], warm=False)
check("启动第一个 tick 就扫", len(fired) == 1, f"（{len(fired)} 次）")

print()
print("=" * 78)
print("⑤ grace 保护：scan_interval=5s 时不会每 tick 都扫")
print("=" * 78)
fired = run_clock(ticks(ts(19, 0, 0), ts(19, 1, 0), step=0.5),
                  sid="FAST", interval="5s")
print(f"  60 秒内触发 {len(fired)} 次")
check("60 秒内不超过 15 次（5s 周期最多 12 次 + 余量）",
      len(fired) <= 15, f"（{len(fired)} 次）")

print()
print("=" * 78)
print("⑥ 服务卡住 40 分钟后恢复：只补扫一次，不连扫一串")
print("=" * 78)
engine._last_scan.clear()
with patch.object(engine.time, "time", return_value=ts(18, 45, 10)):
    engine._due("S15_CE_ZLSMA", "15m")           # 先正常扫一次
fired = run_clock(ticks(ts(19, 25, 0), ts(19, 25, 3), step=0.5), reset=False,
                  warm=False)
print("  恢复后 3 秒内触发:", [fmt(t) for t in fired])
check("只触发 1 次", len(fired) == 1, f"（{len(fired)} 次）")

print()
print("=" * 78)
print("⑦ 老行为对比：以前会晚多久")
print("=" * 78)


def old_due(sid, scan_interval, state, now):
    """旧实现（启动后固定 N 秒），用来对比。"""
    if now - state.get(sid, 0) >= engine._interval_sec(scan_interval) - 0.25:
        state[sid] = now
        return True
    return False


old_state = {}
old_fired = []
for t in ticks(ts(18, 8, 25), ts(19, 30, 0)):
    if old_due("S15_CE_ZLSMA", "15m", old_state, t):
        old_fired.append(t)

print("  旧行为触发时刻:", [fmt(t) for t in old_fired])
new_fired = run_clock(ticks(ts(18, 8, 25), ts(19, 30, 0)))
print("  新行为触发时刻:", [fmt(t) for t in new_fired])

# 找两边「第一次扫到 18:45 这根（即 19:00 之后）」的时刻
old_first = next((t for t in old_fired if t >= ts(19, 0, 0)), None)
new_first = next((t for t in new_fired if t >= ts(19, 0, 0)), None)
if old_first and new_first:
    gap = old_first - new_first
    print(f"  18:45 这根 K 线：旧 {fmt(old_first)} 才扫 / 新 {fmt(new_first)} 扫")
    check("新版提前了（>5 分钟）", gap > 300, f"（提前 {gap / 60:.1f} 分钟）")

print()
print("=" * 78)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 78)
sys.exit(0 if FAIL == 0 else 1)
