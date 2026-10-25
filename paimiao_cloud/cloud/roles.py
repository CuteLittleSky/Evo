from __future__ import annotations

import os
import sys

ROLE_ENV = "EVO_PAIMIAO_ROLE"

_MAIN_ENTRIES = (
    os.path.join("code", "evo_app.py"),
    os.path.join("code", "evo_app.exe"),
)

_role = ""

def set_role(role: str) -> None:

    global _role
    if not _role:
        _role = role or ""

def is_child_process() -> bool:

    mod = sys.modules.get("multiprocessing")
    if mod is None:
        return False
    try:
        return mod.parent_process() is not None
    except Exception:
        return False

def is_evo_main() -> bool:

    if _role or os.environ.get(ROLE_ENV):
        return False
    if is_child_process():
        return False
    try:
        entry = os.path.abspath(sys.argv[0] or "")
    except Exception:
        entry = ""
    if entry.endswith(_MAIN_ENTRIES):
        return True

    return bool(getattr(sys, "frozen", False))

def role() -> str:

    return _role or os.environ.get(ROLE_ENV, "")
