# -*- coding: utf-8 -*-
"""出厂总检（B 档）—— 一条命令跑完所有门，最后给一张总表。

跑法：cd E:\\quant-bot && .venv/Scripts/python.exe scripts/verify_exit_final.py

五道门：
  ① py_compile      三个核心文件能编译
  ② tests/run_all   13 个测试文件
  ③ tests/test_new_gates   A–E 组（69 项：配置锁 / 关键K线 / 自适应偏离 / 三取近止损 / 出场链）
  ④ scripts/audit_exit_v2  静态审计（51 项：顺序 / 已删除项 / 回测口径）
  ④b scripts/verify_breakeven  保本移损真跑（16 项：置位/落库/触发价/回归）
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

# ④b 保本移损动态验证（2026-11-11 加）
#     ★ 为什么单独一道门：保本第一版 yaml 有键、代码看着也有，
#       但 update_position 白名单没 be_on → 抛异常被 log.debug 吞掉 →
#       实际从来没生效。静态审计和回测都发现不了，只有真调 check_exit 才露。
run("④b scripts/verify_breakeven.py（保本移损真跑）", [PY, "scripts/verify_breakeven.py"])
# ④c 同K线同向限笔 + 择优排序口径（2026-11-11 加）
#     ★ 为什么单独一道门：这条规则有两个变量（限几笔 / 留哪个），
#       而且「限笔数」和「排序方向」是绑定的 —— 实验里实际出现过
#       只改限笔数不改排序，结论从 +49.78R 翻成 +44.33R 的坑。
#       这道门同时校验：名额判定、排序方向、三处脚本口径一致。
run("④c scripts/verify_same_bar_cap.py（同K线同向限笔 + 排序口径）",
    [PY, "scripts/verify_same_bar_cap.py"])

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
        # 2026-11-11：进场第⑤道闸加了止损距离下限（min_stop_dist_pct 0.60）
        #   → 回测必须跟着读 yaml，否则「回测口径 = 线上口径」这条就破了。
        #   历史（删掉的）min_stop_pct 是监控池筛币，名字相近但位置不同。
        min_stop_pct=float(P.get("min_stop_dist_pct") or 0.0),
        swing=int(P["swing_lookback"]),
        # 2026-11-10 定档：池子止损闸已删 → 两档止损由策略侧处理
        dual_stop_min_pct=float(P.get("dual_stop_min_pct") or 0.0), dual_mode="ce",
        half_at_r=float(P["half_at_r"]), half_tp_r=float(P.get("half_tp_r") or 0.0),
        full_at_r=float(P["full_at_r"]), right_exit=bool(P["right_exit"]),
        right_exit_strict=bool(P["right_exit_strict"]),
        maxh=(int(mh * 4) if mh > 0 else 999999),
        max_risk_pct=float(P["max_risk_pct"]), min_dev_pct=float(P["min_dev_pct"]),
        dev_atr_mult=float(P["dev_atr_mult"]),
        # 2026-11-10 二轮清理：快速失败 / 移动止盈 / 镜像出场 已从策略和 yaml 里整条删除。
        # 回测 sim() 仍保留这三个开关（用于复现历史口径），这里显式关掉，
        # 免得回测和线上算的不是同一套出场。
        qf_bars=0, qf_min_r=0.0, trail_arm=0.0, trail_dd=0.0,
        mirror_exit=False, mirror_bbw_min=0.0,
        # 2026-11-11：保本移损（峰顶到 N R 把有效止损抬到入场价）
        be_at_r=float(P.get("be_at_r") or 0.0),
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
        got = sim(ks, ind, eff_mask=[x >= thr for x in EFFV[s]], **B)
        for t in got:
            t["symbol"] = s
            t["_rank"] = rank[s]
        o.extend(got)

    # ── 同一信号时点同向限笔（2026-11-11 浩哥定：2 笔，yaml max_same_bar_side）
    #    ★ 为什么回测也要筛：线上 engine 会拦，回测不拦 →「回测口径 = 线上口径」就破了。
    #      筛选逻辑与 scripts/exp_sameside_1111.py::cap_same_bar 完全同构。
    _msb = int(P.get("max_same_bar_side") or 0)
    _raw_n = len(o)
    if _msb > 0:
        from collections import defaultdict as _dd
        _g = _dd(list)
        for t in o:
            _g[(t["entry_ts"], t["side"])].append(t)
        _kept = []
        for _lst in _g.values():
            # ★ 按 strength 升序 = 线上 engine 的择优顺序（离均线最近的先开）
            _lst.sort(key=lambda x: x.get("strength", 0.0))
            _kept.extend(_lst[:_msb])
        o = sorted(_kept, key=lambda x: (x["entry_ts"], x.get("strength", 0.0)))
        print("  同K线同向限笔  ≤%d 笔 → %d 笔被拦（%d → %d）"
              % (_msb, _raw_n - len(o), _raw_n, len(o)))
    n = len(o)
    s_ = canon(o)
    win = [t for t in o if t["r"] > 0]
    gp = sum(t["r"] for t in win)
    gl = -sum(t["r"] for t in o if t["r"] <= 0)
    hs = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in o]
    # 中位止损距离（加 min_stop_dist_pct 下限后必看：应该整体上移）
    _sd = sorted(t["risk_pct"] for t in o if t.get("risk_pct"))
    six = dict(n=n, per_day=n / 60.0, win=s_["win"], pl=(gp / gl if gl else 0),
               net=s_["r"], mdd=s_["mdd"], hold=st.mean(hs),
               med=st.median(hs), med_stop=(st.median(_sd) if _sd else 0.0),
               min_stop=(min(_sd) if _sd else 0.0))
    print("  每天单量  %.2f 单/天（%d 笔 / 60 天）" % (n / 60.0, n))
    print("  胜率      %.1f%%" % s_["win"])
    print("  盈亏比    %.2f" % (gp / gl if gl else 0))
    print("  净值      %+.2fR（每笔 %+.4fR）" % (s_["r"], s_["avg"]))
    print("  最大回撤  %.2fR" % s_["mdd"])
    print("  持单时间  均 %.2fh / 中位 %.2fh" % (st.mean(hs), st.median(hs)))
    print("  中位止损  %.2f%%（最小 %.2f%%）" % (six["med_stop"], six["min_stop"]))
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
