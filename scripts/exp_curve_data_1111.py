# -*- coding: utf-8 -*-
"""导出两套参数的累计净R曲线数据（给图表用，2026-11-11）

输出 E:\\Hermes\\cache\\scratch\\curve_cmp.json：
    {"local": {"cum": [[ts, r], ...], "six": {...}},
     "server": {...}}

与 exp_server_replica_1111.py 完全同一套参数与母本，只多导出时间序列。
只读，不写库。
"""
import json
import os
import sys
from collections import defaultdict

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.exp_server_replica_1111 import load_cache, six_of, run_one  # noqa: E402

SC = r"E:\Hermes\cache\scratch"


def main():
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    node = cfg["strategies"]["S15_CE_ZLSMA"]
    P = node["params"]
    ce_len = int(P.get("ce_len") or 1)
    z_len = int(P.get("zlsma_len") or 50)
    thr = float(P["eff_min_signal"])
    mh = float(node.get("max_hold_hours") or 0)
    D, EFFV, rank, ALL = load_cache()

    local_six, local_o = run_one(
        D, EFFV, rank, ALL,
        ce_mult=float(P["ce_mult"]), swing=int(P["swing_lookback"]),
        min_dev=float(P["min_dev_pct"]),
        qf_bars=0, qf_min_r=0.0, trail_arm=0.0, trail_dd=0.0,
        mirror_exit=False, mirror_bbw=0.0,
        half_at_r=float(P["half_at_r"]), half_tp_r=float(P.get("half_tp_r") or 0.0),
        maxh_bars=(int(mh * 4) if mh > 0 else 999999),
        min_stop_pct=float(P.get("min_stop_dist_pct") or 0.0),
        be_at_r=float(P.get("be_at_r") or 0.0),
        msb=int(P.get("max_same_bar_side") or 0),
        thr=thr, ce_len=ce_len, z_len=z_len)

    srv_six, srv_o = run_one(
        D, EFFV, rank, ALL,
        ce_mult=2.0, swing=20, min_dev=0.5,
        qf_bars=16, qf_min_r=0.5, trail_arm=1.5, trail_dd=1.2,
        mirror_exit=True, mirror_bbw=70.0,
        half_at_r=1.8, half_tp_r=1.0, maxh_bars=64,
        min_stop_pct=1.0, be_at_r=0.0, msb=0,
        thr=thr, ce_len=ce_len, z_len=z_len)

    def cum(o):
        oo = sorted(o, key=lambda t: t["exit_ts"])
        out, acc = [], 0.0
        for t in oo:
            acc += t["r"]
            out.append([int(t["exit_ts"]), round(acc, 4)])
        return out

    data = {
        "local": {"cum": cum(local_o), "six": local_six},
        "server": {"cum": cum(srv_o), "six": srv_six},
    }
    p = os.path.join(SC, "curve_cmp.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print("已写出", p)
    print("本地 %d 点 / 服务器 %d 点" % (len(data["local"]["cum"]), len(data["server"]["cum"])))
    print("本地六项:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in local_six.items()})
    print("服务器六项:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in srv_six.items()})


if __name__ == "__main__":
    main()
