"""退出兜底：保证点「退出」后一定能关掉（2026-11-07 用户同意）。

症状（2026-11-07 实测）：点托盘「退出」后日志只重复「正在退出……」、进程不退出。
用 py-spy 抓到：recorder / panel 线程都没了（收尾已完成），但主线程还停在
pystray 的 `GetMessage` 里 —— `icon.stop()` 没让托盘消息循环退出（pystray 偶发问题）。
上游 `evo_app.shut_down` 是 `main()` 里的**局部函数**，没法直接包；但它的每一步都是
模块级的类方法 / pystray 方法，可以包。

做法（纯注入，上游 0 改动）：
  1. 包 `Recorder.quit`：一进退出就给「退出守护」上计时（GRACE_SECONDS 秒）；
  2. 包 `PanelServer.close` / `pystray.Icon.stop`：记每一步耗时，下次一眼看出卡在哪；
  3. 守护线程到点进程还在，就写日志 + `os._exit(0)` 强退。
     此时记录子进程已收尾（「本次记录」统计已写、数据已保存），不会丢局。

降级：拿不到 evo_app / pystray 就返回 False，只写日志，绝不影响正常退出。
"""
from __future__ import annotations

import os
import sys
import threading
import time

GRACE_SECONDS = 12.0      # 正常退出最多等这么久，还没结束就强退

_MARK_Q = "_paimiao_quit_guard_quit"
_MARK_C = "_paimiao_quit_guard_close"
_MARK_S = "_paimiao_quit_guard_stop"

_armed = False
_log = None


def _say(message: str) -> None:
    try:
        if _log is not None:
            _log(message)
            return
    except Exception:
        pass
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


def _arm_guard() -> None:
    """启动兜底计时线程（只启一次）。到点进程还在就强退。"""
    global _armed
    if _armed:
        return
    _armed = True

    def watch():
        time.sleep(GRACE_SECONDS)
        _say(f"[退出守护] 正常退出超过 {GRACE_SECONDS:.0f} 秒还没结束，强制结束进程（记录已保存）")
        try:
            import logging
            logging.shutdown()
        except Exception:
            pass
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=watch, name="quit-guard", daemon=True).start()


def install(module=None, log=None) -> bool:
    """给 evo_app 的退出路径包一层守护。幂等；返回这次是否真的接上了。"""
    global _log
    if log is not None:
        _log = log
    if module is None:
        module = _find_module()
    if module is None:
        return False
    did = False
    did = _patch_quit(module) or did
    did = _patch_close(module) or did
    did = _patch_stop() or did
    return did


def _find_module():
    mod = sys.modules.get("evo_app")
    if mod is not None and hasattr(mod, "Recorder"):
        return mod
    main = sys.modules.get("__main__")
    if (main is not None
            and str(getattr(main, "__file__", "") or "").endswith(("evo_app.py", "evo_app.pyc"))
            and hasattr(main, "Recorder")):
        return main
    return None


def _patch_quit(module) -> bool:
    recorder = getattr(module, "Recorder", None)
    real = getattr(recorder, "quit", None) if recorder is not None else None
    if real is None or getattr(real, _MARK_Q, False):
        return False

    def quit(self):
        _arm_guard()
        _say(f"[退出守护] 收到退出，正常收尾中（最多 {GRACE_SECONDS:.0f} 秒，记录会先存好）")
        begin = time.monotonic()
        try:
            return real(self)
        finally:
            _say(f"[退出守护] 记录收尾用了 {time.monotonic() - begin:.1f} 秒")

    setattr(quit, _MARK_Q, True)
    setattr(quit, "_paimiao_real", real)
    recorder.quit = quit
    return True


def _patch_close(module) -> bool:
    panel = getattr(module, "PanelServer", None)
    real = getattr(panel, "close", None) if panel is not None else None
    if real is None or getattr(real, _MARK_C, False):
        return False

    def close(self):
        begin = time.monotonic()
        try:
            return real(self)
        finally:
            _say(f"[退出守护] 面板关闭用了 {time.monotonic() - begin:.1f} 秒")

    setattr(close, _MARK_C, True)
    setattr(close, "_paimiao_real", real)
    panel.close = close
    return True


def _patch_stop() -> bool:
    try:
        import pystray  # noqa: PLC0415
        icon_cls = getattr(pystray, "Icon", None)
    except Exception:
        return False
    real = getattr(icon_cls, "stop", None) if icon_cls is not None else None
    if real is None or getattr(real, _MARK_S, False):
        return False

    def stop(self):
        _arm_guard()   # 兜底：万一前面没走到 Recorder.quit（比如别的入口）也能保命
        begin = time.monotonic()
        try:
            return real(self)
        finally:
            _say(f"[退出守护] icon.stop 用了 {time.monotonic() - begin:.1f} 秒"
                 "（之后若进程还在，就是消息循环没退出，守护会强退）")

    setattr(stop, _MARK_S, True)
    setattr(stop, "_paimiao_real", real)
    icon_cls.stop = stop
    return True
