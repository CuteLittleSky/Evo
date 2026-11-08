"""从派喵后端拉取 Evo 通知（只读）。

接口：`GET {root}/evo/notices?version=<当前版本>`（对应 ApexPacks `server/api/evo/notices.get.ts`）

只依赖标准库 urllib，不新增依赖。缓存 30 分钟；断网/解析失败时用磁盘上的旧数据，
一份都没有就返回空（绝不抛错、绝不影响 Evo 正常使用）。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request

from cloud.notify import model, store, version

TIMEOUT = 6.0
TTL = 30 * 60.0


def current_version() -> str:
    try:
        import storage  # noqa: PLC0415
        return str(getattr(storage, "APP_VERSION", "") or "")
    except Exception:
        return ""


def endpoint() -> str:
    env = os.environ.get("EVO_NOTIFY_URL")
    if env:
        return env.strip()
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        base = cloud_state.default_server()  # .../apex/api/battlestats
    except Exception:
        base = "https://tools.skycraft.cn/apex/api/battlestats"
    root = base.rsplit("/battlestats", 1)[0] if "/battlestats" in base else base.rstrip("/")
    return root.rstrip("/") + "/evo/notices"


def cached_items() -> list:
    """只读本地缓存（不联网）。面板每次渲染用它，绝不卡网络。"""
    _at, items = store.cache_items()
    return items if isinstance(items, list) else []


def _normalize_list(raw_items, cur_version: str) -> list:
    out = []
    if not isinstance(raw_items, list):
        return out
    for raw in raw_items:
        item = model.normalize_item(raw)
        if not item:
            continue
        if not version.in_range(cur_version, item.get("minVersion"), item.get("maxVersion")):
            continue
        out.append(item)
    out.sort(key=lambda x: (-int(x.get("priority") or 0),))
    return out


def refresh(force: bool = False) -> list:
    """拉一次通知并更新缓存；失败时保留旧缓存。返回当前缓存列表。"""
    at, items = store.cache_items()
    now = time.time()
    if not force and items and (now - float(at or 0.0)) < TTL:
        return items

    cur = current_version()
    url = endpoint()
    sep = "&" if "?" in url else "?"
    url = "%s%sversion=%s" % (url, sep, urllib.parse.quote(cur))
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "EvoTracker/notify", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not json object")
        norm = _normalize_list(data.get("items"), cur)
        store.write_cache(now, norm)
        return norm
    except Exception:
        return items
