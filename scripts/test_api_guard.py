"""中间件安全回归测试 —— 限流 / 读鉴权 / 写鉴权 / token 轮换宽限期。

★ 不进入 lifespan → 不启动引擎、不跑策略。
★ 只读数据库。
★ 用法：E:\\quant-bot\\.venv\\Scripts\\python.exe scripts\\test_api_guard.py

2026-11-11 建立。浩哥要求：限流 + GET 也要只读 key + token 定期轮换。
"""
import os
import sys

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient          # noqa: E402

from backend import config                          # noqa: E402
from backend import db                              # noqa: E402
from backend import livekeys                        # noqa: E402
from backend.main import _rate_buckets, app         # noqa: E402

# ★ 隔离：把凭据文件指到一个不存在的路径，强制走 config 里的测试值。
#   否则本地一旦存在 data/livekeys，测试读到的就是真实凭据，断言全乱。
livekeys.KEYS_FILE = os.path.join(ROOT, "data", "livekeys.__test_absent__")
livekeys._cache["mtime_ns"] = -1
livekeys._cache["data"] = {}

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  -> {detail}" if detail else ""))


def reset_rate():
    _rate_buckets.clear()


# ★ 不进入 lifespan：不用 with，就不启动引擎
# ★ base_url 必须是 127.0.0.1，否则 TestClient 默认 Host=testserver
#   会被 config.API_ALLOWED_HOSTS 挡成 403（第一版就栽在这）
c = TestClient(app, base_url="http://127.0.0.1")

# 手动初始化 DB（不进 lifespan 就不会自动建连接；本地库，不是生产库）
db.init()

VIEW = "test_view_key_" + "a" * 16
TOK = "test_api_token_" + "b" * 16
TOK_OLD = "test_api_token_prev_" + "c" * 12

old = (config.VIEW_KEY, config.API_TOKEN, config.API_TOKEN_PREV)

try:
    # ══════════════════════════════════════════════════════
    print("\n[1] 未配 VIEW_KEY —— 应该退化回旧行为（GET 不鉴权）")
    # ══════════════════════════════════════════════════════
    config.VIEW_KEY = ""
    config.API_TOKEN = TOK
    config.API_TOKEN_PREV = ""
    reset_rate()
    r = c.get("/api/gates")
    check("GET /api/gates 无凭据 -> 200（旧行为）", r.status_code == 200, f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════
    print("\n[2] 配了 VIEW_KEY —— GET 必须带凭据")
    # ══════════════════════════════════════════════════════
    config.VIEW_KEY = VIEW
    reset_rate()
    r = c.get("/api/gates")
    check("GET 无任何凭据 -> 401", r.status_code == 401, f"HTTP {r.status_code}")

    reset_rate()
    r = c.get("/api/gates", headers={"x-view-key": VIEW})
    check("GET 带正确 x-view-key -> 200", r.status_code == 200, f"HTTP {r.status_code}")

    reset_rate()
    r = c.get("/api/gates", headers={"x-view-key": "wrong_key"})
    check("GET 带错误 x-view-key -> 401", r.status_code == 401, f"HTTP {r.status_code}")

    reset_rate()
    r = c.get("/api/gates", headers={"x-api-token": TOK})
    check("GET 带操作 token -> 200", r.status_code == 200, f"HTTP {r.status_code}")

    reset_rate()
    r = c.get("/api/positions", headers={"x-view-key": VIEW})
    check("GET /api/positions 带 view key -> 200", r.status_code == 200, f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════
    print("\n[3] 非 /api 路径不受读鉴权影响（否则页面打不开）")
    # ══════════════════════════════════════════════════════
    reset_rate()
    r = c.get("/health")
    check("GET /health 无凭据 -> 200", r.status_code == 200, f"HTTP {r.status_code}")

    reset_rate()
    r = c.get("/")
    check("GET / 首页无凭据 -> 200（不是 401）", r.status_code == 200, f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════
    print("\n[4] 写操作：必须带 token，view key 不够")
    # ══════════════════════════════════════════════════════
    reset_rate()
    r = c.post("/api/__probe__", json={})
    check("POST 无凭据 -> 401", r.status_code == 401, f"HTTP {r.status_code}")

    reset_rate()
    r = c.post("/api/__probe__", json={}, headers={"x-view-key": VIEW})
    check("POST 只带 view key -> 401（观战者不能写）", r.status_code == 401, f"HTTP {r.status_code}")

    reset_rate()
    r = c.post("/api/__probe__", json={}, headers={"x-api-token": TOK})
    check("POST 带操作 token -> 不是 401", r.status_code != 401, f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════
    print("\n[5] token 轮换宽限期（PREV 还能用）")
    # ══════════════════════════════════════════════════════
    config.API_TOKEN_PREV = TOK_OLD
    reset_rate()
    r = c.post("/api/__probe__", json={}, headers={"x-api-token": TOK_OLD})
    check("POST 带旧 token（PREV）-> 不是 401", r.status_code != 401, f"HTTP {r.status_code}")

    reset_rate()
    r = c.get("/api/gates", headers={"x-api-token": TOK_OLD})
    check("GET 带旧 token（PREV）-> 200", r.status_code == 200, f"HTTP {r.status_code}")

    config.API_TOKEN_PREV = ""
    reset_rate()
    r = c.post("/api/__probe__", json={}, headers={"x-api-token": TOK_OLD})
    check("清空 PREV 后旧 token -> 401", r.status_code == 401, f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════
    print("\n[6] 限流：外网 IP 超限返 429")
    # ══════════════════════════════════════════════════════
    reset_rate()
    hit = None
    r429 = None
    for i in range(1, 140):
        r = c.get("/api/gates", headers={"x-view-key": VIEW, "x-real-ip": "203.0.113.9"})
        if r.status_code == 429:
            hit = i
            r429 = r
            break
    check("外网 IP 连续请求触发 429", hit is not None, f"第 {hit} 次被限" if hit else "140 次都没触发")
    if hit:
        check("限流阈值合理（100~125 之间）", 100 <= hit <= 125, f"第 {hit} 次")
    check("429 响应带 Retry-After 头",
          r429 is not None and "retry-after" in {k.lower() for k in r429.headers},
          str(dict(r429.headers)) if r429 is not None else "没拿到 429 响应")

    # ══════════════════════════════════════════════════════
    print("\n[7] 本机 IP 不限流（自己排查不受影响）")
    # ══════════════════════════════════════════════════════
    reset_rate()
    blocked = 0
    for _ in range(200):
        r = c.get("/api/gates", headers={"x-view-key": VIEW, "x-real-ip": "127.0.0.1"})
        if r.status_code == 429:
            blocked += 1
    check("127.0.0.1 连发 200 次 -> 0 次 429", blocked == 0, f"被限 {blocked} 次")

    # ══════════════════════════════════════════════════════
    print("\n[8] 静态资源不受影响（否则页面白屏）")
    # ══════════════════════════════════════════════════════
    reset_rate()
    r = c.get("/assets/does-not-exist.js", headers={"x-real-ip": "203.0.113.10"})
    check("GET /assets/* 无凭据 -> 不是 401", r.status_code != 401, f"HTTP {r.status_code}")

    # ══════════════════════════════════════════════════════
    print("\n[9] 操作者不受限流（自己的看板不能被自己拦住）")
    # ══════════════════════════════════════════════════════
    # 你开一个看板：每 5 秒轮询 6 个面板 + 静态资源，120 次/分钟根本不够。
    # 所以带有效 token 的请求必须豁免限流，只限陌生人 / 观战者。
    reset_rate()
    blocked = 0
    for _ in range(200):
        r = c.get("/api/gates", headers={"x-api-token": TOK, "x-real-ip": "203.0.113.11"})
        if r.status_code == 429:
            blocked += 1
    check("带操作 token 连发 200 次 -> 0 次 429", blocked == 0, f"被限 {blocked} 次")

    reset_rate()
    blocked = 0
    for _ in range(200):
        r = c.get("/api/gates", headers={"x-view-key": VIEW, "x-real-ip": "203.0.113.12"})
        if r.status_code == 429:
            blocked += 1
    check("观战 key 仍受限流（不是谁都能刷）", blocked > 0, f"被限 {blocked} 次")

finally:
    config.VIEW_KEY, config.API_TOKEN, config.API_TOKEN_PREV = old
    _rate_buckets.clear()

print("\n" + "=" * 60)
print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("  失败项：")
    for f in FAIL:
        print("    - " + f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
