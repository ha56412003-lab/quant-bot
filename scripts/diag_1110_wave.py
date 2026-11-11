# -*- coding: utf-8 -*-
"""诊断：两闸（共振 ≥6 & 缩量 <1.2）能不能挡掉 11-10 那波连亏的 6 笔？

做法：复用 exp_gates_1111 的 build()，筛出 11-10 23:00 ~ 11-11 01:00 的所有原始信号，
逐笔打印它的共振数、量比、盈亏，再模拟两闸各拦掉谁、拦完剩几笔亏多少。

只读，不写库。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/diag_1110_wave.py
"""
import datetime
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.exp_gates_1111 import apply_gates, build, six   # noqa: E402

TZ = datetime.timezone(datetime.timedelta(hours=8))


def f(ts):
    return datetime.datetime.fromtimestamp(int(ts), TZ).strftime("%m-%d %H:%M")


def main():
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    tr = build(cfg)

    # 11-10 22:00 ~ 11-11 03:00 的原始信号
    t0 = int(datetime.datetime(2026, 11, 10, 22, 0, tzinfo=TZ).timestamp())
    t1 = int(datetime.datetime(2026, 11, 11, 3, 0, tzinfo=TZ).timestamp())
    wave = sorted([t for t in tr if t0 <= int(t["entry_ts"]) <= t1],
                  key=lambda x: (x["entry_ts"], x["symbol"]))

    print("=" * 96)
    print("11-10 22:00 ~ 11-11 03:00 的原始信号（未限笔、未过闸）")
    print("=" * 96)
    if not wave:
        print("  这个窗口没有任何信号 —— 说明当前参数下那波信号不在这里产生")
        return
    print("%-14s %-5s %-12s %8s %8s %9s %10s" %
          ("币", "方向", "进场时间", "共振数", "量比", "R", "被谁拦"))
    print("-" * 96)

    VI = 1.2      # 缩量闸
    RI = 6        # 共振闸
    kept = []
    for t in wave:
        vr = t["_vol_ratio"]
        hits = []
        if t["_n_same"] >= RI:
            hits.append("共振")
        if vr is not None and vr < VI:
            hits.append("缩量")
        if not hits:
            kept.append(t)
        print("%-14s %-5s %-12s %8d %8s %+9.2f %10s"
              % (t["symbol"], t["side"], f(t["entry_ts"]), t["_n_same"],
                 ("%.2fx" % vr) if vr is not None else "n/a", t["r"],
                 "+".join(hits) if hits else "通过"))

    a = six(wave)
    print("-" * 96)
    print("这波原始信号：%d 笔 / 胜率 %.1f%% / 总R %+.2f" % (a["n"], a["win"], a["net"]))
    print()

    # 逐档模拟
    print("=" * 96)
    print("三种规则拦完之后的对照（同 bar 同向限 2 笔，strength 升序）")
    print("=" * 96)
    print("%-30s %7s %8s %9s %10s" % ("规则", "笔数", "胜率", "总R", "被拦掉"))
    print("-" * 96)
    for name, rg, vg in (("什么都不拦（只限2笔）", 0, 0.0),
                         ("共振 ≥6 & 缩量 <1.0", 6, 1.0),
                         ("共振 ≥6 & 缩量 <1.2", 6, 1.2)):
        o, n0, n1, n2 = apply_gates(tr, rg, vg)
        # 只看这波窗口内
        sub = [t for t in o if t0 <= int(t["entry_ts"]) <= t1]
        s = six(sub) if sub else None
        if s:
            print("%-30s %7d %7.1f%% %+9.2f %10s"
                  % (name, s["n"], s["win"], s["net"],
                     "%d 笔" % (len(wave) - s["n"])))
        else:
            print("%-30s %7d %8s %9s %10s" % (name, 0, "-", "0.00", "%d 笔（全拦）" % len(wave)))

    print()
    print("★ 说明：这里统计的是回测信号，与实盘 11-10 23:45 那 6 笔不完全等价 ——")
    print("  实盘那 6 笔是当时参数下的成交，回测是按当前参数重新生成的信号。")


if __name__ == "__main__":
    main()
