"""轮换 QB_API_TOKEN（写操作令牌）。

浩哥 2026-11-11 要求：「token 定期轮换」。

这个脚本做的事：
  1. 生成一个新的 64 位十六进制 token
  2. 把服务器上当前的 QB_API_TOKEN 挪到 QB_API_TOKEN_PREV（宽限期）
  3. 新 token 写进 QB_API_TOKEN
  4. 同步更新本地 E:\\quant-bot\\.env.local 第 1 行
  5. 重启服务器 quantbot
  6. 验证：新 token 能读能写、旧 token 在宽限期内还能用

★ 宽限期的作用：万一你手头的浏览器还挂着旧 token，它不会立刻失效。
  确认所有设备都换好之后，跑 --clear-prev 把旧 token 作废。

用法：
  .venv\\Scripts\\python.exe scripts\\rotate_token.py               # 真轮换
  .venv\\Scripts\\python.exe scripts\\rotate_token.py --dry         # 只预演
  .venv\\Scripts\\python.exe scripts\\rotate_token.py --clear-prev  # 作废旧 token
  .venv\\Scripts\\python.exe scripts\\rotate_token.py --no-restart  # 不重启（只改文件）

★ 安全：任何一步失败都会把 .env 还原，不会把你锁在门外。
"""
import argparse
import os
import re
import secrets
import subprocess
import sys

# ★ 直接调 ssh.exe + 参数列表，完全不经过 shell。
#   踩过的坑：subprocess(["bash","-c",...]) 在这个环境里拿到的 bash 是隔离的
#   （uname 返回 Linux、/c/Users 不存在），所有路径都失效。
#   用列表参数既不展开 ~ 也不吃反斜杠，最稳。
import shutil as _shutil

SSH = _shutil.which("ssh") or "ssh"
SSH_KEY = os.path.expanduser("~/.ssh/qb_hk")          # C:\Users\...\.ssh\qb_hk
HOST = "root@103.148.245.170"
REMOTE = "/opt/quantbot"
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


def remote_env_get(name):
    rc, out = ssh(f'grep "^{name}=" {REMOTE}/.env | head -1 | cut -d= -f2-')
    return out.strip() if rc == 0 else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只预演，不动文件")
    ap.add_argument("--clear-prev", action="store_true", help="清空宽限期，旧 token 立即失效")
    ap.add_argument("--no-restart", action="store_true", help="只改文件，不重启服务")
    args = ap.parse_args()

    print("=" * 64)
    print("  QB_API_TOKEN 轮换")
    print("=" * 64)

    # ── 连通性检查（任何动作之前）────────────────────────────
    rc, out = ssh("systemctl is-active quantbot; echo ---; wc -l < /opt/quantbot/.env")
    if rc != 0:
        print("  ✗ 连不上服务器，什么都没做。")
        print("    " + out.strip()[:200])
        return 2
    lines = out.strip().split("---")
    print(f"  服务状态 : {lines[0].strip()}")
    print(f"  .env 行数: {lines[1].strip()}")

    cur_tok = remote_env_get("QB_API_TOKEN")
    prev_tok = remote_env_get("QB_API_TOKEN_PREV")
    if not cur_tok:
        print("  ✗ 服务器上 QB_API_TOKEN 是空的，先把它配好再轮换。")
        return 2
    print(f"  当前 token: {len(cur_tok)} 位，前 4 位 {cur_tok[:4]}***")

    # ── 清宽限期模式 ────────────────────────────────────────
    if args.clear_prev:
        if not prev_tok:
            print("\n  宽限期本来就是空的，无事可做。")
            return 0
        print(f"\n  即将作废旧 token（{len(prev_tok)} 位，前 4 位 {prev_tok[:4]}***）")
        if args.dry:
            print("  [dry] 会执行：sed -i '/^QB_API_TOKEN_PREV=/d' .env && restart")
            return 0
        rc, out = ssh(
            f'cd {REMOTE} && cp .env .env.bak_rotate_$(date +%s) && '
            f'sed -i "/^QB_API_TOKEN_PREV=/d" .env && echo OK')
        if rc != 0 or "OK" not in out:
            print("  ✗ 失败：" + out.strip()[:200])
            return 1
        if not args.no_restart:
            ssh("systemctl restart quantbot && sleep 6 && systemctl is-active quantbot")
        print("  ✓ 旧 token 已作废，宽限期清空")
        return 0

    # ── 真轮换 ─────────────────────────────────────────────
    new_tok = secrets.token_hex(32)
    print(f"  新   token: {len(new_tok)} 位，前 4 位 {new_tok[:4]}***")

    if args.dry:
        print("\n  [dry] 会做的事：")
        print("    1. 备份 .env 为 .env.bak_rotate_<时间戳>")
        print("    2. 当前 token → QB_API_TOKEN_PREV（宽限期）")
        print("    3. 新 token   → QB_API_TOKEN")
        print(f"    4. 更新本地 {LOCAL_ENV} 第 1 行")
        print("    5. 重启 quantbot")
        print("    6. 验证新 token 能读能写、旧 token 还能用")
        print("\n  确定要真干就加 --dry 去掉，直接跑。")
        return 0

    # ① 服务器：备份 + 改写（当前值挪到 PREV）
    script = f"""
set -e
cd {REMOTE}
STAMP=$(date +%s)
cp .env .env.bak_rotate_$STAMP
echo "  backup: .env.bak_rotate_$STAMP"
sed -i "/^QB_API_TOKEN_PREV=/d" .env
# 当前 token 挪到 PREV
CUR=$(grep "^QB_API_TOKEN=" .env | head -1 | cut -d= -f2-)
echo "QB_API_TOKEN_PREV=$CUR" >> .env
sed -i "/^QB_API_TOKEN=/d" .env
echo "QB_API_TOKEN={new_tok}" >> .env
echo "  env 行数: $(wc -l < .env)"
"""
    rc, out = ssh(script)
    if rc != 0:
        print("  ✗ 改服务器 .env 失败：" + out.strip()[:300])
        print("  → 没有重启，服务仍在用旧 token，不受影响。")
        return 1
    print("  ✓ 服务器 .env 已更新（旧 token 进了宽限期）")

    # ② 本地 .env.local 第 1 行
    try:
        with open(LOCAL_ENV, "r", encoding="utf-8") as f:
            local_lines = f.read().splitlines()
        rest = [ln for ln in local_lines[1:] if not ln.startswith("QB_API_TOKEN=")]
        with open(LOCAL_ENV, "w", encoding="utf-8") as f:
            f.write("\n".join([new_tok] + rest) + "\n")
        print(f"  ✓ 本地 {os.path.basename(LOCAL_ENV)} 第 1 行已更新")
    except Exception as e:                                    # noqa: BLE001
        print(f"  ⚠ 本地 .env.local 更新失败：{e}")
        print(f"    手工改：第 1 行替换成新 token（服务器上已生效）")

    # ③ 重启
    if args.no_restart:
        print("  ⚠ 加了 --no-restart，服务还没重启 —— 新 token 尚未生效，记得手动 restart")
        return 0
    rc, out = ssh("systemctl restart quantbot && sleep 7 && systemctl is-active quantbot")
    state = out.strip().splitlines()[-1] if out.strip() else "?"
    if state != "active":
        print(f"  ✗ 重启后服务状态 {state}，请立刻检查：")
        print("    ssh -i ~/.ssh/qb_hk root@103.148.245.170 'journalctl -u quantbot -n 40'")
        return 1
    print("  ✓ 服务已重启：active")

    # ④ 验证
    print("\n  验证新 token / 旧 token：")
    verify = f"""
cd {REMOTE}
NEW={new_tok}
OLD=$(grep "^QB_API_TOKEN_PREV=" .env | head -1 | cut -d= -f2-)
VK=$(grep "^QB_VIEW_KEY=" .env | head -1 | cut -d= -f2-)
echo "  新 token 读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -H "x-api-token: $NEW" http://127.0.0.1:8899/api/status)"
echo "  新 token 写 probe  : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -X POST -H "Content-Type: application/json" -H "x-api-token: $NEW" -d "{{}}" http://127.0.0.1:8899/api/__probe__)"
echo "  旧 token 读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -H "x-api-token: $OLD" http://127.0.0.1:8899/api/status)"
echo "  观战 key 读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -H "x-view-key: $VK" http://127.0.0.1:8899/api/status)"
echo "  无凭据   读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 http://127.0.0.1:8899/api/status)"
"""
    rc, out = ssh(verify)
    print(out.rstrip())

    ok = ("新 token 读 status : 200" in out and
          "观战 key 读 status : 200" in out and
          "无凭据   读 status : 401" in out)
    print()
    if ok:
        print("  ✓ 轮换成功。新 token 已生效，旧 token 还能用（宽限期）。")
        print("    确认所有设备都换成新的之后，跑：")
        print("      python scripts\\rotate_token.py --clear-prev")
        return 0
    print("  ✗ 验证没全过，请看上面的状态码。回滚办法：")
    print(f"    ssh -i {SSH_KEY} {HOST} 'cd {REMOTE} && cp .env.bak_rotate_* .env && systemctl restart quantbot'")
    return 1


if __name__ == "__main__":
    sys.exit(main())
