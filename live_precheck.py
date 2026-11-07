"""接实盘前置体检 —— 币安账户状态一键自查。

用法（在 E:\\quant-bot 下，用项目自带的 Python）：
    .venv\\Scripts\\python.exe live_precheck.py          # 只查，不改任何东西
    .venv\\Scripts\\python.exe live_precheck.py --fix    # 查 + 关掉联保模式（唯一会动的）

不需要真的下单，纯读状态。跑完对着 [OK] / [!!] 看就行。

为什么要做这个：浩哥 2026-11-07 定「接实盘必须逐仓 isolated，否则一个币爆仓
带崩整个账户」。但币安有个坑 —— 联保模式（多资产模式）下不支持逐仓，而联保
是账户级开关，代码查得到、也应该在启动时拦住。
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from backend import binance, config  # noqa: E402

OK = "[OK]"
BAD = "[!!]"
INFO = "[..]"

_problems: list[str] = []


def line(tag: str, msg: str) -> None:
    print(f"{tag} {msg}")
    if tag == BAD:
        _problems.append(msg)


async def main(fix: bool) -> int:
    print("=" * 66)
    print(" 接实盘前置体检 · 币安")
    print("=" * 66)

    key = getattr(config, "BINANCE_API_KEY", "") or ""
    sec = getattr(config, "BINANCE_SECRET_KEY", "") or ""
    if not key or not sec:
        line(BAD, "BINANCE_API_KEY / BINANCE_SECRET_KEY 没配，后面全部查不了。")
        print()
        print("   去币安建 key 时只勾「读取 + 合约交易」，绝不勾提币，加 IP 白名单，")
        print("   然后写进 E:\\Hermes\\.env：")
        print("       BINANCE_API_KEY=...")
        print("       BINANCE_SECRET_KEY=...")
        print()
        print("=" * 66)
        print(" 结论：验不了（缺凭据）")
        print("=" * 66)
        return 1
    line(OK, f"API key 已配置（长度 {len(key)}），可以继续查。")

    # ---- 1. 联保模式（多资产模式）----------------------------------------
    # 联保模式下币安禁止切逐仓，这是「设置不了逐仓」最常见的真凶。
    print()
    try:
        multi = await binance.multi_assets_mode()
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"查联保模式失败：{exc}")
        multi = None

    if multi is True:
        line(BAD, "联保模式（多资产模式）开着 —— 逐仓设不了，就是它。")
        print("       官方原文：多资产模式仅支援全仓模式。")
        if fix:
            try:
                await binance._signed("POST", binance.FUT,
                                      "/fapi/v1/multiAssetsMargin",
                                      {"multiAssetsMargin": "false"})
                after = await binance.multi_assets_mode()
                if after is False:
                    line(OK, "已切成单币保证金模式（联保已关）。")
                else:
                    line(BAD, f"调用返回成功但复查仍是 {after}，请到 App 里手动切。")
            except Exception as exc:  # noqa: BLE001
                line(BAD, f"自动关联保失败：{exc}")
                print("       → 去 App：合约 → U 本位合约 → 点右上【单】/【联】→ 选「单币保证金模式」")
        else:
            print("       修法二选一：")
            print("       A. 本脚本加 --fix 自动关")
            print("       B. App：合约 → U 本位合约 → 点右上【单】/【联】→「单币保证金模式」")
            print("       注意：关掉后 BTC/ETH 不能当保证金，若另有仓位靠它们抵押，先确认不会被强平。")
    elif multi is False:
        line(OK, "单币保证金模式（联保已关）—— 逐仓可以设。")

    # ---- 2. 持仓模式（单向 / 双向）--------------------------------------
    print()
    try:
        hedge = await binance.position_mode()
        if hedge:
            line(BAD, "账户是双向持仓（hedge）—— 下单必须带 positionSide，"
                      "现在 place_market_order 没这个字段，实盘会全部报 -4061。")
            print("       两种改法：账户切单向（App 里改，需空仓空挂单），或给下单代码补 positionSide。")
        else:
            line(OK, "单向持仓 —— 下单不用带 positionSide，跟现有代码写法一致。")
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"查持仓模式失败：{exc}")

    # ---- 3. 现有持仓的保证金模式 -----------------------------------------
    print()
    try:
        rows = await binance.position_risk()
        if isinstance(rows, dict):
            rows = [rows]
        live_rows = [r for r in (rows or []) if float(r.get("positionAmt") or 0) != 0]
        if not live_rows:
            line(INFO, "当前没有持仓，逐仓状态等开第一个仓时再看。")
        else:
            for r in live_rows:
                sym = r.get("symbol")
                mt = str(r.get("marginType") or "").lower()
                amt = r.get("positionAmt")
                if mt == "isolated":
                    line(OK, f"{sym} 持仓 {amt} · 保证金模式 {mt}")
                else:
                    line(BAD, f"{sym} 持仓 {amt} · 保证金模式 {mt}（不是逐仓！）")
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"查持仓风险失败：{exc}")

    # ---- 4. 可用余额 -----------------------------------------------------
    print()
    try:
        acc = await binance.account()
        line(INFO, f"钱包余额 {acc.get('totalWalletBalance')} USDT · "
                   f"可用 {acc.get('availableBalance')} USDT · "
                   f"未实现盈亏 {acc.get('totalUnrealizedProfit')} USDT")
    except Exception as exc:  # noqa: BLE001
        line(BAD, f"查账户失败：{exc}")

    # ---- 结论 ------------------------------------------------------------
    print()
    print("=" * 66)
    if _problems:
        print(f" 结论：{len(_problems)} 个问题没过，不能接实盘")
        for i, p in enumerate(_problems, 1):
            print(f"   {i}. {p}")
    else:
        print(" 结论：全部通过 —— 可以进下一步（测试网开仓实测）")
    print("=" * 66)
    return 0 if not _problems else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--fix" in sys.argv)))
