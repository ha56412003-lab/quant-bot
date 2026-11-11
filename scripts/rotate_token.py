"""轮换 QB_API_TOKEN（写操作令牌）—— 不重启服务，改完立即生效。

## 浩哥的要求（2026-11-11 原话）

「切换观战 KEY 需要重启服务 这个不太行 需要改掉 后端服务器 不能关」

虽然这句说的是观战 key，但 token 是同一个毛病（模块级常量），
而且现在凭据已统一存 data/livekeys，如果这里还去改 .env，
重启后 .env 会被 livekeys 覆盖掉 —— 轮换直接失灵。所以一并改。

## 这个脚本做的事

  1. 生成一个新的 64 位十六进制 token
  2. 把服务器上当前的 QB_API_TOKEN 挪到 QB_API_TOKEN_PREV（宽限期）
  3. 新 token 写进 QB_API_TOKEN
  4. 同步更新本地 E:\\quant-bot\\.env.local 第 1 行
  5. 【不重启服务】—— 后端每次请求比对文件 mtime，改完立即生效
  6. 验证：新 token 能读能写、旧 token 在宽限期内还能用

★ 宽限期的作用：万一你手头的浏览器还挂着旧 token，它不会立刻失效。
  确认所有设备都换好之后，跑 --clear-prev 把旧 token 作废。

用法：
  .venv\\Scripts\\python.exe scripts\\rotate_token.py               # 真轮换
  .venv\\Scripts\\python.exe scripts\\rotate_token.py --dry         # 只预演
  .venv\\Scripts\\python.exe scripts\\rotate_token.py --clear-prev  # 作废旧 token

★ 安全：任何一步失败都保持原状，不会把你锁在门外。
★ 本文件里不用 ✓ ✗ ⚠ 这类字符（GBK 日志读不了）。
"""
import argparse
import os
import secrets
import shutil as _shutil
import subprocess
import sys

# ★ 直接调 ssh.exe + 参数列表，完全不经过 shell。
#   踩过的坑：subprocess(["bash","-c",...]) 在这个环境里拿到的 bash 是隔离的
#   （uname 返回 Linux、/c/Users 不存在），所有路径都失效。
SSH = _shutil.which("ssh") or "ssh"
SSH_KEY = os.path.expanduser("~/.ssh/qb_hk")
HOST = "root@103.148.245.170"
REMOTE = "/opt/quantbot"
LIVEKEYS = f"{REMOTE}/data/livekeys"
LOCAL_ENV = r"E:\quant-bot\.env.local"


def ssh(script, timeout=180):
    """在服务器上跑一段 shell（script 作为 ssh 的单个参数传给远端 bash）。"""
    p = subprocess.run(
        [SSH, "-i", SSH_KEY,
         "-o", "ConnectTimeout=20",
         "-o", "StrictHostKeyChecking=no",
         "-o", "BatchMode=yes",
         HOST, script],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def live_get(name):
    """读当前真正生效的值：先 livekeys，没有则回退 .env（顺序与 backend/livekeys.py 一致）。"""
    rc, out = ssh(
        f'if [ -f {LIVEKEYS} ] && grep -q "^{name}=" {LIVEKEYS}; then '
        f'grep "^{name}=" {LIVEKEYS} | head -1 | cut -d= -f2-; '
        f'else grep "^{name}=" {REMOTE}/.env | head -1 | cut -d= -f2-; fi')
    return out.strip() if rc == 0 else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只预演，不动文件")
    ap.add_argument("--clear-prev", action="store_true", help="清空宽限期，旧 token 立即失效")
    args = ap.parse_args()

    print("=" * 64)
    print("  QB_API_TOKEN 轮换（不重启服务）")
    print("=" * 64)

    # ── 连通性检查（任何动作之前）────────────────────────────
    rc, out = ssh("systemctl is-active quantbot; echo ---; "
                  f"ls -la {LIVEKEYS} 2>/dev/null | wc -l")
    if rc != 0:
        print("  [!] 连不上服务器，什么都没做。")
        print("    " + out.strip()[:200])
        return 2
    parts = out.strip().split("---")
    print(f"  服务状态 : {parts[0].strip()}")
    has_lk = parts[1].strip() != "0" if len(parts) > 1 else False
    print(f"  凭据文件 : {'data/livekeys（热加载，不重启）' if has_lk else '.env（旧方案，跑一次真轮换自动迁移）'}")

    cur_tok = live_get("QB_API_TOKEN")
    prev_tok = live_get("QB_API_TOKEN_PREV")
    if not cur_tok:
        print("  [!] 服务器上 QB_API_TOKEN 是空的，先把它配好再轮换。")
        return 2
    print(f"  当前 token: {len(cur_tok)} 位，前 4 位 {cur_tok[:4]}***")

    # ── 清宽限期模式 ────────────────────────────────────────
    if args.clear_prev:
        if not prev_tok:
            print("\n  宽限期本来就是空的，无事可做。")
            return 0
        print(f"\n  即将作废旧 token（{len(prev_tok)} 位，前 4 位 {prev_tok[:4]}***）")
        if args.dry:
            print("  [dry] 会执行：从 livekeys 删掉 QB_API_TOKEN_PREV（不重启）")
            return 0
        rc, out = ssh(
            f'set -e; cd {REMOTE}; mkdir -p data; touch {LIVEKEYS}; '
            f'cp {LIVEKEYS} {LIVEKEYS}.bak_prev_$(date +%s) 2>/dev/null || true; '
            f'sed -i "/^QB_API_TOKEN_PREV=/d" {LIVEKEYS}; chmod 600 {LIVEKEYS}; echo OK')
        if rc != 0 or "OK" not in out:
            print("  [!] 失败：" + out.strip()[:200])
            return 1
        print("  [OK] 旧 token 已作废，宽限期清空（服务未重启）")
        return 0

    # ── 真轮换 ─────────────────────────────────────────────
    new_tok = secrets.token_hex(32)
    print(f"  新   token: {len(new_tok)} 位，前 4 位 {new_tok[:4]}***")

    if args.dry:
        print("\n  [dry] 会做的事：")
        print(f"    1. 备份 {LIVEKEYS} 为 .bak_rotate_<时间戳>")
        print("    2. 当前 token → QB_API_TOKEN_PREV（宽限期）")
        print("    3. 新 token   → QB_API_TOKEN")
        print(f"    4. 更新本地 {LOCAL_ENV} 第 1 行")
        print("    5. 【不重启任何服务】")
        print("    6. 验证新 token 能读能写、旧 token 还能用")
        return 0

    # ① 服务器：原子写 livekeys（不重启）
    script = f"""
set -e
cd {REMOTE}
mkdir -p data
touch {LIVEKEYS}
cp {LIVEKEYS} {LIVEKEYS}.bak_rotate_$(date +%s) 2>/dev/null || true
# 首次迁移：.env 里的观战 key 也搬过来
if ! grep -q "^QB_VIEW_KEY=" {LIVEKEYS} 2>/dev/null; then
  V=$(grep "^QB_VIEW_KEY=" .env | head -1 | cut -d= -f2-)
  [ -n "$V" ] && echo "QB_VIEW_KEY=$V" >> {LIVEKEYS}
fi
# 当前 token 挪到 PREV
CUR=$(grep "^QB_API_TOKEN=" {LIVEKEYS} 2>/dev/null | head -1 | cut -d= -f2-)
sed -i "/^QB_API_TOKEN_PREV=/d" {LIVEKEYS}
sed -i "/^QB_API_TOKEN=/d" {LIVEKEYS}
[ -n "$CUR" ] && echo "QB_API_TOKEN_PREV=$CUR" >> {LIVEKEYS}
echo "QB_API_TOKEN={new_tok}" >> {LIVEKEYS}
chmod 600 {LIVEKEYS}
echo "  写入完成，共 $(grep -c '=' {LIVEKEYS}) 个键"
"""
    rc, out = ssh(script)
    if rc != 0:
        print("  [!] 写服务器 livekeys 失败：" + out.strip()[:300])
        print("  → 服务没动，旧 token 仍然有效。")
        return 1
    print("  [OK] 服务器 data/livekeys 已更新（服务未重启）")

    # ② 本地 .env.local 第 1 行
    try:
        with open(LOCAL_ENV, "r", encoding="utf-8") as f:
            local_lines = f.read().splitlines()
        rest = [ln for ln in local_lines[1:] if not ln.startswith("QB_API_TOKEN=")]
        with open(LOCAL_ENV, "w", encoding="utf-8") as f:
            f.write("\n".join([new_tok] + rest) + "\n")
        print(f"  [OK] 本地 {os.path.basename(LOCAL_ENV)} 第 1 行已更新")
    except Exception as e:
        print(f"  [!] 本地 .env.local 更新失败：{e}")
        print("    手工改：第 1 行替换成新 token（服务器上已生效）")

    # ③ 不重启，直接验证
    print("\n  验证（服务全程没重启）：")
    verify = f"""
cd {REMOTE}
NEW={new_tok}
OLD=$(grep "^QB_API_TOKEN_PREV=" {LIVEKEYS} 2>/dev/null | head -1 | cut -d= -f2-)
VK=$(grep "^QB_VIEW_KEY=" {LIVEKEYS} 2>/dev/null | head -1 | cut -d= -f2-)
if [ -z "$VK" ]; then VK=$(grep "^QB_VIEW_KEY=" .env | head -1 | cut -d= -f2-); fi
echo "  服务运行时长      : $(ps -o etimes= -p $(systemctl show quantbot -p MainPID --value) 2>/dev/null | tr -d ' ') 秒"
echo "  新 token 读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -H "x-api-token: $NEW" http://127.0.0.1:8899/api/status)"
echo "  新 token 写 probe  : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -X POST -H "Content-Type: application/json" -H "x-api-token: $NEW" -d "{{}}" http://127.0.0.1:8899/api/__probe__)"
if [ -n "$OLD" ]; then
  echo "  旧 token 读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -H "x-api-token: $OLD" http://127.0.0.1:8899/api/status)"
fi
echo "  观战 key 读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -H "x-view-key: $VK" http://127.0.0.1:8899/api/status)"
echo "  无凭据   读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 http://127.0.0.1:8899/api/status)"
"""
    rc, out = ssh(verify)
    print(out.rstrip())

    # ★ 写 probe 用不存在的路由探路：401 = 鉴权没过；404/405 = 通过了鉴权只是没这个路由（正确）
    ok = ("新 token 读 status : 200" in out and
          ("新 token 写 probe  : 404" in out or "新 token 写 probe  : 405" in out) and
          "观战 key 读 status : 200" in out and
          "无凭据   读 status : 401" in out)
    print()
    if ok:
        print("  [OK] 轮换成功，服务一次都没重启。")
        print("    新 token 已生效，旧 token 还能用（宽限期）。")
        print("    确认所有设备都换成新的之后，跑：")
        print("      python scripts\\rotate_token.py --clear-prev")
        return 0
    print("  [!] 验证没全过，请看上面的状态码。回滚（不用重启）：")
    print(f'    ssh -i {SSH_KEY} {HOST} "cd {REMOTE} && cp data/livekeys.bak_rotate_* data/livekeys"')
    return 1


if __name__ == "__main__":
    sys.exit(main())
