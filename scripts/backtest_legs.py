# -*- coding: utf-8 -*-
"""S15 全市场回测 v2 —— 入场直接调线上 evaluate()，一行判据都不重写。

★ v2 相对 v1 的两个本质改动（2026-11-11 深夜，为验「反手」而做）：

  1) 逐根推进，不再「进场→一口气算到出场→跳过整段持仓」。
     v1 那样做的话，持仓期间 evaluate() 从来没被调用过，
     ctx.pos_side 永远是 None → reverse 信号永远发不出来 → 反手测了个寂寞。

  2) 支持反手（reverse=True）。
     持仓期间照常每根调 evaluate()；一旦回 reverse 信号，
     按「同一根开盘价」平旧仓 + 开新仓（只付手续费，不吃跳空）。

  出场判定顺序严格照抄线上定档（与 v1 完全一致，一个字没动）：
     ① 硬止损（保本后=入场价） → 保本移损 1.5R → 平一半 1.2R
     → 兜底全平 2.0R → 右侧止盈（HA反向+收穿ZLSMA+反向关键K线）

用法：
  python _backtest_legs.py                 # 全 80 币
  python _backtest_legs.py --quick         # 前 20 币
  python _backtest_legs.py --n=20          # 指定币数
  python _backtest_legs.py --from=20 --n=20
  python _backtest_legs.py --no-reverse    # 关掉反手（做 A/B 对照）
"""
import asyncio
import glob
import io as _io
import json
import os
import sys

ROOT = r"E:\quant-bot"
sys.path.insert(0, ROOT)

import yaml  # noqa: E402
from backend import strategies as S  # noqa: E402

DATA = os.path.join(ROOT, "backtest", "data80")
# --data=DIR ：换数据集（默认 data80 不变）。data80 里混了 51 个美股代币，
# 实盘根本不交易那些，所以「纯加密币口径」必须用 --data=data80coin（158 币）。
for _a in sys.argv[1:]:
    if _a.startswith("--data="):
        _d = _a.split("=", 1)[1].strip()
        DATA = _d if os.path.isabs(_d) else os.path.join(ROOT, "backtest", _d)
FEE = 0.0005
# --fee=0.0002 ：换手续费率（默认 0.0005 不变）。用来测算 maker 限价单情形。
for _a in sys.argv[1:]:
    if _a.startswith("--fee="):
        FEE = float(_a.split("=", 1)[1].strip())

# -- 从 yaml 读参数（永远跟线上一致）------------------------------
with _io.open(os.path.join(ROOT, "strategies.yaml"), encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)
_STRAT = _cfg["strategies"]["S15_CE_ZLSMA"]
_P = dict(_STRAT.get("params", {}))
_P.setdefault("kline_interval", "15m")

HALF_AT_R = float(_P.get("half_at_r", 1.2))
FULL_AT_R = float(_P.get("full_at_r", 2.0))
BE_AT_R = float(_P.get("be_at_r", 1.5))


def _arg_f(name, default):
    """从命令行取 float 参数：--be=0.8 之类"""
    for a in sys.argv[1:]:
        if a.startswith("--" + name + "="):
            return float(a.split("=", 1)[1])
    return default


# ── 三个实验开关（只影响回测模拟，生产代码不动）──────────────
# be      : 保本线（多少 R 时把止损移到入场价）
# trail   : 保本之后改跟踪止损，从峰值回撤多少 R 就出场（0 = 关闭，仍钉死入场价）
# timegate: 持仓超过多少根 K 线强制按现价平（0 = 关闭）
BE_AT_R = _arg_f("be", BE_AT_R)
FULL_AT_R = _arg_f("full", FULL_AT_R)     # 全平线（治「右尾被砍」的反向验证）
HALF_AT_R = _arg_f("half", HALF_AT_R)     # 平半线（治「右尾被砍」：推后让大赢单跑）
TRAIL_R = _arg_f("trail", 0.0)
TIME_GATE = int(_arg_f("timegate", 0))
# ★ --eff=X：进场趋势效率门槛。（线上 0.30；文档里的 0.55 只是「单边」分类线，从来不是门槛）
_P["eff_min_signal"] = _arg_f("eff", float(_P.get("eff_min_signal", 0.30)))
# ★ 快速失败：进场后 N 根内若逆行超过 X×R 就砍（数据：MAE>0.9R 的单 84.3% 亏损，合计 -604.8R）
FASTFAIL = _arg_f("ff", 0.0)          # 0 = 关（默认，与线上一致）
FASTFAIL_BARS = int(_arg_f("ffbars", 2))
# --dump=path：把每笔明细导成 CSV（只写不读，不参与任何计算）
DUMP_PATH = None
for _a in sys.argv[1:]:
    if _a.startswith("--dump="):
        DUMP_PATH = _a.split("=", 1)[1]
RIGHT_EXIT = bool(_P.get("right_exit", True))
# ★ VS15-1.5 第④条默认开（与线上一致）；--notight 关掉，用于 A/B 对照。
TIGHT_ON = "--notight" not in sys.argv
FLIP_EARLY = "--flip" in sys.argv
# ★ 默认关，与线上 flip_exit_early=0 一致：
#   什么都不加 = 现网行为（右侧止盈只判平半之后）；
#   --flip                = 信号翻转提前到平半前（80币60天 +82.51R vs 现网 +166.35R，已否决）
#   --flip --flip-losing-only = 只砍亏损中的翻转（+78.27R，更差）
FLIP_LOSING_ONLY = FLIP_EARLY and "--flip-losing-only" in sys.argv
KEY_MULT = float(_P.get("key_mult", 1.0))
KEY_CP = float(_P.get("key_closepos", 0.66))
KEY_BF = float(_P.get("key_bodyfrac", 0.5))
Z_LEN = int(_P.get("zlsma_len", 50))
CE_LEN = int(_P.get("ce_len", 1))
CE_MULT = float(_P.get("ce_mult", 2.0))

# 反手开关：默认读 yaml（现在是 false），命令行 --reverse / --no-reverse 可强制覆盖
_CFG_REV = bool(_P.get("reverse_enabled", False))
if "--reverse" in sys.argv:
    ALLOW_REVERSE = True
elif "--no-reverse" in sys.argv:
    ALLOW_REVERSE = False
else:
    ALLOW_REVERSE = _CFG_REV

# 指标函数真身（每只币预计算完之后会被换成前缀切片，处理完必须换回来）
_ORIG_CE = S._ce_zlsma
_ORIG_ATR = S._atr_n
_ORIG_LEGS = S._zlsma_legs

IS_NEW = hasattr(S, "_zlsma_legs")
MODE = "新口径(ZLSMA时段)" if IS_NEW else "旧口径(CE翻转根)"

print("=" * 96)
print("S15 全市场回测 v2（逐根推进）  模式 = %s" % MODE)
print("  ce_len=%s ce_mult=%s zlsma_len=%s key_mult=%s" % (CE_LEN, CE_MULT, Z_LEN, KEY_MULT))
print("  出场: 硬止损 / %.1fR保本 / %.1fR平半 / %.1fR全平 / 右侧止盈=%s"
      % (BE_AT_R, HALF_AT_R, FULL_AT_R, RIGHT_EXIT))
print("  偏离闸: min_dev=%s dev_atr_mult=%s" % (_P.get("min_dev_pct"), _P.get("dev_atr_mult")))
print("  反手: %s" % ("开" if ALLOW_REVERSE else "关"))
_extra = []
if BE_AT_R != float(_P.get("be_at_r", 1.5)):
    _extra.append("保本线 %.2fR" % BE_AT_R)
if FULL_AT_R != float(_P.get("full_at_r", 2.0)):
    _extra.append("全平线 %.2fR" % FULL_AT_R)
if TRAIL_R > 0:
    _extra.append("跟踪止损 回撤%.2fR" % TRAIL_R)
if TIME_GATE > 0:
    _extra.append("时间闸 %d根(%.1fh)" % (TIME_GATE, TIME_GATE * 0.25))
print("  实验项: %s" % ("、".join(_extra) if _extra else "无（基线）"))
print("=" * 96)


class MockCtx:
    """把历史 K 线切片喂给 evaluate()，让线上代码以为自己在跑实盘。

    ★ pos_side 必须真实反映回测里的持仓方向 —— 否则 evaluate 判「无仓」，
      反手信号永远发不出来。
    """

    def __init__(self, ks, sym):
        self.ks = ks
        self.sym = sym
        self.i = 0
        self.universe = [sym]
        self.monitor = [sym]
        self.pos_side = None

    async def klines(self, sym, iv, need):
        # ★ 不截断：必须给全长前缀，这样下面 install_fast_indicators 的
        #   「预计算序列前缀切片」才和逐根重算严格等价（实测 180 根后 0 差异）
        return self.ks[: self.i + 2]

    def has_position(self, sym, sid):
        return self.pos_side is not None

    def open_side(self, sym, sid=None):
        return self.pos_side

    def price(self, sym):
        j = self.i + 1
        return float(self.ks[j]["close"]) if j < len(self.ks) else None


def install_fast_indicators(ind_full, atr_full, legs_full):
    """把三个指标函数换成「预计算序列的前缀切片」。

    为什么必须这么干：v2 改成逐根推进后，evaluate() 每根都会重算
    _ce_zlsma / _atr_n / _zlsma_legs，全是 O(n) —— 6060 根就是 O(n^2)，
    20 币会直接跑到超时（v1 就是这么死的）。

    等价性已实测（compare 脚本）：180 根之后 zlsma/bbw/bbw_pct/atr/atr20/leg_dir
    逐根对比，**0 处差异**。所以缓存不改变任何结果，只是把 O(n^2) 压回 O(n)。
    """
    def fast_ce(k_, *a, **kw):
        L = len(k_)
        return {kk: (vv[:L] if isinstance(vv, list) else vv)
                for kk, vv in ind_full.items()}

    def fast_atr(k_, n=20):
        return atr_full[:len(k_)]

    def fast_legs(k_, ind, max_wait=3):
        L = len(k_)
        return (legs_full[0][:L], legs_full[1][:L], legs_full[2][:L], legs_full[3])

    return fast_ce, fast_atr, fast_legs


def make_pos(entry_bar, side, entry, stop, tight=0.0):
    return dict(side=side, entry=entry, stop=stop,
                risk=abs(entry - stop),
                peak=0.0, mae=0.0,          # peak=MFE（最大浮盈R） mae=最大浮亏R
                half_done=False, be_on=False, half_part=0.0,
                entry_bar=entry_bar,
                # VS15-1.5 第④条：收紧价 + 是否已武装（CE 翻转后才武装）
                tight=float(tight or 0.0), tight_on=False)


def _flip_signal(ks, ind, atr_n, j, side):
    """信号翻转判定：HA 反向 + HA收穿 ZLSMA50 + 那根原始K线是反向关键K线。

    与线上 _right_exit 同一套口径。zl 前 50 根是 None（预热），必须防。
    """
    zl, hc, ho = ind["zlsma"], ind["close"], ind["open"]
    if j >= len(zl) or j >= len(hc) or j >= len(ho):
        return False
    _z = zl[j]
    if _z is None or _z <= 0:
        return False
    rev_ha = ((hc[j] < ho[j] and hc[j] < _z) if side == "long"
              else (hc[j] > ho[j] and hc[j] > _z))
    if not rev_ha:
        return False
    return bool(S._is_key_bar(ks, j, atr_n, "short" if side == "long" else "long",
                              KEY_MULT, KEY_CP, KEY_BF))


def step_exit(ks, ind, atr_n, j, pos):
    """判第 j 根是否触发出场。命中返回 (exit_px, reason)，否则 None。

    判定顺序与线上 / v1 完全一致，原地更新 pos 的 peak / be_on / half_done。
    """
    side, entry, risk = pos["side"], pos["entry"], pos["risk"]
    sign = 1.0 if side == "long" else -1.0
    zl, hc, ho = ind["zlsma"], ind["close"], ind["open"]
    o_, h_, l_, c_ = (float(ks[j]["open"]), float(ks[j]["high"]),
                      float(ks[j]["low"]), float(ks[j]["close"]))
    # VS15-1.5 第④条：已武装 → 有效止损改用收紧价（它永远比原止损更紧或相等）
    eff_stop = entry if pos["be_on"] else (pos["tight"] if pos["tight_on"] else pos["stop"])

    # 跟踪止损：保本之后，止损跟着峰值往上走（不钉死入场价）
    if pos["be_on"] and TRAIL_R > 0:
        if side == "long":
            eff_stop = max(eff_stop, entry + (pos["peak"] - TRAIL_R) * risk)
        else:
            eff_stop = min(eff_stop, entry - (pos["peak"] - TRAIL_R) * risk)
    _stop_tag = "trail_stop" if (pos["be_on"] and TRAIL_R > 0) else ("be_stop" if pos["be_on"] else "hard_stop")

    # 硬止损（保本后止损 = 入场价 / 跟踪线）
    if side == "long" and l_ <= eff_stop:
        return min(o_, eff_stop), _stop_tag
    if side == "short" and h_ >= eff_stop:
        return max(o_, eff_stop), _stop_tag

    # ★ 快速失败（--ff=X --ffbars=N）：进场后 N 根内逆行超 X×R 就砍。
    #   依据：MAE > 0.9R 的单 84.3% 亏损、合计 -604.8R；MAE < 0.3R 的只亏 9.6%。
    #   默认 FASTFAIL=0 关闭，基线逐值不变。
    if FASTFAIL > 0 and not pos["half_done"] \
            and (j - pos["entry_bar"]) <= FASTFAIL_BARS:
        _ff_line = entry - FASTFAIL * risk * sign
        if (sign > 0 and l_ <= _ff_line) or (sign < 0 and h_ >= _ff_line):
            return _ff_line, "fast_fail"

    # ★ 2026-11-13 新增：信号翻转 → 全平（独立出场条件，平半前也判）
    #   FLIP_EARLY 开（默认）= 新逻辑，位置在硬止损之后、1.2R 平半之前。
    #   FLIP_EARLY 关（--no-flip）= 老逻辑，仍留在最后（兜底全平之后），保证基线逐值一致。
    if FLIP_EARLY and RIGHT_EXIT and _flip_signal(ks, ind, atr_n, j, side):
        if pos["half_done"]:
            return c_, "right_exit"
        # 平半前：--flip-losing-only 只认「当前还在亏」的翻转，保住赚钱中的单
        _cur_now = ((c_ - entry) if sign > 0 else (entry - c_)) / risk
        if not FLIP_LOSING_ONLY or _cur_now < 0:
            return c_, "signal_flip"

    # 时间闸：持够 N 根还没走完，按现价平（治「慢」）
    if TIME_GATE > 0 and (j - pos["entry_bar"]) >= TIME_GATE:
        return c_, "time_exit"

    # 峰顶 / 谷底（诊断用：MFE 和 MAE —— 「离场干不干净」就看这两个数）
    pos["peak"] = max(pos["peak"], ((h_ - entry) if sign > 0 else (entry - l_)) / risk)
    pos["mae"] = min(pos["mae"], ((l_ - entry) if sign > 0 else (entry - h_)) / risk)
    cur = ((c_ - entry) if sign > 0 else (entry - c_)) / risk

    # ★ VS15-1.5 第④条：CE 翻转 + 峰顶还没到 be_at_r → 止损收紧到 pos["tight"]
    #   线上在 check_exit 里每 tick 判（两套时钟：CE 方向看已收完的 K 线，
    #   价格触碰看 0.5 秒实时价）。这里按 bar 推进，所以「本根收盘确认翻转」后
    #   立刻用本根的 low/high 判一次触碰 —— 线上最多滞后 0.5 秒，同样会在
    #   这根 K 线内被扫到，这样模拟不会偏乐观。
    #   ★ 只有开仓时 CE 与持仓同向的单才带 tight（生成信号时就裁掉了反向的），
    #     所以 tight_on 一旦触发，必然代表「持仓期间真的发生了翻转」。
    if (pos["tight"] > 0 and not pos["tight_on"] and not pos["be_on"]
            and pos["peak"] < BE_AT_R - 1e-9):
        _w = 1 if side == "long" else -1
        _d = int(ind["dir"][j] or 0) if j < len(ind["dir"]) else 0
        if _d != 0 and _d != _w:
            pos["tight_on"] = True
            _t = pos["tight"]
            if sign > 0 and l_ <= _t:
                return min(o_, _t), "tight_stop"
            if sign < 0 and h_ >= _t:
                return max(o_, _t), "tight_stop"

    # 保本移损
    if BE_AT_R > 0 and not pos["be_on"] and pos["peak"] >= BE_AT_R - 1e-9:
        pos["be_on"] = True

    # 平一半
    if not pos["half_done"] and pos["peak"] >= HALF_AT_R - 1e-9:
        pos["half_done"] = True
        pos["half_part"] = HALF_AT_R
        return None

    # 兜底全平
    if pos["half_done"] and cur >= FULL_AT_R - 1e-9:
        return c_, "full_target"

    # 右侧止盈（老逻辑位置：只判平半之后）—— --no-flip 时走这里
    if not FLIP_EARLY and RIGHT_EXIT and pos["half_done"] \
            and _flip_signal(ks, ind, atr_n, j, side):
        return c_, "right_exit"
    return None


def finish(pos, exit_px, bars, reason, entry_ts=0):
    """把一笔结单算成 R。已平半的按 1.2R + 剩余半仓结算。"""
    entry, risk = pos["entry"], pos["risk"]
    sign = 1.0 if pos["side"] == "long" else -1.0
    gross = (exit_px - entry) * sign
    r = (gross - (entry + exit_px) * FEE) / risk
    if pos["half_done"]:
        r = 0.5 * pos["half_part"] + 0.5 * r
    return dict(side=pos["side"], r=r, bars=bars, reason=reason,
                mfe=pos["peak"], mae=pos["mae"],
                entry=entry, stop=pos["stop"], exit_px=exit_px,
                entry_ts=int(entry_ts or 0),
                stop_dist=abs(entry - pos["stop"]) / entry * 100 if entry else 0,
                half_done=1 if pos["half_done"] else 0,
                be_on=1 if pos["be_on"] else 0)


def stats(trades):
    if not trades:
        return dict(n=0, win=0.0, pl=0.0, r=0.0, mdd=0.0, bars=0.0)
    n = len(trades)
    r_sum = sum(t["r"] for t in trades)
    wins = [t["r"] for t in trades if t["r"] > 0]
    losses = [t["r"] for t in trades if t["r"] <= 0]
    win = len(wins) / n * 100
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = abs(sum(losses) / len(losses)) if losses else 0.0
    pl = (avg_w / avg_l) if avg_l > 0 else 0.0
    cum = pk = mdd = 0.0
    for t in sorted(trades, key=lambda z: z["exit_ts"]):
        cum += t["r"]
        pk = max(pk, cum)
        mdd = min(mdd, cum - pk)
    return dict(n=n, win=win, pl=pl, r=r_sum, mdd=mdd,
                bars=sum(t["bars"] for t in trades) / n)


async def run(quick=False, ncoins=0, start_at=0):
    files = sorted(glob.glob(os.path.join(DATA, "*_15m_60d.json")))
    if quick:
        files = files[:20]
    elif ncoins > 0:
        files = files[start_at:start_at + ncoins]
    elif start_at > 0:
        files = files[start_at:]
    if not files:
        print("[X] 没有数据文件:", DATA)
        return None

    trades = []
    skipped = 0
    n_rev = 0
    for fp in files:
        sym = os.path.basename(fp).replace("_15m_60d.json", "")
        with _io.open(fp, encoding="utf-8") as f:
            ks = json.load(f)
        if len(ks) < 900:
            skipped += 1
            continue
        cfg = {"_iid": "S15_CE_ZLSMA", "params": dict(_P),
               "symbols": [sym], "max_hold_hours": 0}
        inst = S.S15CeZlsma(cfg)
        ctx = MockCtx(ks, sym)
        # 指标每个币只算一次，然后把函数换成「前缀切片」（见 install_fast_indicators）
        ind_full = _ORIG_CE(ks, CE_LEN, CE_MULT, Z_LEN, True, True)
        atr_n = _ORIG_ATR(ks, 20)
        legs_full = _ORIG_LEGS(ks, ind_full, 3)
        S._ce_zlsma, S._atr_n, S._zlsma_legs = install_fast_indicators(
            ind_full, atr_full=atr_n, legs_full=legs_full)
        S._LEG_ENTERED.clear()

        start = max(Z_LEN * 2 + 80, 120)
        bar = start
        pos = None
        while bar < len(ks) - 2:
            ctx.i = bar
            ctx.pos_side = pos["side"] if pos else None

            # 出场优先（线上：manage_positions 每 0.5s，scan_loop 每 60s）
            if pos is not None and bar > pos["entry_bar"]:
                hit = step_exit(ks, ind_full, atr_n, bar, pos)
                if hit:
                    px, why = hit
                    t = finish(pos, px, bar - pos["entry_bar"], why,
                               int(ks[pos["entry_bar"]].get("ts", 0)))
                    t["sym"] = sym
                    t["exit_ts"] = int(ks[bar].get("ts", bar))
                    trades.append(t)
                    pos = None
                    bar += 1
                    continue

            # 入场判定（有仓时也调，为了拿 reverse 信号）
            try:
                sigs = await inst.evaluate(ctx)
            except Exception:                      # noqa: BLE001
                bar += 1
                continue
            if not sigs:
                bar += 1
                continue
            sig = sigs[0]

            if pos is not None:
                # 同向的 evaluate 已经跳过，走到这里必然是 reverse
                if not (ALLOW_REVERSE and getattr(sig, "reverse", False)):
                    bar += 1
                    continue
                # 反手：同一根开盘价平旧仓（只吃手续费，不吃跳空）
                swap_px = float(ks[bar + 1]["open"])
                t = finish(pos, swap_px, bar - pos["entry_bar"], "reverse",
                           int(ks[pos["entry_bar"]].get("ts", 0)))
                t["sym"] = sym
                t["exit_ts"] = int(ks[bar + 1].get("ts", bar))
                trades.append(t)
                n_rev += 1
                pos = None

            entry = float(ks[bar + 1]["open"])
            if entry <= 0 or float(sig.stop_price) <= 0:
                bar += 1
                continue
            pos = make_pos(bar, sig.side, entry, float(sig.stop_price),
                           float(getattr(sig, "stop_tight", 0.0) or 0.0) if TIGHT_ON else 0.0)
            bar += 1

        # 数据末尾还有仓 → 按最后收盘价结掉
        if pos is not None:
            t = finish(pos, float(ks[-1]["close"]), len(ks) - 1 - pos["entry_bar"], "eod",
                       int(ks[pos["entry_bar"]].get("ts", 0)))
            t["sym"] = sym
            t["exit_ts"] = int(ks[-1].get("ts", len(ks) - 1))
            trades.append(t)

        # 恢复真身，避免影响下一只币的预计算
        S._ce_zlsma, S._atr_n, S._zlsma_legs = _ORIG_CE, _ORIG_ATR, _ORIG_LEGS

    st = stats(trades)
    days = 60.0

    # ── 明细导出（--dump=path）：每笔一行，用于「什么样的单会亏」的归因分析 ──
    if DUMP_PATH:
        import csv as _csv
        _cols = ["sym", "side", "entry_ts", "exit_ts", "entry", "stop", "exit_px",
                 "stop_dist", "mfe", "mae", "r", "reason", "bars",
                 "half_done", "be_on"]
        try:
            with open(DUMP_PATH, "w", encoding="utf-8", newline="") as _f:
                _w = _csv.DictWriter(_f, fieldnames=_cols, extrasaction="ignore")
                _w.writeheader()
                for _t in trades:
                    _w.writerow({k: _t.get(k, "") for k in _cols})
            print("\n  明细已导出: %s（%d 行）" % (DUMP_PATH, len(trades)))
        except Exception as _e:  # noqa: BLE001
            print("\n  [!] 明细导出失败: %s" % _e)
    print("")
    print("=" * 96)
    print("结果：%d 币（跳过 %d 个数据不足）  反手 %d 笔" % (len(files) - skipped, skipped, n_rev))
    print("=" * 96)
    print("  单量      : %d 笔   (%.2f 单/天)" % (st["n"], st["n"] / days if days else 0))
    print("  胜率      : %.1f%%" % st["win"])
    print("  盈亏比    : %.2f" % st["pl"])
    print("  净值      : %+.2fR" % st["r"])
    print("  最大回撤  : %.2fR" % st["mdd"])
    print("  均持时间  : %.2f 小时 (%.1f 根)" % (st["bars"] * 0.25, st["bars"]))
    print("")
    # ── 离场质量诊断（浩哥 2026-11-11 深夜要求「更干净更快」）
    if trades:
        print("")
        print("  ── 按出场原因拆解 ──")
        print("  %-13s %6s %8s %8s %8s %8s" % ("原因", "笔数", "均R", "合计R", "MFE均", "持时h"))
        by = {}
        for t in trades:
            by.setdefault(t["reason"], []).append(t)
        for why in sorted(by, key=lambda k: -len(by[k])):
            g = by[why]
            n_ = len(g)
            print("  %-13s %6d %+8.3f %+8.1f %8.2f %8.2f" % (
                why, n_, sum(x["r"] for x in g) / n_, sum(x["r"] for x in g),
                sum(x.get("mfe", 0) for x in g) / n_,
                sum(x["bars"] for x in g) * 0.25 / n_))

        # ★ 回吐单：浮盈曾经 >= 1R，最后却是亏的 —— 这就是「不干净」
        spill = [t for t in trades if t.get("mfe", 0) >= 1.0 and t["r"] <= 0]
        spill_r = sum(t["r"] for t in spill)
        print("")
        print("  ── 回吐诊断 ──")
        print("  浮盈曾≥1R 却最终亏损 : %d 笔 (%.1f%%)  合计 %+.1fR"
              % (len(spill), len(spill) / len(trades) * 100, spill_r))
        if spill:
            print("  这些单的 MFE 均 : %.2fR   实际出场均 : %+.2fR   回吐 %.2fR/笔"
                  % (sum(t["mfe"] for t in spill) / len(spill),
                     sum(t["r"] for t in spill) / len(spill),
                     sum(t["mfe"] - t["r"] for t in spill) / len(spill)))
        # 浮盈 ≥0.5R 却亏损
        spill2 = [t for t in trades if t.get("mfe", 0) >= 0.5 and t["r"] <= 0]
        print("  浮盈曾≥0.5R 却最终亏损: %d 笔 (%.1f%%)  合计 %+.1fR"
              % (len(spill2), len(spill2) / len(trades) * 100,
                 sum(t["r"] for t in spill2)))
        # ★ MFE 分布：决定「保护线该设在哪」
        print("")
        print("  ── 全部单的 MFE（最大浮盈）分布 ──")
        buckets = [(0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0),
                   (1.0, 1.2), (1.2, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 99)]
        for lo, hi in buckets:
            g = [t for t in trades if lo <= t.get("mfe", 0) < hi]
            if not g:
                continue
            loss = sum(1 for t in g if t["r"] <= 0)
            print("    MFE %4.2f~%-4.2f : %4d 笔  其中亏损 %3d 笔(%3.0f%%)  合计R %+8.1f"
                  % (lo, hi, len(g), loss, loss / len(g) * 100, sum(t["r"] for t in g)))

        # 硬止损单里，浮盈曾经到过多少
        hs = by.get("hard_stop", [])
        if hs:
            print("  硬止损单 %d 笔：MFE均 %.2fR，其中 MFE≥0.5R 的 %d 笔（%d%%）"
                  % (len(hs), sum(t["mfe"] for t in hs) / len(hs),
                     sum(1 for t in hs if t["mfe"] >= 0.5),
                     round(sum(1 for t in hs if t["mfe"] >= 0.5) / len(hs) * 100)))

    from collections import Counter
    c = Counter(t["reason"] for t in trades)
    print("")
    print("  出场原因分布:", dict(c))
    if st["mdd"] < 0:
        print("  净值/回撤 : %.2f" % (abs(st["r"] / st["mdd"])))
    print("=" * 96)
    return st


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    n = 0
    s = 0
    for a in sys.argv[1:]:
        if a.startswith("--n="):
            n = int(a.split("=")[1])
        elif a.startswith("--from="):
            s = int(a.split("=")[1])
    asyncio.run(run(quick, n, s))
