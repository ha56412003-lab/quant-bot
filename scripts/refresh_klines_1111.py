# -*- coding: utf-8 -*-
"""补 K 线数据到最新（2026-11-11）

背景：pool_cache.pkl 的 K 线只到 2026-11-05 20:00，11-06 之后 6 天是盲区，
      导致 11-10 那波 V 型反转不在回测样本里。

做法：
  1. 备份旧缓存（*.bak_1111）
  2. 对每个币从「本地最后一条 ts」往后拉，走代理打币安 fapi klines
  3. 合并去重、按 ts 排序
  4. 指标全量重算（_ce_zlsma 有递归状态，必须从头算）
  5. EFF 只增量补（EFF 只看窗口内，历史值不变）
  6. 写新缓存

★ 只改 E:\\Hermes\\cache\\scratch 下的 pkl，不碰生产库、不碰仓库文件。
用法：PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/refresh_klines_1111.py
"""
import json
import os
import pickle
import shutil
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from backend.strategies import _ce_zlsma        # noqa: E402
from backend.quality import _eff, EFF_WIN      # noqa: E402

SC = r"E:\Hermes\cache\scratch"
PROXY = "http://127.0.0.1:7897"
BASE = "https://fapi.binance.com/fapi/v1/klines"
CHUNK = 1500

_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def fetch(symbol, start_ms, end_ms):
    """拉 [start_ms, end_ms) 的 15m K 线，自动翻页。"""
    out = []
    cur = start_ms
    while cur < end_ms:
        q = urllib.parse.urlencode({
            "symbol": symbol, "interval": "15m",
            "startTime": int(cur), "endTime": int(end_ms), "limit": CHUNK,
        })
        for attempt in range(3):
            try:
                with _opener.open(BASE + "?" + q, timeout=25) as r:
                    raw = json.loads(r.read().decode())
                break
            except Exception as e:                       # noqa: BLE001
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        if not raw:
            break
        for k in raw:
            out.append({
                "ts": int(k[0]) // 1000,
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),
            })
        nxt = int(raw[-1][0]) + 15 * 60 * 1000
        if nxt <= cur:
            break
        cur = nxt
        if len(raw) < CHUNK:
            break
        time.sleep(0.12)
    return out


def main():
    cache_p = os.path.join(SC, "pool_cache.pkl")
    eff_p = os.path.join(SC, "effcache.pkl")

    for src in (cache_p, eff_p):
        bak = src + ".bak_1111"
        if not os.path.exists(bak):
            shutil.copyfile(src, bak)
            print("已备份:", os.path.basename(bak))

    D = pickle.load(open(cache_p, "rb"))
    E = pickle.load(open(eff_p, "rb"))

    now_ms = int(time.time() // 900 * 900 * 1000)     # 对齐到 15m
    syms = sorted(D.keys())
    print("币数 %d，目标截止 %s" % (len(syms),
          time.strftime("%Y-%m-%d %H:%M", time.localtime(now_ms / 1000))))

    ok = fail = 0
    added_total = 0
    for idx, s in enumerate(syms, 1):
        ks, _ind, _flag = D[s]
        if not ks:
            continue
        last_ts = int(ks[-1]["ts"])
        start_ms = (last_ts + 900) * 1000
        if start_ms >= now_ms:
            ok += 1
            continue
        try:
            fresh = fetch(s, start_ms, now_ms)
        except Exception as e:                        # noqa: BLE001
            print("  [%d/%d] %s 拉取失败: %s" % (idx, len(syms), s, e))
            fail += 1
            continue
        if not fresh:
            ok += 1
            continue
        # 合并去重
        seen = {int(k["ts"]) for k in ks}
        add = [k for k in fresh if int(k["ts"]) not in seen]
        if not add:
            ok += 1
            continue
        newks = ks + add
        newks.sort(key=lambda k: int(k["ts"]))
        # 指标全量重算（CE 有递归状态）
        newind = _ce_zlsma(newks, ce_len=1, ce_mult=1.5, z_len=50)
        D[s] = (newks, newind, [False] * len(newks))
        # EFF 增量补
        neweff = list(E.get(s, []))
        if len(neweff) > len(ks):
            neweff = neweff[:len(ks)]
        while len(neweff) < len(ks):
            neweff.append(None)
        for i in range(len(ks), len(newks)):
            v = _eff(newks[:i + 1], EFF_WIN)
            neweff.append(v if v is not None else 0.0)
        E[s] = neweff
        ok += 1
        added_total += len(add)
        if idx % 20 == 0:
            print("  ...%d/%d，累计新增 %d 根" % (idx, len(syms), added_total))

    print("完成：成功 %d，失败 %d，共新增 %d 根 K 线" % (ok, fail, added_total))

    # 抽样核对时间范围
    s0 = syms[0]
    for k in D[s0][0][-3:]:
        print("  %s 末尾: %s" % (s0, time.strftime("%Y-%m-%d %H:%M",
                                                time.localtime(int(k["ts"])))))

    pickle.dump(D, open(cache_p, "wb"), protocol=4)
    pickle.dump(E, open(eff_p, "wb"), protocol=4)
    print("已写回:", cache_p, os.path.getsize(cache_p), "bytes")
    print("已写回:", eff_p, os.path.getsize(eff_p), "bytes")


if __name__ == "__main__":
    main()
