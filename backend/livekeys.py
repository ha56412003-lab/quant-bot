"""热加载凭据 —— 改文件立即生效，不用重启服务。

## 为什么需要这个

`config.VIEW_KEY` / `API_TOKEN` 是 `os.environ.get()` 在模块 import 时读一次，
改了 `.env` 必须 `systemctl restart quantbot` 才生效。

但浩哥 2026-11-11 明确要求：**换观战 key 不能重启后端服务**。
（重启那十几秒里持仓不监控、止损不触发，接了实盘就是裸奔。）

## 做法

凭据放独立文件 `data/livekeys`（`key=value` 每行一条），
每次请求比对文件的 `st_mtime_ns`，**变了才重读**（没变几乎零开销）。

- 文件放在 `data/` 目录，和 `quantbot.db` 同级 —— 不随 `backend/` 代码部署被覆盖
- 文件不存在 → **回退到环境变量**（`QB_VIEW_KEY` / `QB_API_TOKEN` / `QB_API_TOKEN_PREV`）
  → 老部署、没写过这个文件，行为完全不变，不会把人锁在门外
- 写入方必须**原子写**（临时文件 + os.replace），否则可能读到半截内容

## 用法

```python
from . import livekeys

livekeys.view_key()        # 观战只读 key
livekeys.api_token()       # 操作 token
livekeys.api_token_prev()  # 轮换宽限期用的旧 token
livekeys.get("QB_XXX")     # 任意键
```
"""
import os
import threading

from . import config

# data/livekeys —— 和 quantbot.db 同一个目录
_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "livekeys"
)
KEYS_FILE = os.environ.get("QB_KEYS_FILE") or _DEFAULT_PATH

_lock = threading.Lock()
_cache = {"mtime_ns": -1, "data": {}}


def _load():
    """返回 (data_dict, mtime_ns)。mtime 没变就返回缓存。"""
    try:
        st = os.stat(KEYS_FILE)
    except OSError:
        return {}, -1
    with _lock:
        if st.st_mtime_ns == _cache["mtime_ns"]:
            return _cache["data"], st.st_mtime_ns
        data = {}
        try:
            with open(KEYS_FILE, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith("#") or "=" not in ln:
                        continue
                    k, v = ln.split("=", 1)
                    data[k.strip()] = v.strip()
        except OSError:
            return _cache["data"], _cache["mtime_ns"]
        _cache["mtime_ns"] = st.st_mtime_ns
        _cache["data"] = data
        return data, st.st_mtime_ns


def get(name, fallback=""):
    """取一个键。

    ★ 语义（2026-11-11 修过的一个 BUG）：
      文件里【明确写了】这个键（哪怕值是空的）→ 以文件为准，返回空字符串。
      这样「把 QB_VIEW_KEY 留空」能真正关掉读鉴权，而不是被 .env 的旧值顶回去。
      文件里【没有】这个键 → 回退到 fallback（环境变量）。
    """
    data, _ = _load()
    if name in data:
        return data[name]
    return fallback


def view_key():
    """观战只读 key。留空 = 读接口不鉴权（旧行为）。"""
    return get("QB_VIEW_KEY", config.VIEW_KEY)


def api_token():
    """写操作 token。"""
    return get("QB_API_TOKEN", config.API_TOKEN)


def api_token_prev():
    """轮换宽限期的旧 token（可为空）。"""
    return get("QB_API_TOKEN_PREV", config.API_TOKEN_PREV)


def write(values: dict):
    """原子写入凭据文件（临时文件 + os.replace，读者不会读到半截）。

    只更新传入的键，其余键保留原值。传空字符串 = 把这个键写成空
    （有意义的操作：等于关掉对应鉴权）。

    ★ 当前没有任何代码路径调用它 —— 实际写入方是 scripts/rotate_view_key.py
      和 rotate_token.py，它们通过 SSH 在服务器上直接改文件。
      保留这个函数是为了让模块的读写 API 对称（以后要做「网页上改 key」
      就直接能用），别当死代码删掉。
    """
    cur, _ = _load()
    merged = dict(cur)
    for k, v in values.items():
        merged[k] = (v or "").strip()
    os.makedirs(os.path.dirname(KEYS_FILE), exist_ok=True)
    tmp = KEYS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 热加载凭据 —— 改这个文件立即生效，不用重启服务\n")
        f.write("# 由 scripts/rotate_view_key.py / rotate_token.py 维护，手改也行\n")
        for k in sorted(merged):
            # ★ 空值也要写出来（「把这个键留空」是有意义的操作：关掉对应鉴权）
            f.write(f"{k}={merged[k]}\n")
    os.replace(tmp, KEYS_FILE)          # 原子替换
    # 让下一次 _load() 强制重读（os.replace 会改 mtime，但同秒内可能相同）
    with _lock:
        _cache["mtime_ns"] = -1
    return merged
