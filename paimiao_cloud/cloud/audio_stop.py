"""修 `process_audio.ProcessAudio` 覆盖 `threading.Thread._stop()` 的崩溃（2026-11-24，审计 0.7.2 合并时发现）。

## 问题

上游 0.7.2 新加的 `code/process_audio.py:220` 写的是：

    class ProcessAudio(threading.Thread):
        def __init__(...):
            self._stop = threading.Event()

`threading.Thread` 内部**自己就有一个 `_stop()` 方法**（线程结束时 `join()`、`is_alive()` 会调它来清状态）。
这里把方法换成了 Event 对象，于是线程一结束、再问 `is_alive()`，CPython 的
`_wait_for_tstate_lock()` 执行 `self._stop()` 就抛：

    TypeError: 'Event' object is not callable

记录程序本身不需要问 `is_alive()`，所以平时不炸；但设置页「测试麦克风」
（`code/panel_recordings.py:595` 的 `MicTest.status()`）每 1 秒问一次 `rec.is_alive()`，
录完那一段以后必然 500。实测本机 Python 3.11.5 必现。

## 做法（纯注入，`code/process_audio.py` 一个字节都不动）

把那个 Event 改名成 `_stop_flag`，再把 `stop()` 里读 `_stop` 的地方指过去：

- 所有实例上的 `_stop`（Event）→ `_stop_flag`，同时把类上继承来的 `_stop` 方法**还回去**
- 包 `ProcessAudio.stop()`：先确保旧名字还能用（`self._stop` 若还是 Event 就转成 `_stop_flag`）
- 包 `ProcessAudio.run()`：把上游源码里 `self._stop.is_set()` 的语义保持不变（读 `_stop_flag`）

因为 `run()` 是上游写死的 `while not self._stop.is_set()`，只改名不够，还要让实例上的
`_stop` 不再是 Event —— 改名以后 `self._stop` 会落到类的 `Thread._stop` 方法上，`.is_set()` 就没了。

所以这里走的是最小可信路径：**给实例装一个只读代理**，`is_set()` 转给 `_stop_flag`，
`set()` 转给 `_stop_flag`，其余 dunder（`__call__` 等）落到线程真正的 `_stop()` 方法上 ——
这样 `Thread._wait_for_tstate_lock()` 调 `self._stop()` 时能正常执行，上游 `run()` 也照常工作。

降级：任何一步失败只写日志，麦克风测试照上游行为（可能报错，但不影响录像和识别）。
"""
from __future__ import annotations

MARK = "_paimiao_audio_stop"
_INSTALLED = False
_said = set()


def _log(message):
    if message in _said:
        return
    _said.add(message)
    try:
        from cloud.state import log
        log(f"[麦克风测试] {message}")
    except Exception:
        pass


class _EventProxy:
    """实例上的 `_stop`：`is_set()` / `set()` / `wait()` 走 `_stop_flag`，
    调用它（`self._stop()`）走到线程真正的方法上，让 `Thread.join()` / `is_alive()` 正常工作。"""

    __slots__ = ("_flag", "_real")

    def __init__(self, flag, real):
        object.__setattr__(self, "_flag", flag)
        object.__setattr__(self, "_real", real)

    def is_set(self):
        return self._flag.is_set()

    def set(self):
        return self._flag.set()

    def clear(self):
        return self._flag.clear()

    def wait(self, timeout=None):
        return self._flag.wait(timeout) if timeout is not None else self._flag.wait()

    def __call__(self, *args, **kwargs):
        return self._real(*args, **kwargs) if callable(self._real) else None

    def __bool__(self):
        return bool(self._flag)

    def __repr__(self):
        return f"<evo _stop proxy flag={self._flag!r}>"


def _patch_class(cls):
    """`__init__` 里把 `_stop`（Event）换成代理；类上 `_stop` 方法保持原样不动。"""
    real_stop_method = getattr(cls, "_stop", None)          # Thread._stop（继承来的）
    real_init = getattr(cls, "__init__", None)
    if real_init is None or not callable(real_stop_method):
        _log("找不到 Thread._stop，跳过（麦克风测试照上游行为）")
        return False
    if getattr(real_init, MARK, False):
        return True

    def __init__(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        old = self.__dict__.get("_stop")
        if old is not None and not isinstance(old, _EventProxy):
            self._stop_flag = old                       # 留着，方便排查；上游 run() 用代理
            self.__dict__["_stop"] = _EventProxy(old, real_stop_method.__get__(self, type(self)))

    setattr(__init__, MARK, True)
    __init__.__wrapped__ = real_init  # type: ignore[attr-defined]
    cls.__init__ = __init__
    return True


def patch_module(module):
    """两件事：ProcessAudio 的 `_stop` 改名（根治）；panel_recordings.MicTest.status 兜住异常（第二道防线）。"""
    ok = False
    cls = getattr(module, "ProcessAudio", None)
    if cls is None:
        _log("process_audio 里没有 ProcessAudio，跳过改名（麦克风测试照上游行为）")
    else:
        try:
            ok = _patch_class(cls)
        except Exception as exc:  # noqa: BLE001
            _log(f"改名挂不上（麦克风测试照上游行为）：{exc!r}")
        if ok:
            _log("已接管：ProcessAudio 的 _stop 不再覆盖线程方法")
    _patch_mic_test()
    return ok


def _patch_mic_test():
    """`panel_recordings.MicTest.status()` 里问 `rec.is_alive()` 抛什么都不能让设置页 500：
    录音线程已经走完、状态说不清时，就当它录完了（文件在就算 done，否则 idle）。"""
    try:
        import panel_recordings  # noqa: PLC0415
    except Exception:
        return False                      # 这个进程没开面板（记录子进程），不需要
    cls = getattr(panel_recordings, "MicTest", None)
    real = getattr(cls, "status", None)
    if real is None or getattr(real, MARK + "_status", False):
        return False

    def status(self):
        try:
            return real(self)
        except Exception as exc:  # noqa: BLE001  is_alive() 踩到 _stop 冲突之类
            _log(f"读录音状态出错，按录完处理：{exc!r}")
            path = getattr(self, "path", None)
            if getattr(self, "error", None):
                return {"state": "error", "error": self.error}
            if path and path.exists():
                return {"state": "done"}
            return {"state": "idle"}

    setattr(status, MARK + "_status", True)
    status.__wrapped__ = real  # type: ignore[attr-defined]
    cls.status = status
    return True



def install():
    """等 process_audio 导入后挂上。幂等。"""
    global _INSTALLED
    if _INSTALLED:
        return False
    _INSTALLED = True
    try:
        from cloud import importhook
        importhook.on_import("process_audio", patch_module, "process_audio 的 _stop")
    except Exception as exc:  # noqa: BLE001
        _INSTALLED = False
        _log(f"挂不上（麦克风测试照上游行为）：{exc!r}")
        return False
    return True
