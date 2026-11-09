# -*- coding: utf-8 -*-
"""一次性跑完 tests/ 下所有测试脚本。

这些测试是「脚本式」的（每个文件自带 main() 和 sys.exit），
不是 pytest 风格，所以用一个简单的 runner 统一跑，不依赖 pytest。

用法：
    python tests/run_all.py              # 跑全部
    python tests/run_all.py gates        # 只跑文件名含 gates 的
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PY = sys.executable


def main():
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(
        f for f in os.listdir(_HERE)
        if f.startswith("test_") and f.endswith(".py") and pattern in f
    )
    if not files:
        print(f"没找到匹配 '{pattern}' 的测试")
        return 1

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    failed = []
    for f in files:
        r = subprocess.run(
            [_PY, os.path.join(_HERE, f)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=_HERE,
        )
        ok = r.returncode == 0
        print(f"  {'OK  ' if ok else 'FAIL'} {f}")
        if not ok:
            failed.append(f)
            tail = (r.stdout or "") + (r.stderr or "")
            print("\n".join("       " + ln for ln in tail.strip().split("\n")[-12:]))

    print()
    print(f"  {len(files)} 个文件，失败 {len(failed)} 个")
    if failed:
        print(f"  失败：{', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
