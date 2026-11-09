"""死代码 / 冗余审计。

只读脚本，不修改任何文件。用来回答四个问题：

  A. strategies.yaml 里的每个参数，代码是否真的读它？
  B. 每个函数/方法，是否真的有人调用？
  C. 数据库每一列，是否真的被写、被读？
  D. 有没有空指令 / 永远不成立的分支 / 永远走不到的 return？

判定原则：**剔除注释和 docstring 之后再搜**，否则注释里提一句「trail_arm」
就会被误判成「在用」。这一点做过一遍就知道多重要 —— 不做剔除，误报率能到一半。

用法：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/audit_deadcode.py
"""

import ast
import glob
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

BACKEND = sorted(glob.glob("backend/*.py"))
ALLPY = BACKEND + sorted(glob.glob("backtest/*.py")) + sorted(glob.glob("scripts/*.py"))

# 审计对象：只在 backend/ 里找「死代码」。backtest/scripts 是工具，允许各自独立。
PROD = {p: open(p, encoding="utf-8").read() for p in BACKEND}
TOOLS = {p: open(p, encoding="utf-8").read()
         for p in ALLPY if not p.startswith("backend/")}


def strip_comments(src: str) -> str:
    """去掉 # 注释、三引号 docstring 和普通字符串常量，只留真正的代码 token。

    用 ast 拿不到「字符串在哪里出现」，所以这里用正则做：先删三引号块，再逐行删 #。
    不追求 100% 精确，目标是「注释里提到的名字不算使用」。
    """
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    out = []
    for line in src.splitlines():
        # 只删不在引号里的 #。足够应付本项目，不做完整词法分析。
        q_s = line.count('"')
        q_a = line.count("'")
        idx = line.find("#")
        if idx >= 0 and q_s % 2 == 0 and q_a % 2 == 0:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


CODE = {p: strip_comments(v) for p, v in PROD.items()}
CODE_ALL = dict(CODE)
CODE_ALL.update({p: strip_comments(v) for p, v in TOOLS.items()})

ALL_CODE_TEXT = "\n".join(CODE_ALL.values())

sec = lambda t: print("\n" + "=" * 78 + "\n" + t + "\n" + "=" * 78)

# ─────────────────────────────────────────────────────────────
# A. yaml 参数是否真的被读
# ─────────────────────────────────────────────────────────────
sec("A. strategies.yaml 参数：代码是否真的读取")

cfg = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
s15 = cfg["strategies"]["S15_CE_ZLSMA"]
gcfg = cfg.get("global", {})

unused_s15, unused_global = [], []
for k in sorted(s15):
    pat = re.compile(r'["\']%s["\']' % re.escape(k))
    if not pat.search(ALL_CODE_TEXT):
        unused_s15.append(k)
for k in sorted(gcfg):
    if k in ("universe",):
        continue
    pat = re.compile(r'["\']%s["\']' % re.escape(k))
    if not pat.search(ALL_CODE_TEXT):
        unused_global.append(k)

print("S15 参数 %d 个，无人读取：%d 个" % (len(s15), len(unused_s15)))
for k in unused_s15:
    print("   ✗ %-28s = %r" % (k, s15[k]))
print("global 参数 %d 个，无人读取：%d 个" % (len(gcfg) - 1, len(unused_global)))
for k in unused_global:
    print("   ✗ %-28s = %r" % (k, gcfg[k]))

# ─────────────────────────────────────────────────────────────
# B. 函数 / 方法是否被调用
# ─────────────────────────────────────────────────────────────
sec("B. 函数定义：是否有人调用")

# 收集 backend 里所有 def / async def
defs = []
for p, src in CODE.items():
    for m in re.finditer(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", src, re.M):
        name = m.group(1)
        line = src[:m.start()].count("\n") + 1
        defs.append((p, name, line))

SKIP = {"__init__", "__repr__", "__str__", "__enter__", "__exit__", "__post_init__"}
# FastAPI 路由、回调、生命周期钩子按名字豁免
ROUTE_HINT = re.compile(
    r"@app\.(get|post|put|delete|patch|websocket)|@router\.|startup|shutdown|lifespan")

dead_funcs = []
for p, name, line in defs:
    if name in SKIP:
        continue
    # 调用点：name(  或  module.name(  两种都算。
    # 只写 (?<![\w.])name\( 会把 db.init( / quality.refresh( 全判成死代码 —— 上一版就是这个 bug。
    call = re.compile(r"(?:\.|(?<![\w.]))%s\s*\(" % re.escape(name))
    refs = re.compile(r"(?<![\w])%s(?![\w])" % re.escape(name))
    defn = re.compile(r"(?:async\s+)?def\s+%s\s*\(" % re.escape(name))
    n_call = len(call.findall(ALL_CODE_TEXT)) - len(defn.findall(ALL_CODE_TEXT))
    n_ref = len(refs.findall(ALL_CODE_TEXT)) - len(defn.findall(ALL_CODE_TEXT))
    if n_call <= 0 and n_ref <= 0:
        if ROUTE_HINT.search(CODE[p]):
            continue
        dead_funcs.append((p, name, line))

print("定义 %d 个，无任何调用点：%d 个" % (len(defs), len(dead_funcs)))
for p, name, line in dead_funcs:
    print("   ? %-24s %s:%d" % (name, p, line))

# ─────────────────────────────────────────────────────────────
# C. 数据库字段是否被写 / 读
# ─────────────────────────────────────────────────────────────
sec("C. 数据库字段：是否被写、被读")

# 用真实库的 schema（PRAGMA），不要解析 CREATE TABLE 文本 ——
# 本项目有迁移（ALTER TABLE ADD COLUMN），文本解析会漏掉后加的列。
db_src = open("backend/db.py", encoding="utf-8").read()
migrated = set(re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", db_src))
for t, c in migrated:
    pass

import sqlite3
tables = {}
try:
    con = sqlite3.connect("file:quantbot.db?mode=ro", uri=True)
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in sorted(names):
        tables[t] = [r[1] for r in con.execute("PRAGMA table_info(%s)" % t)]
    con.close()
    src_note = "真实库 PRAGMA"
except Exception as e:  # noqa: BLE001
    print("   读库失败（%s），回退到 CREATE TABLE 文本解析" % e)
    cur = None
    for line in db_src.splitlines():
        m = re.match(r"\s*CREATE TABLE IF NOT EXISTS (\w+)", line)
        if m:
            cur = m.group(1)
            tables[cur] = []
            continue
        if cur is not None:
            if line.strip().startswith(")"):
                cur = None
                continue
            cm = re.match(r"\s*(\w+)\s+(TEXT|INTEGER|REAL|BLOB|NUMERIC)", line)
            if cm:
                tables[cur].append(cm.group(1))
    src_note = "CREATE TABLE 文本"

print("（schema 来源：%s）" % src_note)
dead_cols = []
for tbl, cols in tables.items():
    bad = []
    for c in cols:
        pat = re.compile(r"(?<![\w])%s(?![\w])" % re.escape(c))
        n = len(pat.findall(ALL_CODE_TEXT))
        if n <= 1:          # 只在建表语句里出现一次
            bad.append(c)
    print("· %-16s %3d 列" % (tbl, len(cols)), end="")
    if bad:
        print("   ✗ 从不读写: %s" % ", ".join(bad))
        dead_cols += [(tbl, c) for c in bad]
    else:
        print("   ✓ 全部有用")

# ─────────────────────────────────────────────────────────────
# D. 空指令 / 死分支
# ─────────────────────────────────────────────────────────────
sec("D. 空指令 / 永远不成立的分支")

for p, src in CODE.items():
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        if s == "pass":
            print("   ? pass          %s:%d" % (p, i))
        elif re.match(r"if\s+(False|0|None)\s*:", s):
            print("   ✗ 恒定假分支    %s:%d  %s" % (p, i, s[:60]))
        elif re.match(r"while\s+False\s*:", s):
            print("   ✗ 恒定假循环    %s:%d" % (p, i))
        elif s == "...":
            print("   ? 省略号        %s:%d" % (p, i))

print("\n（空 except 需人工看：见下方 grep 结果）")
for p, src in CODE.items():
    for m in re.finditer(r"except[^:]*:\s*\n(\s*)pass", src):
        line = src[:m.start()].count("\n") + 1
        print("   ? 空 except     %s:%d" % (p, line))

# ─────────────────────────────────────────────────────────────
# E. Signal 属性是否被消费
# ─────────────────────────────────────────────────────────────
sec("E. Signal 字段：是否被消费")

sig_path = "backend/signals.py"
if os.path.exists(sig_path):
    ssrc = CODE[sig_path]
    fields = re.findall(r"^\s{4}(\w+)\s*:\s*[^=]+=", ssrc, re.M)
    print("Signal 字段 %d 个" % len(fields))
    for f in fields:
        pat = re.compile(r"\.%s(?![\w])" % re.escape(f))
        n = len(pat.findall(ALL_CODE_TEXT))
        if n == 0:
            print("   ✗ %-20s 从未被读（.字段 出现 0 次）" % f)
else:
    print("   未找到 %s" % sig_path)

# ─────────────────────────────────────────────────────────────
# F. 文档 vs 代码：文档里点名的每条，字符串是否真在代码里
# ─────────────────────────────────────────────────────────────
sec("F. 逻辑文档点名的常量，是否真存在于代码")

FACTS = [
    ("quality.MIN_QUOTE_VOL", "backend/quality.py", "MIN_QUOTE_VOL"),
    ("quality.MAX_SPIKE", "backend/quality.py", "MAX_SPIKE"),
    ("quality.MONITOR_SIZE", "backend/quality.py", "MONITOR_SIZE"),
    ("quality.STOP_MIN_PCT", "backend/quality.py", "STOP_MIN_PCT"),
    ("quality.STOP_EXIT_PCT", "backend/quality.py", "STOP_EXIT_PCT"),
    ("quality.EFF_MIN", "backend/quality.py", "EFF_MIN"),
    ("quality.EFF_EXIT", "backend/quality.py", "EFF_EXIT"),
    ("quality.EFF_WIN", "backend/quality.py", "EFF_WIN"),
    ("quality.SMOOTH_SEC", "backend/quality.py", "SMOOTH_SEC"),
    ("quality.MIN_DWELL_SEC", "backend/quality.py", "MIN_DWELL_SEC"),
    ("risk.allow_entry", "backend/risk.py", "def allow_entry"),
    ("risk.position_qty", "backend/risk.py", "def position_qty"),
    ("strategies.check_exit", "backend/strategies.py", "def check_exit"),
    ("strategies._mirror_exit", "backend/strategies.py", "def _mirror_exit"),
    ("strategies._quick_fail", "backend/strategies.py", "def _quick_fail"),
    ("strategies._half_take_profit", "backend/strategies.py", "def _half_take_profit"),
    ("engine.manage_positions", "backend/engine.py", "def manage_positions"),
    ("engine.scan_loop", "backend/engine.py", "def scan_loop"),
]
for label, path, needle in FACTS:
    ok = needle in open(path, encoding="utf-8").read()
    print("   %s %-32s %s" % ("✓" if ok else "✗ 缺失", label, path))

print("\n" + "=" * 78)
print("说明：? = 需人工判断；✗ = 高置信度问题")
print("=" * 78)
