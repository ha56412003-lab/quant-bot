"""诊断：监控池里的币，现在卡在哪一道入场闸。

入场三道闸（strategies.py S15CeZlsma.evaluate）：
  ① CE 方向翻转：dir[i] != dir[i-1]
  ② HA 关键K线：做多要 HA 阳线且收盘站上 ZLSMA；做空要 HA 阴线且收盘跌破 ZLSMA
  ③ BBW 分位 > 70%：近 100 根布林带宽度分位

注意：
  - 监控池名单必须走 /api/pool（跨进程读 quality.monitor_list() 会得到假 0）
  - _ce_zlsma 返回的是「序列字典」（每项都是 list），不是单值

跑法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe diag_entry_gates.py
"""
import asyncio
import json
import sys
import urllib.request

sys.path.insert(0, ".")

from backend import binance  # noqa: E402
from backend.strategies import _ce_zlsma  # noqa: E402

API_POOL = "http://127.0.0.1:8899/api/pool"
LOOKBACK_BARS = 12          # 回看最近多少根已收盘 K 线


def get_monitor() -> list[str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(API_POOL, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    return list(d.get("monitor") or [])


async def main():
    mon = get_monitor()
    if not mon:
        print("监控池为空，先等服务刷新")
        return

    print(f"监控池 {len(mon)} 个币，逐个检查最近 {LOOKBACK_BARS} 根已收盘 15m K 线")
    print()

    tot_bars = 0
    n_flip = n_kbar = n_bbw = n_all = 0
    only1 = only12 = all3 = 0
    per_sym = []
    # 每次「① CE 翻转 且 ② HA 关键K线」都过时，记下当时的 BBW 分位，
    # 供后面扫门槛：门槛放到多少，这些机会能放出多少个
    flip_kbar_bbw: list[float] = []

    for sym in mon:
        try:
            ks = await binance.klines(sym, "15m", 220)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym:<14} 拉K线失败: {exc}")
            continue
        if not ks or len(ks) < 120:
            continue
        ks = ks[:-1]                     # 丢掉未收盘的最后一根

        try:
            d = _ce_zlsma(ks, ce_len=1, ce_mult=2.0, z_len=50,
                          use_close=True, use_ha=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym:<14} 指标计算失败: {exc}")
            continue

        dirs = d["dir"]
        zl = d["zlsma"]
        ha_c = d["close"]
        ha_o = d["open"]
        bbw_p = d.get("bbw_pct") or []

        c_flip = c_kbar = c_bbw = c_all = 0
        n = 0
        start = max(101, len(ks) - LOOKBACK_BARS)
        for i in range(start, len(ks)):
            if i < 1:
                continue
            dn, dp = dirs[i], dirs[i - 1]
            if dn is None or dp is None:
                continue
            n += 1
            flip = dn != dp
            kbar = (dn == 1 and ha_c[i] > ha_o[i] and ha_c[i] > zl[i]) or \
                   (dn == -1 and ha_c[i] < ha_o[i] and ha_c[i] < zl[i])
            bp = bbw_p[i] if i < len(bbw_p) else None
            b_ok = bp is not None and bp > 70.0

            c_flip += flip
            c_kbar += flip and kbar
            c_bbw += b_ok
            c_all += flip and kbar and b_ok
            if flip:
                only1 += 1
                if kbar:
                    only12 += 1
                    if bp is not None:
                        flip_kbar_bbw.append(bp)
                if b_ok:
                    all3 += 1

        tot_bars += n
        n_flip += c_flip
        n_kbar += c_kbar
        n_bbw += c_bbw
        n_all += c_all
        if c_flip or c_all or c_bbw:
            last_bw = None
            for v in reversed(bbw_p):
                if v is not None:
                    last_bw = v
                    break
            per_sym.append((sym, n, c_flip, c_kbar, c_bbw, c_all, last_bw))

    print(f"共检查 {tot_bars} 个「币×K线」样本")
    print()
    print("=" * 76)
    print("单根 K 线层面：各闸命中率")
    print("=" * 76)
    print(f"  ① CE 翻转                    {n_flip:>5} 次  ({n_flip/max(1,tot_bars)*100:>5.2f}%)")
    print(f"  ③ BBW 分位 > 70%             {n_bbw:>5} 次  ({n_bbw/max(1,tot_bars)*100:>5.2f}%)  ← 定义上天然 <=30%")
    print(f"  ① 且 ② HA 关键K线            {n_kbar:>5} 次  ({n_kbar/max(1,tot_bars)*100:>5.2f}%)")
    print(f"  三道全过                     {n_all:>5} 次  ({n_all/max(1,tot_bars)*100:>5.2f}%)")
    print()
    print("=" * 76)
    print("只看 CE 翻转过的那几根（真正的候选），后面两闸卡掉多少")
    print("=" * 76)
    if only1:
        print(f"  CE 翻转过              {only1:>4} 次")
        print(f"  → ② HA 也过            {only12:>4} 次   ({(only1-only12)/only1*100:>5.1f}% 被 HA 卡掉)")
        print(f"  → ③ BBW 也过           {all3:>4} 次   ({(only1-all3)/only1*100:>5.1f}% 被卡掉)")
    else:
        print("  这段时间一次 CE 翻转都没有")

    print()
    print("=" * 76)
    print("有动静的币")
    print("=" * 76)
    print(f"{'币种':<14}{'样本':>5}{'①翻转':>7}{'①+②':>7}{'③BBW':>7}{'三闸':>6}{'BBW现值':>10}")
    print("-" * 76)
    for row in sorted(per_sym, key=lambda x: (-x[5], -x[2])):
        s, n, a, b, c, dd, bwn = row
        bw = f"{bwn:.0f}" if bwn is not None else "-"
        print(f"{s:<14}{n:>5}{a:>7}{b:>7}{c:>7}{dd:>6}{bw:>10}")

    vals = [r[6] for r in per_sym if r[6] is not None]
    if vals:
        vals.sort()
        over = sum(1 for v in vals if v > 70)
        print()
        print(f"当前 BBW 分位：中位 {vals[len(vals)//2]:.0f}，>70 的有 {over}/{len(vals)} 个币")
        print(f"  → 市场现在{'在波动扩张期' if over > len(vals)*0.3 else '偏收敛，BBW 这道闸正在拦人'}")

    # ── BBW 门槛扫描：降门槛能多放出多少开仓 ──────────────────
    print()
    print("=" * 76)
    print("如果放宽 BBW 门槛，三闸全过会变成多少次（近 12 根 × 34 币）")
    print("=" * 76)
    print(f"{'BBW门槛':>8}{'三闸全过':>10}{'占样本':>10}{'相对现在':>12}")
    print("-" * 76)
    print(f"（① + ② 都过的样本共 {len(flip_kbar_bbw)} 个，看门槛放多低能放出几个）")
    print()
    base = None
    for thr in (90.0, 80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 0.0):
        n = sum(1 for v in flip_kbar_bbw if v > thr)
        if thr == 70.0:
            base = n
        mult = f"{n / base:.1f}x" if base else "-"
        note = "  <- 现在" if thr == 70.0 else ("  <- 等于关掉这道闸" if thr == 0.0 else "")
        print(f"{thr:>8.0f}{n:>10}{n/max(1,tot_bars)*100:>9.2f}%{mult:>12}{note}")


if __name__ == "__main__":
    asyncio.run(main())
