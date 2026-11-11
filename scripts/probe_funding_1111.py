# -*- coding: utf-8 -*-
"""资金费率快检：11-10 那 6 个共线空单开仓时，费率是否处于极端拥挤区？

只读币安公开接口，不写库、不改策略。
结论用途：判断「资金费率」这个维度有没有可能做成第 8 道进场闸。
"""
import json
import os
import statistics as st
import sys
import time
import urllib.parse
import urllib.request

PROXY = "http://127.0.0.1:7897"
BASE = "https://fapi.binance.com/fapi/v1/fundingRate"
_op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

# 2026-11-10 23:45（北京）= 15:45 UTC，最近的结算点是 16:00 UTC（北京 11-11 00:00）
TARGET_MS = 1794240000000 + 0      # 占位，下面按实际算
SYMS = ["INJUSDT", "ENAUSDT", "PUMPUSDT", "BTCUSDT", "TRUMPUSDT", "NEARUSDT"]


def fetch(sym, limit=1000):
    q = urllib.parse.urlencode({"symbol": sym, "limit": limit})
    with _op.open(BASE + "?" + q, timeout=25) as r:
        return json.loads(r.read().decode())


def main():
    # 目标时刻：2026-11-10 23:45 北京时间 = 2026-11-10 15:45 UTC
    target = int(time.mktime(time.strptime("2026-11-10 23:45", "%Y-%m-%d %H:%M"))) * 1000
    # mktime 按本地时区（北京）算，直接得北京时刻的 epoch
    print("目标时刻（本地）:", time.strftime("%Y-%m-%d %H:%M", time.localtime(target / 1000)))
    print()

    hdr = "%-12s %10s %10s %10s %8s %8s" % ("币", "开仓时费率", "历史均值", "历史标准差", "Z值", "分位")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for s in SYMS:
        try:
            d = fetch(s)
        except Exception as e:                  # noqa: BLE001
            print("%-12s 拉取失败: %s" % (s, e))
            continue
        if not d:
            print("%-12s 无数据" % s)
            continue
        rates = [(int(x["fundingTime"]), float(x["fundingRate"])) for x in d]
        rates.sort()
        vals = [v for _, v in rates]
        # 找开仓时刻「之前最近」的结算点（那时市场已知的费率）
        before = [(t, v) for t, v in rates if t <= target]
        if not before:
            print("%-12s 历史太短，无开仓前费率" % s)
            continue
        t0, v0 = before[-1]
        mu = st.mean(vals)
        sd = st.pstdev(vals) if len(vals) > 1 else 0.0
        z = (v0 - mu) / sd if sd else 0.0
        pct = sum(1 for v in vals if v <= v0) / len(vals) * 100
        rows.append((s, v0, mu, sd, z, pct, len(vals)))
        print("%-12s %9.5f%% %9.5f%% %9.5f%% %8.2f %7.1f%%"
              % (s, v0 * 100, mu * 100, sd * 100, z, pct))

    print()
    print("说明：费率 > 0 = 多头付钱给空头（多头拥挤）；< 0 = 空头付钱（空头拥挤）。")
    print("      这 6 笔都是【空单】，若费率显著为负 = 空头已经拥挤 = 做空容易被挤，理论上该过滤。")
    print()
    if rows:
        neg = [r for r in rows if r[1] < 0]
        print("6 个币里费率为负（空头拥挤）的有 %d 个" % len(neg))
        strong = [r for r in rows if r[4] < -1.0]
        print("Z 值 < -1（显著偏空拥挤）的有 %d 个：%s"
              % (len(strong), ", ".join(r[0] for r in strong) if strong else "无"))
        print()
        print("★ 如果这 6 笔开仓时费率并不极端，说明「费率过滤」救不了 11-10 那一波，")
        print("  这个维度对 S15 价值有限；反之值得做成第 8 道闸。")


if __name__ == "__main__":
    main()
