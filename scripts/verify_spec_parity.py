# -*- coding: utf-8 -*-
"""定档规则 ⇄ 运行中系统 对账（第六道门 · 专查「文档写了代码没接」）

和 verify_exit_final.py 分工：
  · verify_exit_final 查「代码逻辑对不对」（编译/单测/回测）
  · 本脚本查「定档文档里的规则，运行中系统到底认不认」

做法：读 strategies.yaml 的定档值 → 打运行中服务的真实接口 → 逐条比对输出。
任何一条不一致就 exit 1。**不看代码，只看输出。**

定档版本：2026-11-10（监控池只剩 ①②，止损两档）
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_spec_parity.py
前置：本地服务在 127.0.0.1:8899 跑着（`run_hidden.vbs`）。
"""
import json
import sys
import urllib.request

import yaml

BASE = "http://127.0.0.1:8899"
YAML = "strategies.yaml"
_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本地直连，不走代理

ok = fail = 0
rows = []


def chk(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        rows.append(("PASS", name, detail))
    else:
        fail += 1
        rows.append(("FAIL", name, detail))


def get(path):
    with _op.open(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    cfg = yaml.safe_load(open(YAML, encoding="utf-8"))
    mon = (cfg.get("pools") or {}).get("monitor") or {}
    node = (cfg.get("strategies") or {}).get("S15_CE_ZLSMA") or {}
    P = node.get("params") or {}

    try:
        pool = get("/api/pool")
        st = get("/api/status")
        gates = get("/api/gates")
    except Exception as e:
        print("!! 连不上 %s（服务没起？）: %s" % (BASE, e))
        return 2

    monitor = pool.get("monitor") or pool.get("members") or []
    if isinstance(monitor, dict):
        monitor = list(monitor.keys())
    mset = set(monitor)

    # ① 容量
    size = int(mon.get("size", 40))
    chk("监控池容量 ≤ yaml size", 0 < len(monitor) <= size, "实际 %d / 上限 %d" % (len(monitor), size))

    # ② 止损距离闸已删除（yaml 里不许再有这两键）
    stale = [k for k in ("stop_min_pct", "stop_exit_pct") if k in mon]
    chk("③ 止损距离闸已删除（yaml 无 stop_min/stop_exit）", not stale, "残留 %s" % stale)

    # ③ 删掉③之后的实际效果：主流 4 币不能再被「止损距离」挡在池外
    #    （仍可能被 ① 成交额 / ② 插针 正常拦掉 —— 那是保留的闸，不算失败）
    mainstream = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "DOGEUSDT"]
    why_of = {}
    for r in (pool.get("rows") or []):
        if isinstance(r, dict) and r.get("symbol"):
            why_of[r["symbol"]] = (r.get("why") or "", bool(r.get("in_monitor")))
    blocked_by_stop = [s for s in mainstream
                       if s not in mset and "止损" in why_of.get(s, ("", False))[0]]
    in_pool_n = sum(1 for s in mainstream if s in mset)
    chk("删③后主流币不再被止损距离挡", not blocked_by_stop,
        "在池 %d/4；被止损闸挡 %s；其余原因 %s" % (
            in_pool_n, blocked_by_stop,
            {s: why_of.get(s, ("?",))[0] for s in mainstream if s not in mset}))

    # ④ 两档止损分界
    dsm = float(P.get("dual_stop_min_pct") or 0)
    chk("止损两档分界 = 0.50%", abs(dsm - 0.5) < 1e-9, "yaml %s" % dsm)
    chk("止损距离上限 = 5%", abs(float(P.get("max_risk_pct") or 0) - 5.0) < 1e-9,
        "yaml %s" % P.get("max_risk_pct"))

    # ⑤ 进场七道闸（面板必须看得见）
    keys = ("ce_n", "ha_n", "key_n", "bbw_n", "dev_n", "risk_n", "eff_n")
    chk("闸门面板 7 道全在", all(k in gates for k in keys),
        "缺 %s" % [k for k in keys if k not in gates])
    chk("进场 EFF 门槛 = yaml", abs(float(gates.get("eff_thr") or 0) - float(P.get("eff_min_signal", 0))) < 1e-9,
        "接口 %s / yaml %s" % (gates.get("eff_thr"), P.get("eff_min_signal")))

    # ⑥ 离场参数
    chk("平半线 1.2R", abs(float(P.get("half_at_r", 0)) - 1.2) < 1e-9, "yaml %s" % P.get("half_at_r"))
    chk("兜底线 2.0R", abs(float(P.get("full_at_r", 0)) - 2.0) < 1e-9, "yaml %s" % P.get("full_at_r"))
    chk("右侧止盈 = 严格", bool(P.get("right_exit")) and bool(P.get("right_exit_strict")),
        "right_exit=%s / strict=%s" % (P.get("right_exit"), P.get("right_exit_strict")))
    chk("时间止损 = 0（关闭）", float(node.get("max_hold_hours", -1)) == 0,
        "yaml %s" % node.get("max_hold_hours"))
    deleted = [k for k in ("quickfail_hours", "quickfail_min_r", "trail_arm", "trail_dd",
                           "mirror_bbw_min", "mirror_exit", "half_tp_r") if k in P]
    chk("已删规则无残留键（快速失败/镜像/移动止盈）", not deleted, "残留 %s" % deleted)

    # ⑦ 服务健康
    chk("服务无 last_error", not st.get("last_error"), "last_error=%r" % st.get("last_error"))

    print("=" * 80)
    print("定档规则 ⇄ 运行中系统 对账（定档 2026-11-10）")
    print("=" * 80)
    for s, n, d in rows:
        print("  %-5s %-44s %s" % (s, n, d))
    print("=" * 80)
    print("通过 %d 项，失败 %d 项" % (ok, fail))
    if fail:
        print("最终：有 %d 条定档规则在运行系统里没生效 ✗" % fail)
        return 1
    print("最终：全部一致 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
