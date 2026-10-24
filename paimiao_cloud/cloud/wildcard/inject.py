"""等 live_collect 导入的钩子（和 `cloud/lobby_mode/inject.py` 同一套写法）。

`live_collect` 只在**记录子进程**里被导入，所以钩子要在主进程和子进程都装：
主进程里它只是挂在 sys.meta_path 上一个永不触发的 finder，子进程里才真正生效。
"""
from __future__ import annotations

import sys

_INSTALLED = False


def install():
    """登记「live_collect 导入后接上外卡识别」。幂等。

    **不再自己挂 sys.meta_path finder**（2026-10-24 实测）：Python 只问第一个
    返回 spec 的 finder，四个注入块各挂一个时后面的永远不触发。统一走
    `cloud/importhook.py`。
    """
    global _INSTALLED
    if _INSTALLED:
        return True
    _INSTALLED = True
    try:
        from cloud import importhook  # noqa: PLC0415

        importhook.on_import("live_collect", _after_import, label="外卡模式")
    except Exception:
        _INSTALLED = False
        raise
    return True


def _after_import(module):
    from cloud.wildcard import detect      # noqa: PLC0415

    if detect.install(module):
        _log("[外卡模式] 已接上：总结页卡片上是「最后幸存」「自我急救」的局记成外卡模式")


def _log(message):
    try:
        from cloud import state as cloud_state      # noqa: PLC0415

        cloud_state.log(message)
    except Exception:
        pass
