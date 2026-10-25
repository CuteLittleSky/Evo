"""OBS 源投影的几个开关（存进 Evo 自己的设置文件，不另起一个）。

为什么不自己写一份配置文件：上游 `app_settings` 已经把设置存在 Evo 的数据目录里，
而且面板每次请求都会重新读 —— 记录子进程是单独一个进程，改完几秒内就能看到。

三个键：
    obs_enabled    要不要让 Evo 去截 OBS 投影（默认 False = 上游行为，截整屏）
    obs_source     画面来源："screen"（截整屏，上游默认）/ "projector"（截 OBS 投影那块屏）
                   —— 保留位：将来要接虚拟摄像头时这里加一个 "camera"
    obs_pause_when_off  投影消失时要不要暂停记录（默认 True）

注意：这里只做**存取**，不判断能不能用。判断在 `projector.describe()`。
"""
from __future__ import annotations

from cloud import state as cloud_state

ENABLED_KEY = "obs_projector_enabled"
SOURCE_KEY = "obs_capture_source"
PAUSE_KEY = "obs_pause_when_projector_closed"

# 来源的取值
SOURCE_SCREEN = "screen"
SOURCE_PROJECTOR = "projector"
SOURCES = (SOURCE_SCREEN, SOURCE_PROJECTOR)


def _settings():
    """拿到上游 app_settings 模块。导入失败返回 None（注入失败时静默降级）。"""
    try:
        import app_settings  # noqa: PLC0415  上游模块
        return app_settings
    except Exception:
        return None


def source() -> str:
    """当前的画面来源。没设过 / 值不合法 → 截整屏（上游行为）。"""
    module = _settings()
    if module is None:
        return SOURCE_SCREEN
    try:
        value = module.get(SOURCE_KEY, SOURCE_SCREEN)
    except Exception:
        return SOURCE_SCREEN
    return value if value in SOURCES else SOURCE_SCREEN


def enabled() -> bool:
    """是否启用了「OBS 源投影」这个来源。等价于 source() == "projector"。"""
    return source() == SOURCE_PROJECTOR


def set_source(value: str) -> bool:
    """改画面来源。返回是否真的写进去了。"""
    if value not in SOURCES:
        return False
    module = _settings()
    if module is None:
        return False
    try:
        module.set_value(SOURCE_KEY, value)
        # 顺手同步一个布尔键：将来可能有别的模块只想问"开没开"
        module.set_value(ENABLED_KEY, value == SOURCE_PROJECTOR)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"保存画面来源失败：{exc!r}")
        return False


def pause_when_closed() -> bool:
    """投影消失时是否暂停记录。默认 True（投影没了多半是用户把 OBS 关了/切走了）。"""
    module = _settings()
    if module is None:
        return True
    try:
        return module.get(PAUSE_KEY, True) is not False
    except Exception:
        return True


def set_pause_when_closed(on: bool) -> bool:
    module = _settings()
    if module is None:
        return False
    try:
        module.set_value(PAUSE_KEY, bool(on))
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"保存暂停开关失败：{exc!r}")
        return False


def _log(message: str) -> None:
    try:
        cloud_state.log(message)
    except Exception:
        pass
