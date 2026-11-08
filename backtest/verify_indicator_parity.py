"""线上代码 vs 回测脚本 —— 指标逐值对账

为什么必须先做这个：
  回测结论（+100.1R、945 笔、回撤-25.3）全部来自 backtest/s15_exit_mirror.py，
  那是独立写的一套指标实现。线上跑的是 backend/strategies.py 里的 _ce_zlsma()。
  两套实现只要差一点点（HA 的算法、ZLSMA 的初值、ATR 的平滑方式），
  回测结论就完全不适用于线上 —— 等于白测。

做法：
  取同一份真实 K 线，分别用两边算指标，逐值对比。
  只要有一个值对不上，就说明有问题，必须查清楚再谈"生效"。

用法：python backtest/verify_indicator_parity.py
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backtest"))

from backend.strategies import _ce_zlsma          # 线上实现
from s15_final import build                       # 回测实现

TOL = 1e-9


def cmp_series(name, a, b, n_check, sym, fails):
    """逐值对比两个序列，None 也要对齐。"""
    if a is None or b is None:
        fails.append(f"{sym}.{name}: 一边为空 (live={a is not None}, bt={b is not None})")
        return
    if len(a) != len(b):
        fails.append(f"{sym}.{name}: 长度不同 live={len(a)} bt={len(b)}")
        return
    bad = 0
    first = None
    for i in range(len(a)):
        x, y = a[i], b[i]
        if x is None and y is None:
            continue
        if x is None or y is None:
            bad += 1
            if first is None:
                first = (i, x, y)
            continue
        scale = max(abs(x), abs(y), 1e-12)
        if abs(x - y) / scale > 1e-6:
            bad += 1
            if first is None:
                first = (i, x, y)
    if bad:
        fails.append(f"{sym}.{name}: {bad}/{len(a)} 个值不一致，首个 idx={first[0]} "
                     f"live={first[1]} bt={first[2]}")


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))
    if not files:
        print("找不到回测数据")
        return 1
    print(f"对账文件数: {len(files)}\n")

    fails = []
    checked = 0
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        ks = json.load(open(fp, encoding="utf-8"))
        if len(ks) < 800:
            continue
        # 线上：_ce_zlsma(ks, ce_len=1, ce_mult=2.0, z_len=50, use_close=True, use_ha=True)
        live = _ce_zlsma(ks, 1, 2.0, 50, True, True)
        bt = build(ks)
        checked += 1

        cmp_series("ha_close", live["close"], bt["hc"], len(ks), sym, fails)
        cmp_series("ha_open", live["open"], bt["ho"], len(ks), sym, fails)
        cmp_series("zlsma", live["zlsma"], bt["zlsma"], len(ks), sym, fails)
        cmp_series("ce_dir", live["dir"], bt["dir"], len(ks), sym, fails)
        cmp_series("atr", live["atr"], bt["atr"], len(ks), sym, fails)
        cmp_series("bbw_pct", live["bbw_pct"], bt["bbw_pct"], len(ks), sym, fails)

    print(f"实际对账 {checked} 个币 × 6 组指标\n")
    if fails:
        print("=" * 90)
        print(f"对不上：{len(fails)} 处")
        print("=" * 90)
        for f in fails[:30]:
            print("  " + f)
        if len(fails) > 30:
            print(f"  ... 还有 {len(fails)-30} 处")
        return 1
    print("=" * 90)
    print("全部一致 —— 线上指标公式与回测脚本逐值相同，回测结论可适用于线上")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    sys.exit(main())
