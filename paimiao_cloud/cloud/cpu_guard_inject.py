from __future__ import annotations

_INSTALLED = False

def install():

    global _INSTALLED
    if _INSTALLED:
        return True
    _INSTALLED = True
    try:
        from cloud import importhook

        importhook.on_import("live_collect", _after_import, label="OpenCV 线程数")
    except Exception:
        _INSTALLED = False
        raise
    return True

def _after_import(module):
    from cloud import cpu_guard

    cpu_guard.install(module)
