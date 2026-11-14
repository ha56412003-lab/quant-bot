"""策略参数模板（presets）存储 —— 独立文件，**不**塞进 strategies.yaml

为什么单独一个文件
─────────────────────────────────────────────────────────────
strategies.yaml 是策略加载的唯一入口，服务器上还被网页改过（风控 10/8/9 等）。
往里加一个 `presets:` 段，一旦 round-trip 写回出岔子，整条策略都起不来。
模板坏了顶多「模板列表用不了」，策略照跑 —— 风险隔离。

实时生效靠什么
─────────────────────────────────────────────────────────────
**不靠这个文件。** 这个文件只是「一组值的存放处」。点「载入模板」时，
后端把模板里的值写进 strategies.yaml 的 params（走 config.set_strategy_params，
bounds 校验 + ruamel 写回），engine 每轮 config.load_strategies() 重读 → 下一轮就生效。
所以模板存在哪个文件、什么格式，跟「实时生效」完全无关。

文件格式（与 strategies.yaml 同目录：本地 E:\\quant-bot\\param_presets.yaml、
服务器 /opt/quantbot/param_presets.yaml）
─────────────────────────────────────────────────────────────
    version: 1
    presets:
      S15_CE_ZLSMA:
        - id: p1794632700
          name: 浩哥自定义版1
          note: 2026-11-14 线上快照
          created_ts: 1794632700
          auto: false          # true = 切换前自动备份（单槽，会被覆盖）
          params:
            zlsma_ext_max: 1.5
            ...

写盘：原子写（同目录 tmp → os.replace），LF，utf-8。
"""
import io
import os
import time

import yaml

from .config import STRATEGIES_PATH

PRESETS_PATH = STRATEGIES_PATH.parent / "param_presets.yaml"

MAX_PER_STRATEGY = 10          # 每策略最多存这么多套，防无限堆积
AUTO_ID = "auto_backup"        # 切换前自动备份的单槽 id
AUTO_NAME = "切换前自动备份"
DEFAULTS_ID = "__defaults__"   # 内置「默认版（代码默认值）」，不存在文件里


def _empty():
    return {"version": 1, "presets": {}}


def _load():
    try:
        raw = PRESETS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        # 文件坏了：不抛给调用方，按空处理（策略不能因为模板文件坏掉就起不来）
        return _empty()
    if not isinstance(doc, dict):
        return _empty()
    doc.setdefault("version", 1)
    if not isinstance(doc.get("presets"), dict):
        doc["presets"] = {}
    return doc


def _save(doc):
    """原子写：tmp → replace。LF、utf-8、不排序（保持人读顺序）。"""
    PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS_PATH.with_name(PRESETS_PATH.name + ".tmp")
    buf = io.StringIO()
    yaml.safe_dump(doc, buf, allow_unicode=True, sort_keys=False, default_flow_style=False)
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(buf.getvalue())
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PRESETS_PATH)


def _bucket(doc, sid, create=False):
    """拿某策略的模板列表 —— 必须是 doc 里那个 list 对象本身。

    ★ 2026-11-14 踩过：原来写成 `return doc["presets"].get(sid) or []`，
      空列表是假值 → 返回了一个**新**列表，append 全丢在临时对象里，
      文件里只剩 `S15_CE_ZLSMA: []`，保存成功但读不回来。
    """
    if create:
        doc["presets"].setdefault(sid, [])
    cur = doc["presets"].get(sid)
    return cur if isinstance(cur, list) else []


def list_for(sid):
    """某策略的所有模板（新的在前；自动备份排最后）。"""
    items = list(_bucket(_load(), sid))
    items.sort(key=lambda p: (bool(p.get("auto")), -(p.get("created_ts") or 0)))
    return items


def get(sid, pid):
    for p in _bucket(_load(), sid):
        if p.get("id") == pid:
            return p
    return None


def _new_id(sid, doc):
    ts = int(time.time())
    n = 1
    ids = {p.get("id") for p in _bucket(doc, sid)}
    while f"p{ts}-{n}" in ids:
        n += 1
    return f"p{ts}-{n}"


def create(sid, name, params, note=""):
    """存一套新模板。返回存下来的那条。"""
    doc = _load()
    bucket = _bucket(doc, sid, create=True)
    manual = [p for p in bucket if not p.get("auto")]
    if len(manual) >= MAX_PER_STRATEGY:
        raise ValueError(f"模板最多存 {MAX_PER_STRATEGY} 套，先删掉不用的一套")
    item = {"id": _new_id(sid, doc), "name": str(name)[:40],
            "note": str(note or "")[:120], "created_ts": int(time.time()),
            "auto": False, "params": dict(params or {})}
    bucket.append(item)
    _save(doc)
    return item


def backup(sid, params):
    """切换前把当前值存进单槽自动备份（覆盖旧的）。"""
    doc = _load()
    bucket = _bucket(doc, sid, create=True)
    item = {"id": AUTO_ID, "name": AUTO_NAME,
            "note": "每次切换模板前自动写入，只保留最近一次", "created_ts": int(time.time()),
            "auto": True, "params": dict(params or {})}
    for i, p in enumerate(bucket):
        if p.get("id") == AUTO_ID:
            bucket[i] = item
            break
    else:
        bucket.append(item)
    _save(doc)
    return item


def delete(sid, pid):
    doc = _load()
    bucket = _bucket(doc, sid)
    keep = [p for p in bucket if p.get("id") != pid]
    if len(keep) == len(bucket):
        return False
    doc["presets"][sid] = keep
    _save(doc)
    return True
