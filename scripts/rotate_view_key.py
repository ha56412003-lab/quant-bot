"""轮换观战 key（QB_VIEW_KEY）。

浩哥 2026-11-11 选 A：观战链接永久有效，需要时手动换 key 让它失效。

这个脚本做的事：
  1. 生成一个新的 64 位十六进制观战 key
  2. 写进服务器 /opt/quantbot/.env 的 QB_VIEW_KEY
  3. 更新本地 E:\\quant-bot\\.env.local 第 2 行
  4. 重启服务器 quantbot
  5. 验证：新 key 能读、旧 key 立刻 401

★ 换完之后：
   · 所有旧的观战链接立刻失效（朋友点开会看到「观战链接可能已失效」）
   · 你自己的「量化看板」不受影响（那是操作 token，另一把钥匙）
   · 你需要重新生成链接发给朋友（双击桌面「生成观战链接」图标）

用法：
  .venv\\Scripts\\python.exe scripts\\rotate_view_key.py --dry    # 预演
  .venv\\Scripts\\python.exe scripts\\rotate_view_key.py          # 真换
  .venv\\Scripts\\python.exe scripts\\rotate_view_key.py --show   # 只看当前的 key 状态
"""
import argparse
import os
import secrets
import shutil
import subprocess
import sys
import time

# ★ 被 vbs 以隐藏窗口调用时看不到 stdout，必须同时写日志文件，
#   否则失败时浩哥和我都无从查起。
LOGFILE = r"E:\quant-bot\.vk_rotate.log"


class _Tee:
    """同时写到原 stdout 和日志文件（原 stdout 可能为 None）。"""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
                st.flush()
            except Exception:                                  # noqa: BLE001
                pass

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:                                  # noqa: BLE001
                pass


def _install_tee():
    try:
        # ★ 必须 GBK：VBScript 的 OpenTextFile 只能按 ANSI 读，UTF-8 会变乱码
        lf = open(LOGFILE, "w", encoding="gbk", errors="replace")
    except Exception:                                          # noqa: BLE001
        return
    sys.stdout = _Tee(sys.__stdout__, lf)
    sys.stderr = _Tee(sys.__stderr__, lf)


# ★ 直接调 ssh.exe + 参数列表，不经过 shell（见 rotate_token.py 里的同款坑）
SSH = shutil.which("ssh") or "ssh"
SSH_KEY = os.path.expanduser("~/.ssh/qb_hk")
HOST = "root@103.148.245.170"
REMOTE = "/opt/quantbot"
LOCAL_ENV = r"E:\quant-bot\.env.local"
HISTORY = r"E:\quant-bot\.view_key_history.txt"


def ssh(script, timeout=180):
    p = subprocess.run(
        [SSH, "-i", SSH_KEY,
         "-o", "ConnectTimeout=20",
         "-o", "StrictHostKeyChecking=no",
         "-o", "BatchMode=yes",
         HOST, script],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def remote_get(name):
    rc, out = ssh(f'grep "^{name}=" {REMOTE}/.env | head -1 | cut -d= -f2-')
    return out.strip() if rc == 0 else ""


def show_status():
    print("=" * 64)
    print("  当前观战 key 状态")
    print("=" * 64)
    vk = remote_get("QB_VIEW_KEY")
    if not vk:
        print("  [X] 服务器上没配 QB_VIEW_KEY —— 说明读接口完全没有鉴权！")
        return 1
    print(f"  长度   : {len(vk)} 位")
    print(f"  前 4 位: {vk[:4]}***")

    # 本地是否一致
    try:
        lines = open(LOCAL_ENV, encoding="utf-8").read().splitlines()
        lv = ""
        for ln in lines:
            if ln.startswith("QB_VIEW_KEY="):
                lv = ln.split("=", 1)[1].strip()
        print(f"  本地一致: {'[OK] 是' if lv == vk else '[X] 不一致（本地 ' + str(len(lv)) + ' 位）'}")
    except Exception:                                          # noqa: BLE001
        print("  本地一致: 读不到 .env.local")

    # 换 key 历史
    if os.path.exists(HISTORY):
        hist = [ln.strip() for ln in open(HISTORY, encoding="utf-8") if ln.strip()]
        if hist:
            last = hist[-1]
            stamp = last.split("\t")[0] if "\t" in last else ""
            print(f"  上次换 : {stamp}")
            try:
                t = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
                days = (time.time() - t) / 86400
                print(f"  已用   : {days:.1f} 天")
                if days > 180:
                    print("           [!] 超过半年了，考虑换一把")
            except Exception:                                  # noqa: BLE001
                pass
            print(f"  换过   : {len(hist)} 次")
    else:
        print("  上次换 : 无记录（这把是 2026-11-11 首次建立的）")

    # 实测
    rc, out = ssh(
        f'cd {REMOTE} && VK=$(grep "^QB_VIEW_KEY=" .env | cut -d= -f2-) && '
        f'echo "  新读测试: $(curl -s -o /dev/null -w %{{http_code}} --noproxy "*" '
        f'-H "x-view-key: $VK" --max-time 8 http://127.0.0.1:8899/api/status)"')
    print(out.strip().splitlines()[-1] if out.strip() else "  实测: 失败")
    return 0


def main():
    _install_tee()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只预演")
    ap.add_argument("--show", action="store_true", help="只看当前状态")
    ap.add_argument("--no-restart", action="store_true", help="只改文件不重启")
    args = ap.parse_args()

    if args.show:
        return show_status()

    print("=" * 64)
    print("  观战 key 轮换（换完所有旧观战链接立刻失效）")
    print("=" * 64)

    # ── 连通性 ──────────────────────────────────────────────
    rc, out = ssh("systemctl is-active quantbot")
    if rc != 0 or "active" not in out:
        print("  [X] 连不上服务器或服务没跑，什么都没做。")
        print("    " + out.strip()[:200])
        return 2
    print("  服务状态: active")

    old_vk = remote_get("QB_VIEW_KEY")
    if old_vk:
        print(f"  旧 key  : {len(old_vk)} 位，前 4 位 {old_vk[:4]}***")
    else:
        print("  旧 key  : 无（读接口目前没有鉴权）")

    new_vk = secrets.token_hex(32)
    print(f"  新 key  : {len(new_vk)} 位，前 4 位 {new_vk[:4]}***")

    if args.dry:
        print("\n  [dry] 会做的事：")
        print("    1. 备份 .env 为 .env.bak_vk_<时间戳>")
        print("    2. 新 key 写进 QB_VIEW_KEY")
        print(f"    3. 更新本地 {LOCAL_ENV} 第 2 行")
        print("    4. 重启 quantbot")
        print("    5. 验证：新 key 200 / 旧 key 401")
        print("\n  [!] 换完之后所有旧观战链接立刻失效，需要重新发给朋友。")
        return 0

    # ① 服务器
    script = f"""
set -e
cd {REMOTE}
STAMP=$(date +%s)
cp .env .env.bak_vk_$STAMP
echo "  backup: .env.bak_vk_$STAMP"
sed -i "/^QB_VIEW_KEY=/d" .env
echo "QB_VIEW_KEY={new_vk}" >> .env
echo "  env 行数: $(wc -l < .env)"
"""
    rc, out = ssh(script)
    if rc != 0:
        print("  [X] 改服务器 .env 失败：" + out.strip()[:300])
        print("  → 没重启，旧 key 仍然有效，服务不受影响。")
        return 1
    print("  [OK] 服务器 .env 已更新")

    # ② 本地 .env.local 第 2 行
    try:
        lines = open(LOCAL_ENV, encoding="utf-8").read().splitlines()
        rest = [ln for ln in lines if not ln.startswith("QB_VIEW_KEY=")]
        with open(LOCAL_ENV, "w", encoding="utf-8") as f:
            f.write("\n".join(rest + [f"QB_VIEW_KEY={new_vk}"]) + "\n")
        print(f"  [OK] 本地 {os.path.basename(LOCAL_ENV)} 已更新")
    except Exception as e:                                     # noqa: BLE001
        print(f"  [!] 本地更新失败：{e}")

    # ③ 重启
    if not args.no_restart:
        rc, out = ssh("systemctl restart quantbot && sleep 7 && systemctl is-active quantbot")
        state = out.strip().splitlines()[-1] if out.strip() else "?"
        if state != "active":
            print(f"  [X] 重启后状态 {state}，立刻检查：")
            print(f"    ssh -i {SSH_KEY} {HOST} 'journalctl -u quantbot -n 40'")
            return 1
        print("  [OK] 服务已重启：active")

    # ④ 验证
    print("\n  验证：")
    verify = f"""
cd {REMOTE}
NEW={new_vk}
OLD={old_vk or "NONE"}
VK=$(grep "^QB_VIEW_KEY=" .env | cut -d= -f2-)
TK=$(grep "^QB_API_TOKEN=" .env | cut -d= -f2-)
echo "  新观战 key 读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -H "x-view-key: $NEW" http://127.0.0.1:8899/api/status)"
if [ "$OLD" != "NONE" ]; then
  echo "  旧观战 key 读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -H "x-view-key: $OLD" http://127.0.0.1:8899/api/status)"
fi
echo "  新观战 key 去写      : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -X POST -H "Content-Type: application/json" -H "x-view-key: $NEW" -d "{{}}" http://127.0.0.1:8899/api/mode)"
echo "  操作 token 读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 -H "x-api-token: $TK" http://127.0.0.1:8899/api/status)"
echo "  无凭据     读 status : $(curl -s -o /dev/null -w "%{{http_code}}" --noproxy "*" --max-time 8 http://127.0.0.1:8899/api/status)"
"""
    rc, out = ssh(verify)
    print(out.rstrip())

    ok = ("新观战 key 读 status : 200" in out and
          "新观战 key 去写      : 401" in out and
          "操作 token 读 status : 200" in out and
          "无凭据     读 status : 401" in out and
          (not old_vk or "旧观战 key 读 status : 401" in out))

    # ⑤ 记历史
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(f"{stamp}\t{new_vk[:4]}***\n")
    except Exception:                                          # noqa: BLE001
        pass

    print()
    if ok:
        print("  [OK] 换完了。")
        print("    · 所有旧观战链接现在都是 401，朋友点开会提示「链接已失效」")
        print("    · 你自己的「量化看板」不受影响")
        print("    · 重新发链接：双击桌面「生成观战链接」图标")
        return 0
    print("  [!] 验证没全过，请看上面状态码。回滚：")
    print(f"    ssh -i {SSH_KEY} {HOST} 'cd {REMOTE} && cp .env.bak_vk_* .env && systemctl restart quantbot'")
    return 1


if __name__ == "__main__":
    sys.exit(main())
