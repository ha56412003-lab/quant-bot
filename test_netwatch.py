"""netwatch 重试 / 告警 / 恢复 全链路测试（打桩，不真断网）。

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe test_netwatch.py
"""
import asyncio
import sys
import time

sys.path.insert(0, '.')

from backend import netwatch

PASS = FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✓ {name}')
    else:
        FAIL += 1
        print(f'  ✗ {name}  {extra}')


def reset():
    netwatch._state.update({
        "status": "ok", "fails": [], "last_push": {},
        "last_detail": "", "down_since": 0,
    })
    c = netwatch.conn()
    c.execute("DELETE FROM system_alerts")
    c.commit()
    # 关掉飞书推送，避免测试真发消息
    netwatch._feishu_targets = lambda: None


def alerts():
    c = netwatch.conn()
    return [dict(r) for r in c.execute(
        "SELECT kind, severity, message, detail, acked FROM system_alerts "
        "ORDER BY id")]


async def main():
    print('=' * 70)
    print('netwatch 全链路测试')
    print('=' * 70)

    # ── 1. 第一次失败，第二次成功 → 返回结果，不告警 ──
    print('\n【1】重试机制')
    reset()
    calls = {'n': 0}

    async def flaky():
        calls['n'] += 1
        if calls['n'] < 2:
            raise ConnectionError("模拟断网第 1 次")
        return {"ok": True}

    r = await netwatch.with_retry(flaky, "GET /test")
    check('第 1 次失败、第 2 次成功 → 返回数据', r == {"ok": True})
    check('确实只调了 2 次（不是 3 次）', calls['n'] == 2, f"实际 {calls['n']}")
    check('未判定瘫痪', netwatch.status()['status'] == 'ok')
    check('未写任何告警', len(alerts()) == 0)

    # ── 2. 三次全失败 → 抛出 + 记一次故障 ──
    print('\n【2】三次全失败')
    reset()
    calls2 = {'n': 0}

    async def always_fail():
        calls2['n'] += 1
        raise TimeoutError("模拟超时")

    raised = None
    try:
        await netwatch.with_retry(always_fail, "GET /test2")
    except Exception as exc:
        raised = exc
    check('异常原样抛出（不吞）', isinstance(raised, TimeoutError))
    check('确实试了 3 次', calls2['n'] == 3, f"实际 {calls2['n']}")
    check('记了 1 次故障', netwatch.status()['recent_fails'] == 1)
    check('1 次还不够阈值，未告警', len(alerts()) == 0)
    check('状态仍是 ok', netwatch.status()['status'] == 'ok')

    # ── 3. 60 秒内累计 3 次故障 → 判定瘫痪 ──
    print('\n【3】达标判定瘫痪')
    netwatch.report_failure("GET /t3", TimeoutError("第2次"))
    check('2 次仍未瘫痪', netwatch.status()['status'] == 'ok')
    netwatch.report_failure("GET /t3", TimeoutError("第3次"))
    st = netwatch.status()
    check('3 次 → 判定瘫痪', st['status'] == 'down')
    a = alerts()
    check('写入了 1 条 critical 告警', len(a) == 1 and a[0]['severity'] == 'critical')
    check('告警 kind = net_down', a and a[0]['kind'] == 'net_down')
    check('告警里带失败原因', '第3次' in (a[0]['detail'] or ''))
    check('未读（acked=0）', a and a[0]['acked'] == 0)

    # ── 4. 继续失败不重复刷库 ──
    print('\n【4】防刷屏')
    before = len(alerts())
    for _ in range(10):
        netwatch.report_failure("GET /t4", TimeoutError("继续失败"))
    check('再失败 10 次，不重复写库', len(alerts()) == before, f"实际 {len(alerts())}")

    # ── 5. 恢复 ──
    print('\n【5】恢复通知')
    netwatch.report_success()
    st = netwatch.status()
    check('状态回到 ok', st['status'] == 'ok')
    check('失败计数清零', st['recent_fails'] == 0)
    a = alerts()
    check('写入恢复通知', len(a) == 2 and a[1]['kind'] == 'net_recovered')
    check('恢复是 info 级', a[1]['severity'] == 'info')
    check('恢复消息含中断时长', '中断时长' in (a[1]['message'] or '') or
          '中断' in (a[1]['detail'] or ''))

    # ── 6. 恢复后再失败，能重新告警 ──
    print('\n【6】恢复后再次瘫痪')
    netwatch._state['last_push'] = {}   # 跳过冷却，模拟 5 分钟后
    for _ in range(3):
        netwatch.report_failure("GET /t6", TimeoutError("又断了"))
    check('能再次判定瘫痪', netwatch.status()['status'] == 'down')
    check('又写了 1 条 net_down', len([x for x in alerts() if x['kind'] == 'net_down']) == 2)

    # ── 7. 窗口过期：60 秒前的失败不算 ──
    print('\n【7】时间窗口')
    reset()
    netwatch._state['fails'] = [time.time() - 120, time.time() - 100]
    netwatch.report_failure("GET /t7", TimeoutError("窗口测试"))
    check('120 秒前的失败被丢弃，只剩本次 1 次',
          netwatch.status()['recent_fails'] == 1,
          f"实际 {netwatch.status()['recent_fails']}")
    check('未误判瘫痪', netwatch.status()['status'] == 'ok')

    # ── 8. 写操作不重试（binance._signed POST 口径）──
    print('\n【8】写操作不重试（防重复下单）')
    reset()
    import inspect
    src = inspect.getsource(__import__('backend.binance', fromlist=['_signed'])._signed)
    check('POST 分支不走 with_retry', 'if method.upper() == "GET"' in src)
    check('写操作分支有 report_failure', '写操作，未重试' in src)
    check('写操作分支没有 with_retry 包', src.count('with_retry') == 1)

    # 清理测试数据
    reset()

    print()
    print('=' * 70)
    print(f'结果：通过 {PASS} 项，失败 {FAIL} 项')
    print('=' * 70)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
