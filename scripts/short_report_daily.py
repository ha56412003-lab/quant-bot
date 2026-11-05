"""空头验证日报 —— 定时任务入口（每天 UTC 0:10 = 北京 8:10）。

被 Hermes cron 以 no_agent 模式调用：stdout 即投递内容。
本脚本自己负责推飞书，所以两条通道都有：
  stdout → 桌面端聊天
  内部 _push() → 飞书机器人「嘿咻」
"""
import os
import subprocess
import sys

ROOT = r"E:\quant-bot"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

env = dict(os.environ, PYTHONIOENCODING="utf-8")
r = subprocess.run(
    [PY, os.path.join(ROOT, "scripts", "short_report.py"), "--push"],
    cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    timeout=300,
)
out = (r.stdout or "").strip()
err = (r.stderr or "").strip()

if r.returncode != 0 or not out:
    print("⚠ 空头验证日报生成失败")
    if err:
        print(err[-800:])
    sys.exit(0)

# short_report.py 的 stdout 混了 "→ 飞书推送: 成功" 这类尾巴，原样投递即可
print(out)
