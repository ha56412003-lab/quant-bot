#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页面冒烟测试 —— 用真实 Chrome 把看板 6 个页面挨个点一遍。

为什么需要它
------------
后端有 12 个测试文件（tests/），但前端一个测试都没有。结果是
「tests 全绿」和「页面能打开」是两件不相干的事 —— 2026-11-11
配置页因为一行裸 fetch 白屏，21 项后端鉴权测试全 PASS 也没发现。

这个脚本补的就是这条缝：真的开一个 Chrome、真的点每个 tab、
真的读页面内容和控制台报错。

★ 关键教训（第一版就这么翻车的）
--------------------------------
第一版判据是「页面文字 > 80 字就算通过」。拿一个假 token 一测，
6/6 照样全绿 —— 因为 token 无效时页面骨架（菜单、表头、字段名）
照样渲染，只是数据全空：`0 个监控标的` / `暂无数据` / `账户净值 -`。

这比白屏更隐蔽 —— 看着像「今天没交易」。

所以判据必须是三层：
  第一层 API 预检：token 无效当场退出，不看页面
  第二层 通用黑名单：出现「没有访问权限 / 加载失败 / Unauthorized」直接判死
  第三层 每页数据指纹：必须命中「真数据」正则（见 PAGES）

零安装
------
只依赖 venv 里已有的 websockets / httpx，用 Chrome 自带的
DevTools Protocol 驱动，不装 playwright / selenium。

用法
----
    python scripts/smoke_pages.py                # 测线上域名
    python scripts/smoke_pages.py --base http://127.0.0.1:8899
    python scripts/smoke_pages.py --token xxx    # 显式给 token
    python scripts/smoke_pages.py --keep         # 测试完不关 Chrome（排查用）
    python scripts/smoke_pages.py --headed       # 用有窗口的 Chrome

输出
----
    reports/smoke_latest.json    最近一次完整结果（覆盖写）
    logs/smoke.log               每次 6 行摘要（追加）
    reports/smoke_fail_*.png     只在失败时留截图

退出码
------
    0 = 全部页面通过
    1 = 至少一个页面失败
    2 = 环境问题（Chrome 起不来 / 没 token / token 无效）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
LOGS = ROOT / "logs"
CST = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 6 个页面 + 每页的"真数据指纹"
# 指纹 = 真 token 下必然出现、假 token 下必然不出现的东西
# （2026-11-11 用真/假 token 各跑一遍逐页对比出来的）
# ---------------------------------------------------------------------------
PAGES = [
    # (按钮文字, [指纹正则...命中任一即算有数据], 指纹说明)
    ("总览", [r"[1-9]\d* 个监控标的"], "监控标的数 > 0"),
    ("策略库", [r"S15", r"笔·胜率", r"笔 · 胜率"], "策略实例行（含战绩）"),
    ("因子库", [r"显示 ([1-9]\d*)/"], "因子数量非 0/0"),
    ("交易日志", [r"\d+\s*笔\s*·\s*胜率"], "交易战绩行"),
    # 风控页：账户净值 和数字之间可能有换行/竖线/冒号，必须容忍
    ("风控", [r"账户净值[\s|:：]+\$?[\d,]+"], "净值数字"),
    # 配置页：不能用 Clawby（字段标签在未授权时也有），必须用只有真数据才会渲染的区块
    ("配置", [r"实盘执行交易所"], "凭据区块完整渲染"),
]

# 通用黑名单：出现任何一个 = 这个页面在报错
BAD_WORDS = [
    "没有访问权限", "加载失败", "获取失败", "请求失败", "网络错误",
    "Unauthorized", "未授权", "Failed to fetch", "Network Error",
]

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 在 document 创建前就装好错误收集器
ERR_HOOK = r"""
window.__smokeErrs = [];
window.addEventListener('error', function (e) {
    window.__smokeErrs.push('[error] ' + (e.message || '') +
        ' @ ' + (e.filename || '').split('/').pop() + ':' + (e.lineno || ''));
});
window.addEventListener('unhandledrejection', function (e) {
    var r = e.reason;
    window.__smokeErrs.push('[reject] ' + String((r && (r.stack || r.message)) || r).slice(0, 300));
});
"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_token(explicit):
    """token 优先级：命令行 > .env.local 第一行"""
    if explicit:
        return explicit.strip()
    p = ROOT / ".env.local"
    if p.exists():
        try:
            first = p.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
            if first and not first.startswith("#") and "=" not in first:
                return first
        except Exception:
            pass
    return None


def precheck(base, token):
    """第一层：直接打接口。token 无效就在这里死掉，不用看页面。

    返回 (ok, 说明, 关键数据)
    """
    import httpx

    try:
        r = httpx.get(
            base.rstrip("/") + "/api/status",
            headers={"x-api-token": token},
            timeout=15.0,
        )
    except Exception as e:
        return False, "接口连不上：%s: %s" % (type(e).__name__, e), {}

    if r.status_code == 401:
        return False, "★ token 无效（/api/status 返回 401）—— 页面数据必然全空", {}
    if r.status_code != 200:
        return False, "★ /api/status 返回 %s" % r.status_code, {}

    try:
        d = r.json()
    except Exception:
        return False, "/api/status 返回的不是 JSON", {}

    return True, "token 有效", d


def log_line(row):
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "smoke.log", "a", encoding="utf-8", newline="\n") as f:
        f.write(
            "%s  %-6s  text=%-6s  errs=%-2s  %s\n"
            % (
                row["time"],
                row["page"],
                row.get("body_len", "?"),
                len(row.get("errors") or []),
                "PASS" if row["ok"] else "FAIL: " + (row.get("reason") or ""),
            )
        )


# ---------------------------------------------------------------------------
# CDP 极简客户端
# ---------------------------------------------------------------------------
class CDP:
    def __init__(self, ws_url):
        from websockets.sync.client import connect

        self._connect = connect
        self._ws = connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=20)
        self._id = 0

    def send(self, method, **params):
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = time.time() + 30
        while time.time() < deadline:
            raw = self._ws.recv(timeout=30)
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("CDP %s 失败: %s" % (method, msg["error"]))
                return msg.get("result") or {}
        raise TimeoutError("CDP %s 超时" % method)

    def eval(self, expr, await_promise=False):
        r = self.send(
            "Runtime.evaluate",
            expression=expr,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
        )
        if r.get("exceptionDetails"):
            raise RuntimeError(
                "JS 异常: " + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:300]
            )
        return (r.get("result") or {}).get("value")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Chrome 生命周期
# ---------------------------------------------------------------------------
class Chrome:
    def __init__(self, exe, headless=True):
        self.exe = exe
        self.headless = headless
        self.proc = None
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="qb_smoke_")

    def start(self):
        args = [
            self.exe,
            "--remote-debugging-port=%d" % self.port,
            "--user-data-dir=%s" % self.profile,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--window-size=1600,1000",
        ]
        if self.headless:
            args.append("--headless=new")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        import httpx

        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                r = httpx.get("http://127.0.0.1:%d/json/version" % self.port, timeout=1.0)
                if r.status_code == 200:
                    return
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("Chrome 起不来（25 秒内调试端口没就绪）")

    def new_tab(self, url):
        import httpx

        r = httpx.put(
            "http://127.0.0.1:%d/json/new?%s" % (self.port, url), timeout=10.0
        )
        if r.status_code != 200:
            raise RuntimeError("开标签页失败: %s %s" % (r.status_code, r.text[:200]))
        return r.json()["webSocketDebuggerUrl"]

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        shutil.rmtree(self.profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# 单页检测
# ---------------------------------------------------------------------------
def click_tab(cdp, label):
    js = (
        "(() => {"
        "  const bs = [...document.querySelectorAll('button')];"
        "  const b = bs.find(x => (x.textContent||'').trim() === %s);"
        "  if (!b) return false;"
        "  b.click(); return true;"
        "})()" % json.dumps(label)
    )
    try:
        return bool(cdp.eval(js))
    except Exception:
        return False


def judge(body, errors, root_kids, fingerprints, hint):
    """返回 '' 表示通过，否则返回失败原因"""
    if len(body) < 80:
        return "★ 白屏（页面文字只有 %d 字）" % len(body)
    if root_kids == 0:
        return "★ React 根节点是空的（整棵树没挂上）"

    # 通用黑名单优先
    for w in BAD_WORDS:
        if w in body:
            return "★ 页面出现错误字样「%s」" % w
    for e in errors:
        for w in BAD_WORDS:
            if w in e:
                return "★ JS 报错：%s" % e[:140]

    # 数据指纹：必须命中一个，否则就是"渲染了但没数据"
    hit = None
    for pat in fingerprints:
        m = re.search(pat, body)
        if m:
            hit = m.group(0)[:40]
            break
    if not hit:
        return "★ 页面渲染了但没有真数据（缺「%s」）—— 可能接口 401 被静默吞掉" % hint

    if errors:
        return "★ 有 %d 条 JS 报错：%s" % (len(errors), errors[0][:120])
    return ""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(base, token, keep, headless):
    base = base.rstrip("/")

    # ===== 第一层：API 预检 =====
    print("目标  : %s" % base)
    print("凭证  : token 前 4 位 %s***" % token[:4])
    ok, why, st = precheck(base, token)
    if not ok:
        print("预检  : [X] %s" % why)
        print("")
        print("不测页面了 —— token 都不通，页面必然是空数据。")
        print("先把 token 弄对（桌面「量化看板」图标会自己带对 token）。")
        return 2

    eq = st.get("equity")
    mode = st.get("mode")
    halted = st.get("halted")
    pool = st.get("pool") or {}
    pool_n = pool.get("monitor_n") if isinstance(pool, dict) else None
    print("预检  : [OK] %s" % why)
    print("        模式=%s  净值=%s  监控池=%s  熔断=%s" % (mode, eq, pool_n, halted))

    exe = find_chrome()
    if not exe:
        print("[X] 找不到 Chrome / Edge")
        return 2
    print("浏览器: %s  %s" % (os.path.basename(exe), "无头" if headless else "有窗口"))
    print("-" * 74)

    chrome = Chrome(exe, headless=headless)
    results = []
    started = datetime.now(CST)

    try:
        chrome.start()
        cdp = CDP(chrome.new_tab("about:blank"))
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        cdp.send("Page.addScriptToEvaluateOnNewDocument", source=ERR_HOOK)
        cdp.send("Page.navigate", url="%s/?token=%s" % (base, token))
        time.sleep(6)

        for label, fingerprints, hint in PAGES:
            t0 = time.time()
            row = {"time": datetime.now(CST).strftime("%H:%M:%S"), "page": label}

            clicked = click_tab(cdp, label)
            if label == "总览" and not clicked:
                clicked = True  # 首屏本来就是总览
            time.sleep(2.6)

            body = cdp.eval("(document.body && document.body.innerText || '').trim()") or ""
            raw_errs = cdp.eval("JSON.stringify(window.__smokeErrs || [])") or "[]"
            try:
                errors = json.loads(raw_errs)
            except Exception:
                errors = []
            root_kids = cdp.eval(
                "document.getElementById('root') ? document.getElementById('root').children.length : -1"
            )

            reason = ""
            if not clicked:
                reason = "页面上找不到这个 tab 按钮"
            else:
                reason = judge(body, errors, root_kids, fingerprints, hint)

            row["body_len"] = len(body)
            row["errors"] = errors
            row["root_children"] = root_kids
            row["took_ms"] = int((time.time() - t0) * 1000)
            row["ok"] = not reason
            row["reason"] = reason
            results.append(row)
            log_line(row)

            print(
                "  %s  %-6s  text=%5d  jsErrs=%d  %dms%s"
                % (
                    "PASS" if row["ok"] else "FAIL",
                    label,
                    len(body),
                    len(errors),
                    row["took_ms"],
                    "" if row["ok"] else "   <- " + reason,
                )
            )

            if not row["ok"]:
                try:
                    shot = cdp.send("Page.captureScreenshot", format="png")
                    REPORTS.mkdir(parents=True, exist_ok=True)
                    fp = REPORTS / ("smoke_fail_%s_%s.png" % (label, started.strftime("%H%M%S")))
                    fp.write_bytes(base64.b64decode(shot["data"]))
                    row["screenshot"] = str(fp)
                    print("        截图: %s" % fp)
                except Exception as e:
                    print("        截图失败: %s" % e)

        cdp.close()
    finally:
        if keep:
            print("")
            print("[--keep] Chrome 留着，调试端口 %d" % chrome.port)
            print("         profile %s" % chrome.profile)
        else:
            chrome.close()

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    summary = {
        "time": started.isoformat(),
        "base": base,
        "precheck": {"ok": ok, "why": why, "equity": eq, "mode": mode, "monitor_n": pool_n},
        "pages": results,
        "passed": passed,
        "total": total,
        "ok": passed == total and total == len(PAGES),
    }

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "smoke_latest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print("-" * 74)
    print(
        "结果: %d/%d 页面通过   %s"
        % (passed, total, "[OK] 全绿" if summary["ok"] else "[X] 有失败")
    )
    print("报告: %s" % (REPORTS / "smoke_latest.json"))
    print("日志: %s" % (LOGS / "smoke.log"))

    if not summary["ok"]:
        print("")
        print("失败详情:")
        for r in results:
            if not r["ok"]:
                print("  - %s: %s" % (r["page"], r["reason"]))

    return 0 if summary["ok"] else 1


def main():
    ap = argparse.ArgumentParser(description="看板页面冒烟测试（真实 Chrome）")
    ap.add_argument("--base", default="https://quant.hahayuyue.cn", help="站点根地址")
    ap.add_argument("--token", default=None, help="操作 token（默认读 .env.local 第一行）")
    ap.add_argument("--keep", action="store_true", help="测试完不关 Chrome")
    ap.add_argument("--headed", action="store_true", help="用有窗口的 Chrome")
    a = ap.parse_args()

    tok = read_token(a.token)
    if not tok:
        print("[!] 没拿到 token。")
        print("    → 用 --token <token> 指定，或确认 E:/quant-bot/.env.local 第一行有值。")
        return 2

    return run(a.base, tok, a.keep, headless=not a.headed)


if __name__ == "__main__":
    sys.exit(main())
