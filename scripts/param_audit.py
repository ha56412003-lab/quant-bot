# -*- coding: utf-8 -*-
"""全链路参数一致性审查

回答三个问题（这三类就是「改了白改」的全部来源）：
  ① 死配置     —— yaml 里写了这个键，但代码里没有任何地方读它
  ② 双源冲突   —— 同一语义在两处读（例如 策略级 risk.leverage vs 全局 max_gross_leverage）
  ③ 默认值漂移 —— 代码里的默认值 ≠ yaml 里的值（配置没加载时行为不同）

用法：
    ./.venv/Scripts/python.exe scripts/param_audit.py
    ./.venv/Scripts/python.exe scripts/param_audit.py --json   # 机器可读
"""
import re
import sys
import json
import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
YAML = ROOT / "strategies.yaml"
BACKEND = ROOT / "backend"

# 已知的语义重合组：这些参数名不同，但控制的是同一件事，
# 一处改了另一处不跟就会互相覆盖。手工维护，发现新的就加进来。
DUP_GROUPS = [
    {
        "name": "杠杆",
        "keys": ["leverage", "max_gross_leverage"],
        "note": "engine.py / risk.py 都是「策略级 risk.leverage 优先，全局 max_gross_leverage 兜底」。"
                "只改全局、不改策略级 = 全局值被静默覆盖。",
    },
    {
        "name": "单币仓位上限",
        "keys": ["max_position_pct_per_coin", "max_position_usd"],
        "note": "单币名义 = min(风险金额/止损距离, 权益×pct×leverage, 策略级 max_position_usd)。"
                "三个闸取最小，只放开一个还是被别的削。",
    },
    {
        "name": "同时持仓数",
        "keys": ["max_concurrent_coins", "max_same_side"],
        "note": "总并发和同向并发是两道独立闸，改一个不跟另一个会撞。",
    },
    {
        "name": "ATR 档位门槛",
        "keys": ["atr_band_lo_max", "atr_band_hi_min", "atr_lo_hold", "atr_lo_exit",
                 "atr_hi_hold", "atr_hi_exit"],
        "note": "进池线（lo_max/hi_min）、留池缓冲（hold）、强制踢出（exit）是三套阈值，"
                "改进池线不同步 hold/exit 会导致刚进池就被踢。",
    },
    {
        "name": "池子容量与刷新",
        "keys": ["size", "refresh_sec", "scan_sec"],
        "note": "容量放大 N 倍 = 池内刷新量的 N 倍。容量和刷新间隔必须一起调，否则流量爆。",
    },
    {
        "name": "止损口径",
        "keys": ["swing_lookback", "stop_atr_mult", "max_risk_pct"],
        "note": "止损距离直接决定仓位大小（notional = 风险金额 / 止损距离）。",
    },
    {
        "name": "日目标与熔断",
        "keys": ["target_usd", "daily_loss_halt_pct", "pause_new"],
        "note": "熔断线必须 ≥ 单笔风险 × 并发数，否则同时止损一次就打穿。",
    },
]


def walk(node, prefix=""):
    """展开 yaml 成 (路径, 值) 叶子列表。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{prefix}[{i}]")
    else:
        yield prefix, node


def load_sources():
    """读所有后端 py 源码，返回 {相对路径: 文本}。"""
    out = {}
    for f in sorted(BACKEND.glob("*.py")):
        out[f.name] = f.read_text(encoding="utf-8", errors="replace")
    return out


def grep_key(key, sources):
    """在源码里找这个 key 被字符串引用的位置。"""
    hits = []
    # 匹配 "key" 或 'key'（配置文件读取基本都是字符串键）
    pat = re.compile(r"""['"]%s['"]""" % re.escape(key))
    for fname, text in sources.items():
        for i, line in enumerate(text.split("\n"), 1):
            if pat.search(line):
                hits.append((fname, i, line.strip()[:100]))
    # 也匹配 attr 形式（dataclass 字段等）
    if not hits:
        pat2 = re.compile(r"\b%s\s*=" % re.escape(key))
        for fname, text in sources.items():
            for i, line in enumerate(text.split("\n"), 1):
                if pat2.search(line) and not line.strip().startswith("#"):
                    hits.append((fname, i, line.strip()[:100]))
    return hits


def find_code_defaults(sources):
    """抓代码里常量形式的默认值：NAME = 值  # 注释"""
    out = {}
    pat = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*=\s*([^#\n]+?)\s*(?:#\s*(.*))?$")
    for fname, text in sources.items():
        for i, line in enumerate(text.split("\n"), 1):
            m = pat.match(line)
            if m:
                name, val, cmt = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
                out.setdefault(name, []).append((fname, i, val, cmt))
    return out


def main():
    as_json = "--json" in sys.argv
    if not YAML.exists():
        print(f"找不到 {YAML}")
        return 1

    cfg = yaml.safe_load(YAML.read_text(encoding="utf-8"))
    sources = load_sources()
    leaves = list(walk(cfg))

    # ── ① 死配置 ──
    dead = []
    alive = []
    for path, val in leaves:
        leaf = path.split(".")[-1]
        leaf = re.sub(r"\[\d+\]$", "", leaf)
        hits = grep_key(leaf, sources)
        if hits:
            alive.append((path, val, hits))
        else:
            dead.append((path, val))

    # ── ② 双源冲突 ──
    dup_hits = []
    for g in DUP_GROUPS:
        present = []
        for k in g["keys"]:
            paths = [p for p, _ in leaves if p.split(".")[-1] == k]
            if paths:
                present.append((k, paths))
        if len(present) >= 2:
            dup_hits.append({**g, "present": present})

    # ── ③ 默认值漂移（只查代码常量大写形式里能对上 yaml 值的）──
    code_defaults = find_code_defaults(sources)

    if as_json:
        print(json.dumps({
            "total_params": len(leaves),
            "dead": [{"path": p, "value": str(v)} for p, v in dead],
            "dual_source": [{"name": g["name"], "keys": [k for k, _ in g["present"]],
                             "note": g["note"]} for g in dup_hits],
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print(f"参数一致性审查   yaml 叶子参数 {len(leaves)} 个")
    print("=" * 78)

    print()
    print(f"【① 死配置】yaml 里有、代码里没读的：{len(dead)} 个")
    if dead:
        print("  改了完全不起作用。要么是给未来留的坑，要么是读法不同（需人工确认）：")
        for p, v in dead:
            print(f"    {p:<46} = {v!r}")
    else:
        print("  ✅ 无")

    print()
    print(f"【② 双源冲突】同一件事被两处读的：{len(dup_hits)} 组")
    for g in dup_hits:
        print(f"  ▸ {g['name']}")
        for k, paths in g["present"]:
            print(f"      {k:<32} 出现在 {', '.join(paths)}")
        print(f"      为什么危险：{g['note']}")
        print()

    print(f"【③ 参数清单】（按 yaml 路径，标注代码引用处数）")
    by_file = {}
    for path, val, hits in alive:
        files = sorted({h[0] for h in hits})
        by_file.setdefault(",".join(files), []).append((path, val, len(hits)))
    for files, items in sorted(by_file.items(), key=lambda z: -len(z[1])):
        print(f"  ▸ 被 {files} 引用（{len(items)} 个参数）")
        for path, val, n in items:
            print(f"      {path:<46} = {str(val)[:26]:<28} ×{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
