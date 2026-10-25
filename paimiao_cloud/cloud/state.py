from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_NAME = "state.json"
STATE_FOLDER = "cloud-sync"
LOG_NAME = "cloud.log"

DEFAULT_SERVER = "https://tools.skycraft.cn/apex/api/battlestats"

def default_server() -> str:

    return (os.environ.get("EVO_CLOUD_SERVER") or DEFAULT_SERVER).rstrip("/")

_PAIR_SUFFIX = "/battlestats"

def pair_server(snapshot) -> str:

    base = (snapshot.get("pair_server") or "").rstrip("/")
    if base:
        return base
    root = (os.environ.get("EVO_CLOUD_PAIR_SERVER") or "").rstrip("/")
    if root:
        return root
    server = (snapshot.get("server") or default_server()).rstrip("/")
    if server.endswith(_PAIR_SUFFIX):
        return server[: -len(_PAIR_SUFFIX)]
    return server

CONSENT_VERSION = "2026-10-22b"

def data_dir() -> Path:

    try:
        from paths import DB_PATH
        return Path(DB_PATH).parent / STATE_FOLDER
    except Exception:
        base = os.environ.get("EVO_DATA_DIR")
        root = Path(base) if base else Path(__file__).resolve().parent.parent.parent / "data"
        return root / STATE_FOLDER

def log_path() -> Path:
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / LOG_NAME

def state_path() -> Path:
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / STATE_NAME

DEFAULTS = {
    "consent": {},
    "server": "",
    "device": {},
    "enabled": True,
    "pair_code": "",
    "pair_hint": "",
    "pair_ticket": "",
    "pair_qr_ticket": "",
    "pair_qr_url": "",
    "pair_mini_code": "",
    "pair_mini_code_mime": "",
    "pair_qr_revoked": False,
    "pair_code_at": 0.0,
    "pair_expires_ms": 0,
    "hashes": {},
    "stats": {"uploaded": 0, "failed": 0, "last_ok_at": "", "last_error": ""},

    "notice_seen": "",
}

def load() -> dict:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in raw.items() if k in DEFAULTS})
    merged["stats"] = {**DEFAULTS["stats"], **(raw.get("stats") or {})}
    merged["device"] = raw.get("device") or {}
    merged["hashes"] = raw.get("hashes") or {}
    merged["consent"] = raw.get("consent") or {}
    merged["notice_seen"] = str(raw.get("notice_seen") or "")

    merged["server"] = default_server()
    return merged

def save(state: dict) -> None:

    path = state_path()
    handle, temp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise

def agreed(snapshot: dict) -> bool:

    consent = snapshot.get("consent") or {}
    return bool(consent.get("agreed")) and consent.get("version") == CONSENT_VERSION

def paired(snapshot: dict) -> bool:
    return bool((snapshot.get("device") or {}).get("token"))

def log(message: str) -> None:
    try:
        import datetime
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path(), "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass
