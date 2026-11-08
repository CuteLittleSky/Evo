"""通知的本地状态：已读、Windows 通知已发、以及拉取缓存。

存 `<数据目录>/cloud-sync/notify_state.json`，与云同步状态分开（那个是业务数据，
这个是运营通知的已读记录，坏了也不影响记录）。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

_lock = threading.Lock()


def _path() -> Path | None:
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        return Path(cloud_state.data_dir()) / "notify_state.json"
    except Exception:
        return None


def _empty() -> dict:
    return {"seen": {}, "win_seen": {}, "cache_at": 0.0, "cache_items": []}


def load() -> dict:
    path = _path()
    data = _empty()
    if path is None:
        return data
    try:
        with path.open("r", encoding="utf-8") as fp:
            raw = json.load(fp)
        if isinstance(raw, dict):
            data["seen"] = raw.get("seen") if isinstance(raw.get("seen"), dict) else {}
            data["win_seen"] = raw.get("win_seen") if isinstance(raw.get("win_seen"), dict) else {}
            data["cache_at"] = float(raw.get("cache_at") or 0.0)
            data["cache_items"] = raw.get("cache_items") if isinstance(raw.get("cache_items"), list) else []
    except Exception:
        pass
    return data


def save(data: dict) -> None:
    path = _path()
    if path is None:
        return
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".notify-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(data, fp, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        pass


def is_seen(notice_id: str) -> bool:
    return notice_id in (load().get("seen") or {})


def mark_seen(notice_id: str, action: str = "dismiss") -> None:
    data = load()
    data.setdefault("seen", {})[notice_id] = {"at": __import__("time").time(), "action": action}
    save(data)


def win_seen(notice_id: str) -> bool:
    return notice_id in (load().get("win_seen") or {})


def mark_win_seen(notice_id: str) -> None:
    data = load()
    data.setdefault("win_seen", {})[notice_id] = __import__("time").time()
    save(data)


def cache_items():
    data = load()
    return data.get("cache_at") or 0.0, data.get("cache_items") or []


def write_cache(at: float, items: list) -> None:
    data = load()
    data["cache_at"] = float(at)
    data["cache_items"] = items
    save(data)
