"""等 live_collect 导入的钩子（和 `cloud/lobby_mode/inject.py`、`cloud/wildcard/inject.py` 同一套写法）。

`live_collect` 只在**记录子进程**里被导入，所以钩子要在主进程和子进程都装：
主进程里它只是登记在 `cloud/importhook.py` 上、永不触发的回调，子进程里才真正生效。
"""
from __future__ import annotations

_INSTALLED = False


def install():
    """登记「live_collect 导入后接上子弹时间识别」。幂等。"""
    global _INSTALLED
    if _INSTALLED:
        return True
    _INSTALLED = True
    try:
        from cloud import importhook  # noqa: PLC0415

        importhook.on_import("live_collect", _after_import, label="子弹时间")
    except Exception:
        _INSTALLED = False
        raise
    return True


def _after_import(module):
    from cloud.bullet import detect      # noqa: PLC0415

    if detect.install(module):
        _log("[子弹时间] 已接上：总结页卡片上有「降级近战攻击」的局记成子弹时间")


def _log(message):
    try:
        from cloud import state as cloud_state      # noqa: PLC0415

        cloud_state.log(message)
    except Exception:
        pass
