"""镜像出场的「K 线还没追上当前窗口」BUG 回归测试。

背景（2026-11-08 TAUSDT 实证）：
  20:30 那根三闸全过，本该立即出场，系统拖到 21:00 才平，多亏 0.38R。
  根因：20:45:00 窗口一一翻，check_exit 立刻拉 K 线，但币安那一刻还没
  生成 20:45 那根 → len(ks)-2 指向 20:15 而不是 20:30 →
  结果按 cur_bar 缓存整窗 15 分钟 → 20:30 的信号被完整跳过。

修法：拉回来的数据最后一根若还没到 cur_bar，直接放弃本轮、不写缓存。

本测试守住的 5 条：
  ① 数据没追到当前窗口 → 不出场，且 不写 _S15_ZL_CACHE
  ② 数据没追到当前窗口 → 不污染 _S15_MIRROR_STATE 的 bar_ts
  ③ 同一窗口内数据追上来之后 → 正常判定（不再被前一次失败卡住）
  ④ 数据正常（已含当前窗口那根）→ 判定的是「最后已收盘那根」，不是上上一根
  ⑤ 整根 K 线只判一次的行为保持不变（重复调用不重复触发）

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe test_mirror_stale_bar.py
"""
import asyncio
import math
import sys
from unittest.mock import patch

sys.path.insert(0, r"E:\quant-bot")

from backend.strategies import (  # noqa: E402
    S15CeZlsma, _S15_ZL_CACHE, _S15_MIRROR_STATE,
)

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}   {detail}")


def make_ks(n, start_ts, step=900):
    """造一段平滑下跌后拉升的 K 线，保证最后几根 CE 会由空翻多。"""
    ks = []
    px = 1.0
    for i in range(n):
        # 前 92% 缓慢下跌，最后 8% 快速拉升 → CE 由空翻多发生在尾部，
        # 这样「三闸全过」的那根下标能落在 180 以后（满足 need=180 的下限）
        if i < n * 0.92:
            px *= 0.9985
        else:
            px *= 1.012
        o = px * 0.999
        h = px * 1.004
        l = px * 0.996
        ks.append({"ts": start_ts + i * step, "open": o, "high": h,
                   "low": l, "close": px})
    return ks


class FakeCtx:
    def __init__(self, ks, px):
        self._ks = ks
        self._px = px

    async def klines(self, symbol, interval="15m", limit=100):
        return self._ks

    def price(self, symbol):
        return self._px


def make_pos(**kw):
    p = dict(id=9001, symbol="TESTUSDT", side="short",
             entry_price=1.0, stop_price=1.25, half_closed=0, strategy="S15_CE_ZLSMA")
    p.update(kw)
    return p


BAR = 900
# 当前时间：20:45:00 之后 0.3 秒 —— 正是币安可能还没生成 20:45 那根的窗口
NOW_STALE = 20 * 3600 + 45 * 60 + 0.3
CUR_BAR = int(NOW_STALE // BAR) * BAR


async def main():
    print("=" * 90)
    print("镜像出场 · K 线未追上当前窗口 的回归测试")
    print("=" * 90)
    print()

    strat = S15CeZlsma({})

    # ── 场景 ① ②：数据最后一根还停在上一根（没有当前窗口那根）──────
    print("① 数据没追到当前窗口 —— 不能算，也不能缓存")
    _S15_ZL_CACHE.clear()
    _S15_MIRROR_STATE.clear()

    ks_stale = make_ks(200, CUR_BAR - 200 * BAR)   # 最后一根 = CUR_BAR - 900
    assert int(ks_stale[-1]["ts"]) == CUR_BAR - BAR, "构造有误"

    pos = make_pos()
    ctx = FakeCtx(ks_stale, px=1.0)
    with patch("backend.strategies.time.time", return_value=NOW_STALE):
        r = await strat.check_exit(ctx, pos)

    check("不出场（返回 None）", r is None, f"实际 {r!r}")
    check("没有写 _S15_ZL_CACHE", ("TESTUSDT", "15m") not in _S15_ZL_CACHE,
          f"缓存内容 {_S15_ZL_CACHE}")
    st = _S15_MIRROR_STATE.get(9001)
    check("没有污染 bar_ts（仍是 None）",
          st is None or st.get("bar_ts") is None,
          f"实际 {st}")
    print()

    # ── 场景 ③：同一窗口内数据追上来了 ──────────────────────
    print("② 同一窗口内数据追上来 —— 必须能正常判定（不能被上一次失败卡死）")
    ks_ok = make_ks(201, CUR_BAR - 200 * BAR)      # 最后一根 = CUR_BAR
    assert int(ks_ok[-1]["ts"]) == CUR_BAR, "构造有误"

    ctx2 = FakeCtx(ks_ok, px=float(ks_ok[-1]["close"]))
    with patch("backend.strategies.time.time", return_value=NOW_STALE + 2):
        r2 = await strat.check_exit(ctx2, pos)

    check("缓存被写入了", ("TESTUSDT", "15m") in _S15_ZL_CACHE,
          f"缓存键 {list(_S15_ZL_CACHE)}")
    st2 = _S15_MIRROR_STATE.get(9001)
    check("bar_ts 被设为「最后已收盘那根」",
          st2 is not None and st2.get("bar_ts") == ks_ok[-2]["ts"],
          f"实际 {st2}  期望 ts={ks_ok[-2]['ts']}（CUR_BAR={CUR_BAR}）")
    print()

    # ── 场景 ④：判定用的是「最后已收盘那根」，不是上上一根 ──────────
    print("③ 判定索引正确 —— i 指向最后已收盘那根（len-2），不是 len-3")
    from backend.strategies import _ce_zlsma
    ind = _ce_zlsma(ks_ok, 1, 2.0, 50, True, True)
    i_expect = len(ks_ok) - 2
    ts_expect = ks_ok[i_expect]["ts"]
    got_ts = st2.get("bar_ts") if st2 else None
    check("缓存里的 bar_ts == ks[len-2].ts", got_ts == ts_expect,
          f"实际 {got_ts}  期望 {ts_expect}")
    check("ZLSMA 有值（指标算出来了）", ind["zlsma"][i_expect] is not None)
    print()

    # ── 场景 ⑤：同一根只判一次 ───────────────────────────
    print("④ 同一根 K 线只判一次（防 0.5 秒一轮反复触发）")
    with patch("backend.strategies.time.time", return_value=NOW_STALE + 3):
        r3 = await strat.check_exit(ctx2, pos)
    check("同一根第二次调用返回 None", r3 is None, f"实际 {r3!r}")
    print()

    # ── 场景 ⑥：真·三闸全过时能出场（回归老功能没坏）────────────
    print("⑤ 三闸真全过时必须出场（确认修复没把正常出场也挡掉）")
    _S15_ZL_CACHE.clear()
    _S15_MIRROR_STATE.clear()

    # 顺着数据找到一根「CE 由空翻多 + HA阳 + 站上ZLSMA」的已收盘 K 线
    ind2 = _ce_zlsma(ks_ok, 1, 2.0, 50, True, True)
    # need = max(ce_len, z_len*2, 100) + 80 = 180，所以判定的那根下标必须 >= 179
    # （ks[:k+2] 长度 k+2 必须 >= 180），否则 check_exit 会因为「数据不够」直接返回
    hit_i = None
    for k in range(len(ks_ok) - 2, 178, -1):
        z, hn, hp = ind2["zlsma"][k], ind2["close"][k], ind2["open"][k]
        dn, dp = ind2["dir"][k], ind2["dir"][k - 1]
        if z is None or dn is None or dp is None:
            continue
        if dp == -1 and dn == 1 and hn > hp and hn > z:
            hit_i = k
            break

    if hit_i is None:
        check("合成数据里出现「三闸全过」的 bar", False, "数据构造没造出翻转")
    else:
        # 时间设到 hit_i+1 那根刚开盘 0.3 秒：cur_bar == ks[-1].ts，
        # 于是 i = len-2 正好指向 hit_i 那根（三闸全过），数据也是新鲜的
        now_at = ks_ok[hit_i + 1]["ts"] + 0.3
        ks_trim = ks_ok[:hit_i + 2]
        ctx3 = FakeCtx(ks_trim, px=float(ks_trim[-1]["close"]))
        with patch("backend.strategies.time.time", return_value=now_at):
            r4 = await strat.check_exit(ctx3, pos)
        check(f"命中第 {hit_i} 根的三闸 → 出场", r4 is not None, f"实际 {r4!r}")
        if r4:
            check("出场原因含「镜像出场」", "镜像出场" in str(r4), f"实际 {r4}")
            print(f"     出场理由：{r4}")

    print()
    print("=" * 90)
    print(f"结果：{PASS} 过 / {FAIL} 失败")
    print("=" * 90)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
