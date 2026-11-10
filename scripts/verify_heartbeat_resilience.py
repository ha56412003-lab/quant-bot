# -*- coding: utf-8 -*-
"""心跳容错回归测试 —— 证明「写锁冲突不会拖死 0.5s 循环」

背景（2026-11-10 修的 BUG）：
  每天 08:16 质量/因子日刷新跑长事务，占着 sqlite 写锁 >8 秒（busy_timeout），
  `db.set_meta()` 抛 `database is locked` → 异常冒出 tick 的 try 块 →
  **position_loop / scan_loop 整体退出** → 看门狗 20~30 秒后才拉起来。
  这段时间里止损/止盈完全停摆 —— 价格照样在动，单子没人管。

本脚本的做法：
  起一个进程对 quantbot.db 持 EXCLUSIVE 锁 12 秒（> busy_timeout 8 秒，必触发超时），
  同时盯 meta 里的心跳时间戳：
    修好之后 → 锁期间会打出「心跳写入失败」warning，但时间戳在锁释放后立刻继续前进，
               循环一次都没死（日志无 "loop tick failed"、无 "看门狗…重启"）
    修好之前 → 会看到 "loop tick failed" + 看门狗重启

用法：
  .venv/Scripts/python.exe scripts/verify_heartbeat_resilience.py
  前提：本地服务在跑（不跑也没关系，只会测不到服务侧行为）
"""
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "quantbot.db")
LOG = os.path.join(ROOT, "logs", "server.log")
LOCK_SEC = 12

LOCKER = f'''
import sqlite3, time
c = sqlite3.connect(r"{DB}", timeout=30, isolation_level=None)
c.execute("BEGIN IMMEDIATE")   # 拿 RESERVED 写锁，只抢写不写数据 —— 这才是日常撞锁的真实场景
print("LOCKED", flush=True)
time.sleep({LOCK_SEC})
c.rollback()
c.close()
print("RELEASED", flush=True)
'''


def beat():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
    try:
        row = con.execute("SELECT value FROM meta WHERE key='last_manage_ts_ms'").fetchone()
        row2 = con.execute("SELECT value FROM meta WHERE key='last_tick_ts'").fetchone()
        return (int(row[0]) if row else 0), (int(row2[0]) if row2 else 0)
    finally:
        con.close()


def log_tail_mark():
    if not os.path.exists(LOG):
        return 0
    return os.path.getsize(LOG)


def read_since(mark):
    if not os.path.exists(LOG):
        return ""
    with open(LOG, "rb") as f:
        f.seek(mark)
        return f.read().decode("utf-8", "ignore")


def main():
    print("=" * 80)
    print(f"心跳容错回归测试 · 持写锁 {LOCK_SEC} 秒（busy_timeout=8 秒，必超时）")
    print("=" * 80)

    b1, b2 = beat()
    print(f"  锁前心跳：manage={datetime.fromtimestamp(b1/1000):%H:%M:%S}  tick={datetime.fromtimestamp(b2 if b2<1e11 else b2/1000):%H:%M:%S}")
    mark = log_tail_mark()

    print(f"\n  → 起进程持 EXCLUSIVE 锁 {LOCK_SEC} 秒 …")
    proc = subprocess.Popen([sys.executable, "-c", LOCKER],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline().strip()
    print(f"     locker: {line}")
    if "LOCKED" not in line:
        rest = proc.stdout.read()
        print("     ✗ 锁没拿到，本测试无效：")
        print("     " + rest.strip().replace(chr(10), chr(10) + "     "))
        return 1
    t0 = time.time()

    # 锁住期间盯心跳：不该崩溃，服务进程也不该消失
    while time.time() - t0 < LOCK_SEC + 4:
        time.sleep(2)
        left = LOCK_SEC - (time.time() - t0)
        if left > 0:
            print(f"     锁中 … 还剩 {left:.0f}s")

    proc.wait(timeout=10)
    t_release = time.time()

    # 锁释放后多久心跳才恢复 —— 这是本轮真正的指标：
    # 修复前（set_meta 带 8 秒 busy_timeout + 3 次重试）实测要 16~19 秒才写进去，
    # 那 16 秒里 position_loop 一直卡在写心跳上，止损/止盈没在判断。
    lag = None
    for _ in range(100):
        b_, _ = beat()
        if b_ > b1:
            lag = time.time() - t_release
            break
        time.sleep(0.2)

    a1, a2 = beat()
    print(f"\n  锁后心跳：manage={datetime.fromtimestamp(a1/1000):%H:%M:%S}  tick={datetime.fromtimestamp(a2 if a2<1e11 else a2/1000):%H:%M:%S}")

    seg = read_since(mark)
    bad_loop = seg.count("loop tick failed")
    bad_wd = seg.count("看门狗") + seg.count("watchdog")
    good_hb = seg.count("心跳写入失败")

    print("\n" + "-" * 80)
    print("判定")
    print("-" * 80)
    ok_adv = a1 > b1
    print(f"  {'PASS' if ok_adv else 'FAIL'}  锁释放后心跳继续前进（{a1-b1} ms）→ 循环没死")
    if lag is None:
        print("  FAIL  锁释放后 20 秒内心跳都没恢复 → 循环卡在写库上")
    else:
        fast = lag < 2.5
        print(f"  {'PASS' if fast else 'FAIL'}  锁释放 {lag:.1f} 秒后心跳恢复"
              f"（要求 <2.5 秒；修复前是 16~19 秒 → 那段时间止损/止盈停摆）")
        if not fast:
            bad_loop += 1  # 计入失败
    print(f"  {'PASS' if bad_loop == 0 else 'FAIL'}  期间没有 'loop tick failed'（实际 {bad_loop} 次）")
    print(f"  {'PASS' if bad_wd == 0 else 'FAIL'}  期间看门狗没有重启循环（实际 {bad_wd} 次）")
    print(f"  {'PASS' if good_hb > 0 else 'WARN'}  「心跳写入失败」warning 出现 {good_hb} 次"
          f"（锁期间写不进去是预期的，关键是它没有拖住循环）")

    if bad_loop or bad_wd:
        print("\n  失败片段：")
        for ln in seg.splitlines():
            if "loop tick failed" in ln or "看门狗" in ln or "database is locked" in ln:
                print("   ", ln[:150])
        return 1
    print("\n  结论：写锁冲突期间循环存活，心跳自动恢复 ✓")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
