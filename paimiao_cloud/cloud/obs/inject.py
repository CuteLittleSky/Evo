"""注入点：让记录子进程用「OBS 投影」那块屏截屏（全部在内存里改，上游文件 0 改动）。

三种运行形态、两个入口：

    源码运行、主进程      run_evo.py → sitecustomize → bootstrap.install()
    打包运行              runtime_hook.py → bootstrap.install()
                          ↓ 只装「等待 live_collect 被导入」的钩子
                          live_collect 在记录子进程里才被导入（evo_app.py:251）
                          ↓ 那时再接管 dxcam.create

**为什么挂钩点放在 live_collect 而不是主进程的启动处**：
    - `camera = dxcam.create(...)` 在 `live_collect.collect()` 里（live_collect.py:373），
      `live_collect` 又是记录子进程里才 import 的（evo_app.py:251）——
      所以在 live_collect 导入时包 dxcam.create，**只会影响记录子进程**，
      托盘进程完全不受影响（它本来也不截屏）。
    - 用 meta_path 包 loader 的方式和 bootstrap.py 包 panel_app 是同一套写法，
      不会提前 import 上游的重模块，也不改变加载顺序。

降级：任何一步失败都只写日志，Evo 照常跑（截整屏）。
"""
from __future__ import annotations

import sys

_INSTALLED = False

# 记录子进程里当前正在用的日志函数。None = 用控制台。
# live_collect.collect(log=...) 一进来就会把它换掉（见 _patched_collect），
# 这样相机打的提示和 Evo 别的日志写进同一个文件。
_CURRENT_LOG = None


def install():
    """登记「等 live_collect 导入后接管 dxcam」。幂等。

    **不再自己挂 sys.meta_path finder**（2026-10-24 实测）：Python 只问第一个
    返回 spec 的 finder，四个注入块各挂一个时后面的永远不触发 —— OBS 投影接管
    也一样会被挤掉。统一走 `cloud/importhook.py`。
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    try:
        from cloud import importhook  # noqa: PLC0415

        importhook.on_import("live_collect", _after_import, label="OBS 投影")
    except Exception:
        _INSTALLED = False
        raise


def _after_import(module):
    """live_collect 已经导入，它的命名空间里有 dxcam 了，接管它的 create。"""
    dxcam_module = getattr(module, "dxcam", None)
    if dxcam_module is None:
        return
    from cloud.obs import capture as obs_capture  # noqa: PLC0415
    if obs_capture.install(dxcam_module, log=_log_now):
        _log("[OBS 投影] 已接管截屏（设置里选「OBS 投影」时截那块屏，否则照旧整屏）")
    _patch_collect_log(module)
    _patch_dedupe(module)


def _patch_dedupe(module):
    """让「上一局」从数据库里读，而不是从内存里读：重启后同一张总结页不再存两次。

    细节见 cloud/obs/dedupe.py。这里只在主机模式下接（PC 玩家一点都不碰）。
    """
    try:
        from cloud.obs import dedupe as obs_dedupe  # noqa: PLC0415
        if obs_dedupe.install(module):
            _log("[主机模式] 已接管「是不是同一局」的判断：重启后在总结页上不会重复记录")
    except Exception as exc:  # noqa: BLE001 判重接不上只是多一条重复，不能让记录停掉
        _log(f"[主机模式] 判重接管失败（不影响记录）：{exc!r}")


def _patch_collect_log(module):
    """包 live_collect.collect，把它收到的 log 存起来给相机用。

    为什么：相机打的提示（找到投影、投影不见了……）应该写进 Evo 的日志文件，
    而不是只在控制台。collect() 的 log 参数是唯一拿到那个函数的途径。
    包法是「先记下 log 再原样调用」，签名、返回值、异常行为都不变。
    """
    real = getattr(module, "collect", None)
    if real is None or getattr(real, "_paimiao_obs", False):
        return

    def collect(*args, **kwargs):
        global _CURRENT_LOG
        _CURRENT_LOG = kwargs.get("log")
        try:
            return real(*args, **kwargs)
        finally:
            _CURRENT_LOG = None

    collect._paimiao_obs = True  # type: ignore[attr-defined]
    collect._paimiao_real = real  # type: ignore[attr-defined]
    module.collect = collect


def _log_now(message):
    """相机用的日志出口：collect 里给的 log 优先，其次写 cloud 日志，最后控制台。"""
    log = _CURRENT_LOG
    if log is not None:
        try:
            log(message)
            return
        except Exception:
            pass
    _log(message)


def _log(message):
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        cloud_state.log(message)
        return
    except Exception:
        pass
    try:
        print(message)
    except Exception:
        pass
