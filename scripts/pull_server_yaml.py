"""从服务器拉 strategies.yaml —— 回测前跑一次，保证回测口径与线上一致

为什么要这个工具
----------------
2026-11-11 起，**服务器是唯一准绳**：参数直接在服务器网页上改，改完 0.5 秒生效
（`config.load_strategies()` 无缓存，`engine` 每轮 0.5s 重读文件）。
本地这份 `strategies.yaml` 只是「回测用的副本」，随时可能是旧的。

不拉一次就回测 = 拿旧参数算新结论 = 白干。

铁律（方向不要搞反）
--------------------
- 同步代码上服务器：只传 `backend/` 与 `frontend-v2/dist`，**永不传 strategies.yaml**
- 本工具是**反方向**：从服务器往本地拉，供回测使用

用法
----
    python scripts/pull_server_yaml.py            # 拉取 + 显示参数差异 + 备份旧文件
    python scripts/pull_server_yaml.py --dry      # 只看差异，不写任何文件
    python scripts/pull_server_yaml.py --force    # 无差异也重写（换行符归一）

退出码：0 = 成功或无需变更；1 = 连接/校验失败（本地文件一个字节都没动）
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL = os.path.join(ROOT, "strategies.yaml")
BACKUP_DIR = os.path.join(ROOT, ".yaml_backups")

HOST = "root@103.148.245.170"
KEY = os.path.join(os.path.expanduser("~"), ".ssh", "qb_hk")
REMOTE = "/opt/quantbot/strategies.yaml"
TIMEOUT = 25


def fetch_remote():
    """ssh 读服务器 yaml 全文。失败返回 (None, 原因)。"""
    if not os.path.exists(KEY):
        return None, "私钥不存在：%s" % KEY
    cmd = [
        "ssh", "-i", KEY, "-o", "ConnectTimeout=15",
        "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        HOST, "cat %s" % REMOTE,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "SSH 超时（%ds）" % TIMEOUT
    except Exception as e:                                    # noqa: BLE001
        return None, "SSH 调用失败：%s" % e
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        return None, "SSH 返回 %d：%s" % (r.returncode, err[:200])
    text = r.stdout.decode("utf-8", "replace")
    if not text.strip():
        return None, "服务器返回空内容"
    return text, None


def flat(d, prefix=""):
    """把嵌套 dict 摊平成 {点号路径: 值}，方便逐参数比对。"""
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flat(v, "%s%s." % (prefix, k)))
    elif isinstance(d, list):
        out[prefix.rstrip(".")] = json.dumps(d, ensure_ascii=False, sort_keys=True)
    else:
        out[prefix.rstrip(".")] = d
    return out


def show_diff(old, new):
    """逐参数打印差异。返回差异条数。"""
    fo, fn = flat(old), flat(new)
    keys = sorted(set(fo) | set(fn))
    rows = []
    for k in keys:
        a, b = fo.get(k, "<不存在>"), fn.get(k, "<不存在>")
        if a == b:
            continue
        rows.append((k, a, b))
    if not rows:
        print("  参数：完全一致（0 处差异）")
        return 0

    print("  参数差异 %d 处（本地 → 服务器）：" % len(rows))
    print()
    print("  %-46s %-16s %-16s" % ("参数", "本地(回测用)", "服务器(准绳)"))
    print("  " + "-" * 80)
    for k, a, b in rows:
        print("  %-46s %-16s %-16s" % (k[:46], str(a)[:16], str(b)[:16]))
    print()
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="从服务器拉 strategies.yaml（回测口径对齐）")
    ap.add_argument("--dry", action="store_true", help="只看差异，不写文件")
    ap.add_argument("--force", action="store_true", help="无差异也重写")
    args = ap.parse_args()

    print("=" * 84)
    print("  从服务器拉 strategies.yaml —— 回测口径对齐")
    print("=" * 84)
    print("  服务器：%s:%s" % (HOST, REMOTE))
    print("  本地　：%s" % LOCAL)
    print()

    # ---------- 1. 拉 ----------
    print("[1/4] 连接服务器...")
    text, err = fetch_remote()
    if text is None:
        print("  ✗ %s" % err)
        print()
        print("  本地文件未做任何改动。请检查网络 / 私钥后重试。")
        return 1
    print("  ✓ 已取回 %d 字节" % len(text))

    # ---------- 2. 校验 ----------
    print()
    print("[2/4] 校验内容...")
    try:
        new_data = yaml.safe_load(text)
    except Exception as e:                                    # noqa: BLE001
        print("  ✗ 服务器 yaml 解析失败：%s" % e)
        print("  本地文件未做任何改动。")
        return 1
    if not isinstance(new_data, dict):
        print("  ✗ 服务器 yaml 顶层不是字典（拿到的是 %s）" % type(new_data).__name__)
        print("  本地文件未做任何改动。")
        return 1
    n_strat = len(new_data.get("strategies") or {})
    print("  ✓ YAML 合法，%d 个顶层键，%d 个策略" % (len(new_data), n_strat))

    # ---------- 3. 比对 ----------
    print()
    print("[3/4] 与本地比对...")
    old_data = {}
    if os.path.exists(LOCAL):
        try:
            old_data = yaml.safe_load(io.open(LOCAL, encoding="utf-8")) or {}
        except Exception as e:                                # noqa: BLE001
            print("  ! 本地 yaml 解析失败（将以服务器版覆盖）：%s" % e)
            old_data = {}
    else:
        print("  ! 本地没有 strategies.yaml，将新建")
    ndiff = show_diff(old_data, new_data)

    if args.dry:
        print("  --dry：未写入任何文件。")
        print()
        print("  确认无误后去掉 --dry 再跑一次即可覆盖本地。")
        return 0

    if ndiff == 0 and not args.force:
        print("  本地已与服务器一致，无需覆盖。")
        return 0

    # ---------- 4. 备份 + 覆盖 ----------
    print("[4/4] 备份并写入...")
    bak = None
    if os.path.exists(LOCAL):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%m%d_%H%M%S")
        bak = os.path.join(BACKUP_DIR, "strategies.yaml.bak_%s" % stamp)
        shutil.copy2(LOCAL, bak)
        print("  旧文件已备份 → %s" % bak)

    # 统一 LF 结尾（跨平台 diff 不产生假差异）
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    if not body.endswith("\n"):
        body += "\n"
    with io.open(LOCAL, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)

    # 回读校验
    chk = yaml.safe_load(io.open(LOCAL, encoding="utf-8"))
    if not isinstance(chk, dict) or len(chk) != len(new_data):
        where = bak if bak else "（无备份：本地原本没有该文件）"
        print("  ✗ 写入后校验失败！请从备份恢复：%s" % where)
        return 1

    print("  ✓ 已写入本地 %d 字节" % os.path.getsize(LOCAL))
    print()
    print("=" * 84)
    print("  完成 —— 本地 yaml 现在与服务器一致，可以开始回测了。")
    print("=" * 84)
    print()
    print("  提示：本地 strategies.yaml 现在显示为 git 已修改，这是正常的")
    print("        （它现在是服务器定档的快照）。不要把这份回传到服务器。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
