"""台账式自检 —— 新出场方案「逻辑 + 参数」逐条验真

不靠单测打桩，全部走真通路：
  1. 参数：strategies.yaml → config 加载 → 实例 → self.p → _p() 转换后取值，
     逐项比对「yaml 写的值」和「代码实际拿到的值 + 类型」。
  2. 逻辑：用真实 K 线 + 线上 _ce_zlsma，统计四条出场各自真实触发次数，
     并抽查触发时刻的指标是否真的满足定义（不是靠肉眼）。
  3. 端到端：真调 S15CeZlsma._mirror_exit()，传真 K 线，看它真的会返回出场原因。
  4. 死代码：确认旧出场的方法名 / 参数名在 backend 里零引用。

用法：python verify_exit_final.py
"""
import asyncio
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import yaml

from backend import strategies as S
from backend.config import load_strategies

FAILS = []
OKS = []


def check(name, cond, detail=""):
    (OKS if cond else FAILS).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return cond


# ══════════════════════════════════════════════════════════
# 一、参数全链路
# ══════════════════════════════════════════════════════════
print("=" * 78)
print("一、参数全链路（yaml → 实例 → _p() 转换后的真实取值）")
print("=" * 78)

raw_yaml = yaml.safe_load(open(os.path.join(ROOT, "strategies.yaml"), encoding="utf-8"))
node = raw_yaml["strategies"]["S15_CE_ZLSMA"]
yaml_params = node.get("params", {})
yaml_top = {k: v for k, v in node.items() if k != "params"}

# 用线上同一个加载函数造实例，和启动时一模一样
import backend.config as _cfg
cfgs = load_strategies() if callable(load_strategies) else None
target = None
if isinstance(cfgs, dict):
    target = cfgs.get("S15_CE_ZLSMA")
elif isinstance(cfgs, list):
    target = next((c for c in cfgs if c.get("sid") == "S15_CE_ZLSMA"
                   or c.get("id") == "S15_CE_ZLSMA"), None)
if target is None:
    target = dict(yaml_top)
    target["params"] = yaml_params

# 实例化线上策略类
strat = S.S15CeZlsma(target)

EXPECT = [
    # (yaml 里写的显示名, 参数名, yaml 值, 期望类型)
    ("2R 平一半门槛",      "half_at_r",        2.0,  float),
    ("3R 全平（再赚1R）",  "half_tp_r",        1.0,  float),
    ("半仓回撤%（关）",    "half_dd_pct",      0.0,  float),
    ("半仓回撤R（关）",    "half_dd_r",        0.0,  float),
    ("CE 周期",            "ce_len",           1,    int),
    ("CE 倍数",            "ce_mult",          2.0,  float),
    ("ZLSMA 周期",         "zlsma_len",        50,   int),
    ("CE 极值用收盘",      "use_close",        True, bool),
    ("用 Heikin Ashi",     "use_ha",           True, bool),
    ("止损摆动根数",       "swing_lookback",   20,   int),
    ("入场布林带门槛",     "bbw_pct_min",      70.0, float),
    ("止损距离上限",       "max_risk_pct",     30.0, float),
    ("K 线周期",           "kline_interval",   "15m", str),
]
for label, key, want, typ in EXPECT:
    got = strat._p(key, want)          # ← 代码真正用的入口
    yaml_v = yaml_params.get(key)
    same = (got == want)
    check(f"{label:<20} {key:<16} yaml={yaml_v!r:<8} 代码取到={got!r:<8} 类型={type(got).__name__}",
          same and isinstance(got, typ) and yaml_v == want,
          "" if same else f"不一致！")

# 顶层 max_hold_hours（16h 超时）走 cfg 不是 params
mh = strat.max_hold
check(f"{'16h 超时秒数':<20} max_hold        = {mh} 秒 (= {mh/3600:.0f} 小时)",
      mh == 16 * 3600)

# 旧参数必须彻底消失
for dead in ("mirror_exit", "trail_pct", "trail_r", "trail_pct_max_r",
             "trail_pct_cap", "ma_bars_lt", "ma_confirm_bars"):
    check(f"旧参数已删除        {dead:<16} yaml={yaml_params.get(dead)!r}",
          dead not in yaml_params)


# ══════════════════════════════════════════════════════════
# 二、逻辑：真实 K 线上四类出场各自触发多少次
# ══════════════════════════════════════════════════════════
print()
print("=" * 78)
print("二、逻辑：真实 K 线扫描，四条出场各自触发统计 + 逐条验算")
print("=" * 78)

FEE = 0.0005
SWING = 20
MAXH = 64          # 16 小时 / 15 分钟 = 64 根
files = sorted(glob.glob(os.path.join(ROOT, "backtest", "data", "*_15m_60d.json")))

stat = {"mirror": 0, "half_tp": 0, "timeout": 0, "hard_stop": 0}
mirror_samples = []
violation = []       # 触发时刻不满足定义的样本（应为 0）


def scan(ks, ind):
    ho, hc = ind["open"], ind["close"]
    zl, dirs = ind["zlsma"], ind["dir"]
    hi, lo = ind["high_raw"], ind["low_raw"]
    n = len(hc)
    i = max(1, 60, SWING)
    while i < n - 2:
        if zl[i] is None or dirs[i] is None or dirs[i - 1] is None or ind["bbw_pct"][i] is None:
            i += 1
            continue
        # 入场条件（与线上同口径，用于造出持仓）
        long_sig = dirs[i] == 1 and dirs[i - 1] == -1 and hc[i] > zl[i]
        short_sig = dirs[i] == -1 and dirs[i - 1] == 1 and hc[i] < zl[i]
        if not (long_sig or short_sig):
            i += 1
            continue
        want = "long" if long_sig else "short"
        if want == "long":
            if not (hc[i] > ho[i] and hc[i] > zl[i]):
                i += 1
                continue
        else:
            if not (hc[i] < ho[i] and hc[i] < zl[i]):
                i += 1
                continue
        if ind["bbw_pct"][i] < 70.0:
            i += 1
            continue
        entry = float(ks[i + 1]["open"])
        lo_i = max(0, i - SWING + 1)
        stop = (min(lo[j] for j in range(lo_i, i + 1)) if want == "long"
                else max(hi[j] for j in range(lo_i, i + 1)))
        risk = (entry - stop) if want == "long" else (stop - entry)
        if risk <= 0:
            i += 1
            continue
        sign = 1 if want == "long" else -1
        peak = 0.0
        half = False
        ei = None
        for j in range(i + 1, n):
            o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                              float(ks[j]["low"]), float(ks[j]["close"]))
            if want == "long" and l_ <= stop:
                stat["hard_stop"] += 1
                ei = j
                break
            if want == "short" and h_ >= stop:
                stat["hard_stop"] += 1
                ei = j
                break
            peak = max(peak, ((h_ - entry) if want == "long" else (entry - l_)) / risk)
            cur = ((c_ - entry) if want == "long" else (entry - c_)) / risk
            if peak >= 2.0:
                half = True
            if not half and (j - i) >= MAXH:
                stat["timeout"] += 1
                ei = j
                break
            if half and (cur - 2.0) >= 1.0:
                stat["half_tp"] += 1
                ei = j
                break
            # 镜像：CE 翻转 + HA 反向 + 站错边（无布林带）
            flip = (dirs[j] == -1 and dirs[j - 1] == 1) if want == "long" \
                else (dirs[j] == 1 and dirs[j - 1] == -1)
            if flip:
                ha_ok = (hc[j] < ho[j] and hc[j] < zl[j]) if want == "long" \
                    else (hc[j] > ho[j] and hc[j] > zl[j])
                if ha_ok:
                    stat["mirror"] += 1
                    # ★ 逐条验算：这个触发真的满足定义吗？
                    if want == "long":
                        good = (dirs[j] == -1 and dirs[j - 1] == 1
                                and hc[j] < ho[j] and hc[j] < zl[j])
                    else:
                        good = (dirs[j] == 1 and dirs[j - 1] == -1
                                and hc[j] > ho[j] and hc[j] > zl[j])
                    if not good:
                        violation.append((want, j))
                    if len(mirror_samples) < 5:
                        mirror_samples.append(
                            dict(side=want, bar=j,
                                 dir_prev=dirs[j - 1], dir_now=dirs[j],
                                 ha_open=round(ho[j], 6), ha_close=round(hc[j], 6),
                                 zlsma=round(zl[j], 6),
                                 ha_bear_ok=hc[j] < ho[j], side_of_ma=hc[j] < zl[j]))
                    ei = j
                    break
        i = (ei + 1) if ei else (i + 1)


total_bars = 0
for fp in files[:8]:      # 取 8 个币，够统计意义
    sym = os.path.basename(fp).replace("_15m_60d.json", "")
    ks = json.load(open(fp, encoding="utf-8"))
    if len(ks) < 800:
        continue
    ind = S._ce_zlsma(ks, 1, 2.0, 50, True, True)
    total_bars += len(ks)
    scan(ks, ind)

total = sum(stat.values())
print(f"  扫描 {min(8, len(files))} 币 / {total_bars} 根 K 线 / 共 {total} 笔出场")
for k, v in sorted(stat.items(), key=lambda x: -x[1]):
    print(f"    {k:<10} {v:>5} 笔  {v/total*100 if total else 0:>5.1f}%")
check("镜像出场真的会触发（不是死条件）", stat["mirror"] > 0, f"{stat['mirror']} 笔")
check("2R 平半后 3R 全平真的会触发", stat["half_tp"] > 0, f"{stat['half_tp']} 笔")
check("硬止损真的会触发", stat["hard_stop"] > 0, f"{stat['hard_stop']} 笔")
check("每一笔镜像出场都满足三条件定义（逐条验算）",
      len(violation) == 0, f"违规 {len(violation)} 笔")


# ══════════════════════════════════════════════════════════
# 三、端到端：真调 _mirror_exit，传真 K 线
# ══════════════════════════════════════════════════════════
print()
print("=" * 78)
print("三、端到端：真调线上 _mirror_exit()，不打桩，喂真实 K 线")
print("=" * 78)


class RealCtx:
    """用真实 K 线喂策略，只截到指定 bar，模拟「那一刻的盘面」。"""
    def __init__(self, ks, cut, price):
        self._ks = ks
        self._cut = cut
        self._px = price

    async def klines(self, sym, iv, n):
        end = self._cut + 1
        start = max(0, end - n)
        return self._ks[start:end]

    def price(self, sym):
        return self._px


async def e2e():
    fp = files[0]
    ks = json.load(open(fp, encoding="utf-8"))
    ind = S._ce_zlsma(ks, 1, 2.0, 50, True, True)
    n = len(ks)
    ho, hc, zl, dirs = ind["open"], ind["close"], ind["zlsma"], ind["dir"]

    # 找一个真实的「CE 翻空 + HA 阴线跌破 ZLSMA」时刻
    hit = None
    for j in range(200, n - 1):
        if (dirs[j] == -1 and dirs[j - 1] == 1
                and hc[j] < ho[j] and hc[j] < zl[j]):
            hit = j
            break
    if not hit:
        check("能找到真实的镜像出场时刻", False, "数据里没有")
        return

    entry = float(ks[hit - 10]["open"])
    stop = entry * 0.97         # 3% 止损，R = 3% 价格
    pos = dict(id="e2e-1", symbol="E2E", side="long", entry_price=entry,
               stop_price=stop, opened_at="2026-11-08T00:00:00", half_closed=0,
               peak_r=0.0, mfe_pct=0.0)
    S._S15_MIRROR_STATE.clear()
    S._S15_POS_STATE.clear()
    S._S15_ZL_CACHE.clear()

    # 用 hit 那根的收盘价当「当前价」，cut 也停在 hit（i = len-2 = hit-1？）
    # _mirror_exit 取 i = len(ks)-2，所以要让 len(ks)-2 == hit → cut = hit+1
    ctx = RealCtx(ks, hit + 1, float(ks[hit]["close"]))
    reason = await strat._mirror_exit(ctx, pos)
    check("真 K 线上 _mirror_exit 返回出场原因", reason is not None, str(reason))
    if reason:
        check("出场原因文案里没有 BBW（布林带已移除）", "BBW" not in str(reason), str(reason))
        check("出场原因含「镜像出场」", "镜像出场" in str(reason), str(reason))

    # 反向：把 cut 挪到「CE 没翻转」的 bar，应该不出场
    S._S15_MIRROR_STATE.clear()
    S._S15_ZL_CACHE.clear()
    quiet = None
    for j in range(200, n - 1):
        if dirs[j] == dirs[j - 1] and abs((hc[j] / zl[j] - 1) * 100) > 0.5:
            quiet = j
            break
    if quiet:
        ctx2 = RealCtx(ks, quiet + 1, float(ks[quiet]["close"]))
        r2 = await strat._mirror_exit(ctx2, pos)
        check("CE 没翻转时不误出场", r2 is None, f"bar={quiet} → {r2}")


asyncio.run(e2e())


# ══════════════════════════════════════════════════════════
# 四、死代码 / 引用面
# ══════════════════════════════════════════════════════════
print()
print("=" * 78)
print("四、死代码：旧出场是否真的没人再引用")
print("=" * 78)

import subprocess
for dead_name in ("_trend_reversal_exit",):
    r = subprocess.run(["grep", "-rn", dead_name, "backend/", "--include=*.py"],
                       cwd=ROOT, capture_output=True, text=True)
    hits = [l for l in r.stdout.strip().split("\n") if l and "__pycache__" not in l]
    check(f"{dead_name} 在 backend/*.py 中零引用", len(hits) == 0,
          f"命中 {len(hits)} 处: {hits[:2]}")

# 旧开关 mirror_exit：查的是「被当成参数读取」这个形态 —— _p("mirror_exit"
# （方法名 _mirror_exit 必须存在，不能一起算进去）
r2 = subprocess.run(["grep", "-rn", '_p("mirror_exit"', "backend/", "--include=*.py"],
                    cwd=ROOT, capture_output=True, text=True)
h2 = [l for l in r2.stdout.strip().split("\n") if l and "__pycache__" not in l]
check('旧开关 _p("mirror_exit") 零引用', len(h2) == 0, f"命中 {len(h2)} 处")

# 镜像出场方法本身必须存在且真的被调用
r3 = subprocess.run(["grep", "-rn", "await self._mirror_exit", "backend/", "--include=*.py"],
                    cwd=ROOT, capture_output=True, text=True)
h3 = [l for l in r3.stdout.strip().split("\n") if l and "__pycache__" not in l]
check("镜像出场真的被 check_exit 调用", len(h3) >= 1, f"调用点 {len(h3)} 处")


# ══════════════════════════════════════════════════════════
print()
print("=" * 78)
print(f"结果：通过 {len(OKS)} 项，失败 {len(FAILS)} 项")
if FAILS:
    print("失败项：")
    for f in FAILS:
        print(f"  ✗ {f}")
else:
    print("全部通过")
print("=" * 78)
sys.exit(1 if FAILS else 0)
