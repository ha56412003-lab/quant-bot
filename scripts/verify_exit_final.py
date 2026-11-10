# -*- coding: utf-8 -*-
"""出厂总检（B 档）—— 一条命令跑完所有门，最后给一张总表。

跑法：cd E:\\quant-bot && .venv/Scripts/python.exe scripts/verify_exit_final.py

五道门：
  ① py_compile      三个核心文件能编译
  ② tests/run_all   13 个测试文件
  ③ tests/test_new_gates   A–E 组（69 项：配置锁 / 关键K线 / 自适应偏离 / 三取近止损 / 出场链）
  ④ scripts/audit_exit_v2  静态审计（51 项：顺序 / 已删除项 / 回测口径）
  ⑤ 回测六项        strategies.yaml 的真实参数跑 40 币 60 天，给单量·胜率·盈亏比·净值·回撤·持单时间

任何一道红了，最后一行就是 FAIL。
"""
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable

ENV = dict(os.environ, PYTHONIOENCODING="utf-8")
ROWS = []


def run(tag, args, timeout=1200):
    print()
    print("=" * 78)
    print("▶ " + tag)
    print("=" * 78)
    p = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env=ENV, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    tail = "\n".join(out.strip().split("\n")[-12:])
    print(tail)
    ok = p.returncode == 0
    ROWS.append((tag, "PASS" if ok else "FAIL", p.returncode))
    return ok, out


# ① 编译
print("▶ py_compile 三个核心文件")
r = subprocess.run([PY, "-m", "py_compile", "backend/strategies.py",
                    "backend/quality.py", "backend/engine.py"],
                   capture_output=True, text=True, env=ENV)
print("  " + ("compile OK" if r.returncode == 0 else (r.stderr or "")[-400:]))
ROWS.append(("① py_compile", "PASS" if r.returncode == 0 else "FAIL", r.returncode))

# ② 单元测试
run("② tests/run_all.py（13 个测试文件）", [PY, "tests/run_all.py"])

# ③ 新闸门测试
ok3, _ = run("③ tests/test_new_gates.py（A–E 组）", [PY, "tests/test_new_gates.py"])

# ④ 静态审计
run("④ scripts/audit_exit_v2.py（出场链静态审计）", [PY, "scripts/audit_exit_v2.py"])

# ⑤ 回测六项
print()
print("=" * 78)
print("▶ ⑤ 回测六项：strategies.yaml 真实参数 × 40 币 × 60 天")
print("=" * 78)
six = {}
try:
    import pickle
    import statistics as st
    import yaml
    from backtest.exit_speed_test import sim
    from backtest.s15_live_parity import stats as canon

    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    node = cfg["strategies"]["S15_CE_ZLSMA"]
    P = node["params"]
    MON = cfg["pools"]["monitor"]
    mh = float(node.get("max_hold_hours") or 0)
    B = dict(
        key_bar=bool(P["key_bar"]), key_mult=float(P["key_mult"]),
        key_closepos=float(P["key_closepos"]), key_bodyfrac=float(P["key_bodyfrac"]),
        stop_key=bool(P["stop_key"]), stop_mode="both",
        min_stop_pct=float(MON["stop_min_pct"]), swing=int(P["swing_lookback"]),
        half_at_r=float(P["half_at_r"]), half_tp_r=float(P["half_tp_r"]),
        full_at_r=float(P["full_at_r"]), right_exit=bool(P["right_exit"]),
        right_exit_strict=bool(P["right_exit_strict"]),
        maxh=(int(mh * 4) if mh > 0 else 999999),
        qf_bars=int(round(float(P["quickfail_hours"]) * 4)),
        qf_min_r=float(P["quickfail_min_r"]),
        trail_arm=float(P["trail_arm"]), trail_dd=float(P["trail_dd"]),
        max_risk_pct=float(P["max_risk_pct"]), min_dev_pct=float(P["min_dev_pct"]),
        dev_atr_mult=float(P["dev_atr_mult"]), mirror_exit=bool(P["mirror_exit"]),
        mirror_bbw_min=float(P["mirror_bbw_min"]),
    )
    thr = float(P["eff_min_signal"])
    SC = r"E:\Hermes\cache\scratch"
    with open(os.path.join(SC, "pool_cache.pkl"), "rb") as f:
        D = pickle.load(f)
    with open(os.path.join(SC, "effcache.pkl"), "rb") as f:
        EFFV = pickle.load(f)
    with open(os.path.join(SC, "rank_cache.pkl"), "rb") as f:
        rank = pickle.load(f)
    ALL = sorted([s for s in D if s in rank], key=lambda v: rank[v])

    o = []
    for s in ALL[:40]:
        ks, ind, _ = D[s]
        o.extend(sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B))
    n = len(o)
    s_ = canon(o)
    win = [t for t in o if t["r"] > 0]
    gp = sum(t["r"] for t in win)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    six = dict(n=n, per_day=n / 60.0, win=s_["win"], pl=(gp / gl if gl else 0),
               net=s_["r"], mdd=s_["mdd"], hold=st.mean(hs),
               med=st.median(hs))
    print("  每天单量  %.2f 单/天（%d 笔 / 60 天）" % (n / 60.0, n))
    print("  胜率      %.1f%%" % s_["win"])
    print("  盈亏比    %.2f" % (gp / gl if gl else 0))
    print("  净值      %+.2fR（每笔 %+.4fR）" % (s_["r"], s_["avg"]))
    print("  最大回撤  %.2fR" % s_["mdd"])
    print("  持单时间  均 %.2fh / 中位 %.2fh" % (st.mean(hs), st.median(hs)))
    ROWS.append(("⑤ 回测六项", "PASS", 0))
except Exception as exc:  # noqa: BLE001
    print("  回测跑不起来：%s" % exc)
    ROWS.append(("⑤ 回测六项", "FAIL", 1))

print()
print("=" * 78)
print("总检")
print("=" * 78)
bad = 0
for tag, st_, _ in ROWS:
    print("  %-6s %s" % (st_, tag))
    if st_ != "PASS":
        bad += 1
if six:
    print()
    print("  六项：%.2f 单/天 | 胜率 %.1f%% | 盈亏比 %.2f | 净值 %+.1fR | 回撤 %.2fR | 均持 %.2fh"
          % (six["per_day"], six["win"], six["pl"], six["net"], six["mdd"], six["hold"]))
print()
print("最终：" + ("全部通过 ✓" if bad == 0 else "有 %d 道门没过 ✗" % bad))
print("=" * 78)
sys.exit(0 if bad == 0 else 1)
