# -*- coding: utf-8 -*-
"""出场链审计 v2 —— **B 档口径（2026-11-10 浩哥拍板）**

本脚本是「静态审计」：只看代码结构和参数，不跑行情。
行为验证在 tests/test_new_gates.py（C/D/E 组，共 69 项）。

跑法：cd E:\\quant-bot && .venv/Scripts/python.exe scripts/audit_exit_v2.py

B 档出场链（就四条，顺序就是这个顺序）：
  ① half_at_r 1.2R  → 平一半            (_half_take_profit)
  ② full_at_r 2.0R  → 兜底全平（只对已平半的仓位）(_full_target)
  ③ 右侧止盈        → HA 反向 + HA 收盘穿 ZLSMA50 + 那根【原始K线】是反向关键K线 (_right_exit)
  ④ 镜像出场        → mirror_exit=false，整条停用 (_mirror_exit)
  ⑤ 硬止损          → 在 engine.py，优先于以上所有

已删除（不许复活）：16h 时间止损、快速失败、移动止盈、镜像出场（规则本身）。

⚠ 审计实现坑（踩过两次，别再踩）：
  1) 必须剔掉注释和 docstring 再搜 —— 否则搜到的是说明文字里的旧规则
  2) 文件里有两个 check_exit（基类 + S15 实现）→ 必须取真正的那个
  3) 不能用「三引号通配正则」全文去 docstring：docstring 里再有引号会把真代码一起删掉，
     导致后面的函数"消失"。改成【按行切函数块，再对块内去注释】。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import yaml

OK = 0
BAD = []


def chk(cond, name, detail=""):
    global OK
    if cond:
        OK += 1
        print("  PASS  %s" % name)
    else:
        BAD.append("%s  %s" % (name, detail))
        print("  FAIL  %s    %s" % (name, detail))


def read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def strip_comments(text):
    """去掉注释、docstring 和普通字符串 —— 只留真代码。"""
    text = re.sub(r'"""(?:[^"\\]|\\.|"(?!""))*"""', "", text)
    text = re.sub(r"'''(?:[^'\\]|\\.|'(?!''))*'''", "", text)
    text = re.sub(r"#[^\n]*", "", text)
    return text


def method_block(src_text, name, last=True):
    """按行切出类方法体（到下一个同级 def/@ 为止），返回去注释后的代码。

    last=True 取最后一个同名方法（文件里基类 + 实现都有 check_exit）。
    """
    lines = src_text.split("\n")
    cand = [i for i, ln in enumerate(lines)
            if re.match(r"    (?:async )?def %s\(" % re.escape(name), ln)]
    if not cand:
        return ""
    i = cand[-1] if last else cand[0]
    out = [lines[i]]
    for ln in lines[i + 1:]:
        if re.match(r"    (?:async )?def \w+", ln) or re.match(r"    @", ln):
            break
        out.append(ln)
    return strip_comments("\n".join(out))


S_SRC = read("backend/strategies.py")
E_SRC = read("backend/engine.py")
CFG = yaml.safe_load(open("strategies.yaml", encoding="utf-8"))
NODE = CFG["strategies"]["S15_CE_ZLSMA"]
P = NODE["params"]
MON = CFG["pools"]["monitor"]

print("=" * 78)
print("一、出场四条：顺序与门槛（策略层）")
print("=" * 78)

body = method_block(S_SRC, "check_exit")
chk(bool(body), "定位到 S15 的 check_exit（不是基类）", "没找到方法体")
chk("_half_take_profit" in body and "_full_target" in body and "_right_exit" in body,
    "三条价位出场都在 check_exit 里被调用（平半 / 兜底 / 右侧）",
    "缺: %s" % [n for n in ("_half_take_profit", "_full_target", "_right_exit")
                if n not in body])
chk("_mirror_exit" not in body and "_quick_fail" not in body,
    "镜像 / 快速失败 已不在 check_exit 里（整条删除，不是开关关闭）")
positions = [(k, body.find(p)) for k, p in
             (("half", "_half_take_profit"), ("full", "_full_target"),
              ("right", "_right_exit"))]
seq = [k for k, v in sorted(positions, key=lambda kv: kv[1]) if v >= 0]
chk(seq == ["half", "full", "right"],
    "顺序 = 平半 1.2R → 兜底 2.0R → 右侧止盈", "实际 %s" % seq)
chk("_refresh_peak" in body, "峰顶维护在 check_exit 最前面（无条件）",
    "峰顶藏在镜像里 → 镜像一关峰顶冻结")
chk(body.find("_refresh_peak") < body.find("_half_take_profit"),
    "峰顶维护早于平半判定")

full_body = method_block(S_SRC, "_full_target")
chk("full_at_r" in full_body, "_full_target 用 full_at_r 门槛", repr(full_body[:80]))
chk("half_closed" in full_body, "2.0R 兜底只对已平半的仓位生效",
    "没有 half_closed 判断 → 会在 2R 就把整仓平掉")

print()
print("=" * 78)
print("二、右侧止盈：strict 必须用【原始 K 线】判关键K线")
print("=" * 78)
rbody = method_block(S_SRC, "_right_exit")
chk(bool(rbody), "定位到 _right_exit 函数体")
chk("_is_key_bar(ks," in rbody or "_is_key_bar(ks" in rbody,
    "右侧止盈用 ks（原始K线）调 _is_key_bar")
chk('self._p("right_exit_strict"' in rbody, "right_exit_strict 开关存在")
chk("ha_close" in rbody and "z_now" in rbody, "反向条件 = HA 收盘穿 ZLSMA")
chk("bar_ts" in rbody, "同一根 K 线只判一次（bar_ts 去重）")
chk("half_closed" in rbody, "只在平半之后判（不平半不抢跑）")
chk("_bar_pack" in rbody, "右侧止盈走 _bar_pack（与镜像同一份缓存）")

print()
print("=" * 78)
print("三、已删除的四条：不许在真代码里复活")
print("=" * 78)
chk('pos["max_hold_sec"] and' in strip_comments(E_SRC), "engine 时间止损有 0 值保护",
    "缺这一句 → max_hold_hours=0 时 now-opened_at>=0 恒真，每单开出来就被平")
chk(int(NODE.get("max_hold_hours") or 0) == 0, "max_hold_hours = 0（16h 时间止损已删）",
    "实际 %s" % NODE.get("max_hold_hours"))

# 2026-11-10 二轮清理：这三条从「开关调到 0」升级为「代码 + 配置一起删」，
# 所以判据从「值等于 0」改成「方法不能存在、yaml 不能留键」——
# 留着空方法+空键只是把死代码藏起来，下次谁改 yaml 就能复活一条被否掉的规则。
_dead_methods = [n for n in ("_quick_fail", "_mirror_exit") if method_block(S_SRC, n)]
chk(not _dead_methods, "快速失败 / 镜像出场 方法体已删除", "还在: %s" % _dead_methods)
_dead_keys = [k for k in ("quickfail_hours", "quickfail_min_r", "trail_arm", "trail_dd",
                          "half_tp_r", "mirror_exit", "mirror_bbw_min") if k in P]
chk(not _dead_keys, "yaml 里已删除 7 个死键", "还在: %s" % _dead_keys)

print()
print("=" * 78)
print("四、指标缓存与去重表")
print("=" * 78)
pack = method_block(S_SRC, "_bar_pack")
chk(bool(pack), "定位到 _bar_pack")
chk("_atr_n(ks, 20)" in pack, "_bar_pack 里算 ATR(20)（不是 CE 的 ATR(1)）")
chk("ks" in pack and "atr_n" in pack, "_bar_pack 把 ks / atr_n 一起返回")
chk("_S15_RIGHT_STATE" in S_SRC, "右侧止盈有独立去重表（同一根K线只判一次）")
chk("_S15_MIRROR_STATE" not in S_SRC, "镜像去重表已删除（方法删了，表也删）")

print()
print("=" * 78)
print("五、yaml ↔ 回测 sim() 参数一致性（回测口径必须等于线上）")
print("=" * 78)
i_sim = read("backtest/exit_speed_test.py")
m = re.search(r"def sim\(([\s\S]*?)\):", i_sim)
sig = m.group(1) if m else ""
for pname in ("half_at_r", "full_at_r", "right_exit", "right_exit_strict",
              "mirror_exit", "stop_key", "key_bar", "key_mult", "stop_mode",
              "min_stop_pct", "dev_atr_mult", "swing", "maxh", "qf_bars"):
    chk(pname in sig, "sim() 参数含 %s" % pname, "回测缺这个参数 → 口径对不上")

chk(float(P["half_at_r"]) == 1.2, "half_at_r = 1.2", "实际 %s" % P["half_at_r"])
chk(float(P["full_at_r"]) == 2.0, "full_at_r = 2.0", "实际 %s" % P["full_at_r"])
chk(bool(P["right_exit_strict"]) is True, "right_exit_strict = true")
chk(float(P["swing_lookback"]) == 8, "swing_lookback = 8", "实际 %s" % P["swing_lookback"])
chk(float(P["max_risk_pct"]) == 5.0, "max_risk_pct = 5.0")
chk(float(P["dev_atr_mult"]) == 1.0, "dev_atr_mult = 1.0（偏离闸 = 1.0×ATR20）")
chk(float(P["key_mult"]) == 0.7, "key_mult = 0.7")
# 2026-11-10 定档：池子止损距离闸已整条删除
chk("stop_min_pct" not in MON and "stop_exit_pct" not in MON,
    "池子止损距离闸已删除（两键不该存在）",
    "残留 %s" % [k for k in ("stop_min_pct", "stop_exit_pct") if k in MON])
chk(float(P.get("dual_stop_min_pct") or 0) == 0.5,
    "两档止损分界 = 0.5%", "实际 %s" % P.get("dual_stop_min_pct"))

print()
print("=" * 78)
print("结果：通过 %d 项，失败 %d 项" % (OK, len(BAD)))
if BAD:
    print("失败项：")
    for b in BAD:
        print("  · %s" % b)
print("=" * 78)
sys.exit(1 if BAD else 0)
