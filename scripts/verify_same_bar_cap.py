# -*- coding: utf-8 -*-
"""「同一信号时点同向限笔 + 择优排序」回归验证 —— 2026-11-11 浩哥定 B 方案

配置：max_same_bar_side = 2（同一根 15m K 线同向最多 2 笔）
      择优顺序 = strength 升序（离 ZLSMA50 最近的先开 = 最不追高）

验五件事：
  A. engine._same_bar_allow 的名额判定（真调那个函数，不复制逻辑）
  B. 择优排序方向：strength 升序时留下的是"最不追高"那几笔
  C. 用本地库真实那 8 笔回放：cap=2 应该剩 4 笔（6 个共线空砍到 2）
  D. yaml 参数 + engine 真读了它
  E. 三处排序口径一致（engine / verify_exit_final / exp_sameside）★ 最容易漏

★ 只读，不碰 quantbot.db 写操作。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_same_bar_cap.py
"""
import os
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

OK = FAIL = 0


def chk(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  PASS  %s%s" % (name, ("   " + extra) if extra else ""))
    else:
        FAIL += 1
        print("  FAIL  %s%s" % (name, ("   " + extra) if extra else ""))


def cap_same_bar(trades, k):
    """回测侧筛选：同 entry_ts + 同向，按 strength 升序留前 k 笔。"""
    g = defaultdict(list)
    for t in trades:
        g[(t["entry_ts"], t["side"])].append(t)
    out = []
    for lst in g.values():
        lst.sort(key=lambda x: x.get("strength", 0.0))
        out.extend(lst[:k])
    return sorted(out, key=lambda x: x.get("entry_ts", 0))


def main():
    from backend.engine import _same_bar_allow

    print("=" * 78)
    print("A · engine._same_bar_allow 名额判定（真调）")
    print("=" * 78)
    cnt = {"long": 0, "short": 0}
    r = []
    for _ in range(5):
        ok = _same_bar_allow(cnt, "short", 2)
        r.append(ok)
        if ok:
            cnt["short"] += 1
    chk("cap=2 · 连来 5 个空单 → 前 2 放行、后 3 拦", r == [True, True, False, False, False], str(r))

    cnt = {"long": 0, "short": 0}
    r = []
    for side in ["short", "short", "short", "long", "long", "long"]:
        ok = _same_bar_allow(cnt, side, 2)
        r.append(ok)
        if ok:
            cnt[side] += 1
    chk("cap=2 · 3 空 + 3 多 → 多空各自算，各放 2 笔",
        r == [True, True, False, True, True, False], str(r))
    chk("cap=2 · 空名额已满时多单不受影响",
        _same_bar_allow({"long": 0, "short": 2}, "long", 2) is True)
    chk("cap=0 → 恒放行（关闭）", _same_bar_allow({"long": 99, "short": 99}, "long", 0) is True)
    chk("cap=1 → 只放第一笔", _same_bar_allow({"long": 1, "short": 0}, "long", 1) is False)

    print()
    print("=" * 78)
    print("B · 择优排序方向：strength 升序应留下「最不追高」那几笔")
    print("=" * 78)
    # 同一信号时点 4 个空单，strength = 离均线多远
    demo = [
        dict(symbol="A", side="short", entry_ts=1000, strength=0.20, pnl=-1),
        dict(symbol="B", side="short", entry_ts=1000, strength=1.40, pnl=-1),
        dict(symbol="C", side="short", entry_ts=1000, strength=0.45, pnl=-1),
        dict(symbol="D", side="short", entry_ts=1000, strength=2.10, pnl=-1),
    ]
    kept = cap_same_bar(demo, 2)
    names = sorted(t["symbol"] for t in kept)
    chk("cap=2 留下 strength 最小的两个（A=0.20 / C=0.45）", names == ["A", "C"], str(names))
    chk("把 strength 最大的两个（B=1.40 / D=2.10）拦掉",
        not any(t["symbol"] in ("B", "D") for t in kept))
    desc = sorted(demo, key=lambda x: -x["strength"])[:2]
    chk("对照：降序会留 B/D（证明排序方向是关键变量）",
        sorted(t["symbol"] for t in desc) == ["B", "D"], str(sorted(t["symbol"] for t in desc)))

    print()
    print("=" * 78)
    print("C · 本地库真实 8 笔回放（11-10 23:45 ~ 11-11 02:45）")
    print("=" * 78)
    c = sqlite3.connect("file:quantbot.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""SELECT id,symbol,side,opened_at,closed_at,pnl
                        FROM positions WHERE id BETWEEN 97 AND 104 ORDER BY opened_at""").fetchall()
    # DB 没存 strength（entry_factors 里只有资金面因子），这里按 id 造单调值 ——
    # 只用于验证「笔数削减」这个不变量，不用于业绩统计。
    tr = []
    for i, r in enumerate(rows):
        tr.append(dict(symbol=r["symbol"], side=r["side"], entry_ts=r["opened_at"],
                       exit_ts=(r["closed_at"] or r["opened_at"] + 3600),
                       pnl=r["pnl"] or 0.0, strength=float(i)))
    raw = sum(t["pnl"] for t in tr)
    kept2 = cap_same_bar(tr, 2)
    print("  原始 %d 笔，已平合计 %.2fU" % (len(tr), raw))
    print("  cap=2 → %d 笔，合计 %.2fU" % (len(kept2), sum(t["pnl"] for t in kept2)))
    same_ts = defaultdict(int)
    for t in tr:
        if t["side"] == "short":
            same_ts[t["entry_ts"]] += 1
    big = max(same_ts.values()) if same_ts else 0
    chk("原始确实存在「同一秒同向 6 笔」", big == 6, "最大同秒同向 %d 笔" % big)
    k2 = defaultdict(int)
    for t in kept2:
        if t["side"] == "short":
            k2[t["entry_ts"]] += 1
    chk("cap=2 之后同秒同向最多 2 笔", max(list(k2.values()) or [0]) <= 2,
        "实际 %d" % max(list(k2.values()) or [0]))
    chk("cap=2 保留 4 笔（2 空 + 2 多，多单在不同时点各留 1）",
        len(kept2) == 4, "实际 %d 笔" % len(kept2))
    chk("cap=2 亏损明显少于原始", sum(t["pnl"] for t in kept2) > raw + 400,
        "少亏 %.2fU" % (sum(t["pnl"] for t in kept2) - raw))

    print()
    print("=" * 78)
    print("D · yaml 参数与 engine 接线")
    print("=" * 78)
    import yaml
    cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
    P = cfg["strategies"]["S15_CE_ZLSMA"]["params"]
    chk("yaml max_same_bar_side = 2", int(P.get("max_same_bar_side") or 0) == 2,
        "= %s" % P.get("max_same_bar_side"))
    src = open("backend/engine.py", encoding="utf-8").read()
    chk("engine 真读了这个键", 'max_same_bar_side' in src)
    chk("engine 调用 _same_bar_allow（不是另写一套）",
        '_same_bar_allow(_side_cnt, sig.side, _msb)' in src)
    chk("限笔计数在 allowed 之后（被风控否掉的不占名额）",
        src.index("_side_cnt[sig.side] = _side_cnt.get(sig.side, 0) + 1")
        > src.index("allowed, why, mult = risk.allow_entry"))

    print()
    print("=" * 78)
    print("E · 三处排序口径必须一致（engine / verify_exit_final / exp_sameside）")
    print("=" * 78)
    eng_asc = 'signals.sort(key=lambda s: (\n                    getattr(s, "strength", 0.0),' in src
    chk("engine 排序是 strength 升序（无负号）", eng_asc,
        "命中" if eng_asc else "★没改或还带负号★")
    vf = open("scripts/verify_exit_final.py", encoding="utf-8").read()
    chk("verify_exit_final 回测也按 max_same_bar_side 筛", 'max_same_bar_side' in vf)
    chk("verify_exit_final 排序与 engine 同序（升序）",
        '_lst.sort(key=lambda x: x.get("strength", 0.0))' in vf,
        "命中" if '_lst.sort(key=lambda x: x.get("strength", 0.0))' in vf else "★序不同★")
    ex = open("scripts/exp_sameside_1111.py", encoding="utf-8").read()
    chk("exp_sameside 排序与 engine 同序（升序）",
        'lst.sort(key=lambda x: x.get("strength", 0.0))' in ex,
        "命中" if 'lst.sort(key=lambda x: x.get("strength", 0.0))' in ex else "★序不同★")
    for f, tag in (("scripts/verify_exit_final.py", "verify_exit_final"),
                   ("scripts/exp_sameside_1111.py", "exp_sameside")):
        t = open(f, encoding="utf-8").read()
        bad = '-x.get("strength"' in t
        chk("%s 没有残留降序（-strength）" % tag, not bad,
            "★还有降序★" if bad else "干净")

    print()
    print("=" * 78)
    print("结果：通过 %d 项，失败 %d 项" % (OK, FAIL))
    print("=" * 78)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
