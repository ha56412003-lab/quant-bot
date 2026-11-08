"""测连接池自愈（方案 A）。

要验的四件事：
  ① 判定失败到阈值后，会发出重建标记
  ② 标记是一次性的（take 完就清），不会重复重建
  ③ 冷却期内不重复发标记（防止疯狂重建）
  ④ 代理恢复后，重建能真的让请求成功（真实链路，不打桩）

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe test_reconnect_selfheal.py
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:7897")
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:7897")
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")

from backend import binance, netwatch  # noqa: E402

# ★★★ 必须打桩 ★★★
# report_failure() 到阈值后会做两件有副作用的事：
#   ① _write_alert()  —— 往真实 quantbot.db 的 system_alerts 表插一行
#   ② _push_async()   —— 往真实飞书群推消息
# 不桩的话，每跑一次这个测试，前端就会弹一次「交易系统网络中断」红色告警，
# 而且写的是生产库（2026-11-09 实际踩过：跑了测试→浩哥屏幕弹窗→库里 3 条假告警）。
_written = []
netwatch._write_alert = lambda *a, **k: (_written.append(a), True)[1]
netwatch._push_async = lambda *a, **k: None

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def reset():
    """把 netwatch 状态恢复干净。"""
    netwatch._state["status"] = "ok"
    netwatch._state["fails"] = []
    netwatch._state["reconnect"] = False
    netwatch._state["last_reconnect"] = 0.0


print("=" * 76)
print("① 初始状态：不该有重建标记")
print("=" * 76)
reset()
check("初始 reconnect 为 False", netwatch.take_reconnect() is False)

print()
print("=" * 76)
print("② 失败不到阈值（3 次）：不该发标记")
print("=" * 76)
reset()
for i in range(2):
    netwatch.report_failure(f"GET /test{i}", RuntimeError("boom"))
check("2 次失败后无标记", netwatch.take_reconnect() is False,
      f"（fails={len(netwatch._state['fails'])}）")

print()
print("=" * 76)
print("③ 到阈值（3 次）：应该发标记，且只发一次")
print("=" * 76)
reset()
for i in range(3):
    netwatch.report_failure(f"GET /test{i}", RuntimeError("boom"))
check("3 次失败后 status=down", netwatch._state["status"] == "down")
check("3 次失败后有标记", netwatch._state["reconnect"] is True)
check("第一次 take 返回 True", netwatch.take_reconnect() is True)
check("第二次 take 返回 False（一次性）", netwatch.take_reconnect() is False)

print()
print("=" * 76)
print("④ 已经是 down 状态时继续失败：仍要能发标记（这是之前卡死的根因）")
print("=" * 76)
reset()
for i in range(3):
    netwatch.report_failure(f"GET /t{i}", RuntimeError("boom"))
netwatch.take_reconnect()                       # 消耗掉第一次
netwatch._state["last_reconnect"] = 0.0         # 清掉冷却，模拟 10 秒已过
netwatch.report_failure("GET /t9", RuntimeError("still down"))
check("down 状态下继续失败仍发标记", netwatch._state["reconnect"] is True)

print()
print("=" * 76)
print("⑤ 冷却：10 秒内不重复标记")
print("=" * 76)
reset()
netwatch._state["fails"] = [time.time()] * 3    # 伪造已经在窗口内失败 3 次
netwatch._state["last_reconnect"] = time.time()  # 刚重建过
netwatch.report_failure("GET /t", RuntimeError("boom"))
check("冷却期内不标记", netwatch._state["reconnect"] is False)
netwatch._state["last_reconnect"] = time.time() - 11   # 11 秒前
netwatch.report_failure("GET /t", RuntimeError("boom"))
check("冷却期满后能标记", netwatch._state["reconnect"] is True)

print()
print("=" * 76)
print("⑥ 真实链路：重建后请求能成功")
print("=" * 76)


async def real_path():
    reset()
    # 先正常拉一次，把连接池跑热
    rows = await binance.mark_price("BTCUSDT")
    check("预热请求成功", bool(rows))
    c1 = binance._client
    check("连接池已建立", c1 is not None and not c1.is_closed)

    # 手动触发重建
    await binance._reconnect()
    check("重建后 _client 被置空", binance._client is None)
    check("旧 client 已关闭", c1 is not None and c1.is_closed)

    # 再拉一次，应自动新建并成功
    rows2 = await binance.mark_price("BTCUSDT")
    check("重建后请求成功", bool(rows2))
    check("已建立新连接池", binance._client is not None and binance._client is not c1)

    # 走完整 netwatch 信号链路：标记 → _get 接住
    reset()
    netwatch._state["reconnect"] = True
    rows3 = await binance.mark_price("BTCUSDT")
    check("netwatch 标记被 _get 接住并重建", bool(rows3))
    check("标记已被消费", netwatch._state["reconnect"] is False)


asyncio.run(real_path())

print()
print("=" * 76)
print("⑦ 污染隔离：告警写入被桩拦住，一行都没落进生产库")
print("=" * 76)
check("确实触发了告警写入（逻辑正常）", len(_written) >= 1, f"（{len(_written)} 次）")
check("全部落在桩上，未写真实库", netwatch._write_alert.__module__ != "backend.netwatch"
      if hasattr(netwatch._write_alert, "__module__") else True)

print()
print("=" * 76)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 76)
sys.exit(0 if FAIL == 0 else 1)
