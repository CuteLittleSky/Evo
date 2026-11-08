"""Windows 托盘通知（复用 pystray 图标，零新增依赖）。

`Icon.notify(message, title)` 是 pystray 自带的托盘气泡。我们在 bootstrap 早期包一层
`pystray.Icon.__init__` 记住实例，之后就能在后台线程里发通知。

降级：pystray 不在 / 图标还没建好时 notify() 返回 False，调用方会跳过（不标记已发，下次再试）。
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_icon = None

_CAP = "_paimiao_notify_capture"


def _remember(icon) -> None:
    """记下图标实例（后台线程发通知时用）。"""
    global _icon
    if icon is None:
        return
    with _lock:
        _icon = icon


def _wrap_init(cls) -> None:
    """包 Icon.__init__：本函数早于图标创建时装上，创建那一刻就记住实例。"""
    if getattr(cls.__init__, _CAP, False):
        return
    real_init = cls.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        _remember(self)

    setattr(init, _CAP, True)
    cls.__init__ = init


def _wrap_run(cls) -> None:
    """包 Icon.run：run 一定会在图标实例上调用一次，self 就是实例。"""
    run = getattr(cls, "run", None)
    if run is None or getattr(run, _CAP, False):
        return
    real_run = run

    def run_wrapper(self, *args, **kwargs):
        _remember(self)
        return real_run(self, *args, **kwargs)

    setattr(run_wrapper, _CAP, True)
    cls.run = run_wrapper


def _wrap_property_setters(cls) -> None:
    """包 Icon 的 icon/title/visible 的 setter，赋值时把 self 记下来。

    属性是**类级**的，对「setter 被包装之前就已创建」的实例同样生效。上游在
    evo_app.py:613 就 `pystray.Icon(...)` 把图标建好了，而 capture_icon() 要等
    panel.open()(:680，在 icon.run 的 setup 里) 第一次 import panel_app 才被调到，
    必然晚于创建。此后 Recorder 每 5 秒刷新一次（refresh() 里的 `icon.icon=...` /
    `icon.title=...`），setter 一被调用就抓住实例 —— 这是兼容已存在实例的关键。
    """
    for klass in getattr(cls, "__mro__", ()):
        for name in ("icon", "title", "visible"):
            prop = klass.__dict__.get(name)
            if not isinstance(prop, property) or prop.fset is None:
                continue
            mark = _CAP + "_" + name
            if getattr(klass, mark, False):
                continue
            real_fset = prop.fset

            def make_setter(real_fset):
                def setter(self, value):
                    _remember(self)
                    return real_fset(self, value)
                return setter

            setattr(klass, name,
                    property(prop.fget, make_setter(real_fset), prop.fdel, prop.__doc__))
            setattr(klass, mark, True)


def capture_icon() -> bool:
    """拿到 pystray 托盘图标实例（供后台线程发通知）。幂等；装不上返回 False。

    2026-11-08 修复托盘通知发不出去：原来只包 Icon.__init__，但图标在上游
    evo_app.py:613 就建好了，capture_icon() 却要等 panel.open()(:680) 才第一次被调到，
    实例早已存在 → 只包 __init__ 永远抓不到 → _icon 恒为 None → win.notify 恒返回
    False，托盘气泡一条都发不出（应用内弹窗不受影响）。现在三种时机都抓：
    __init__（本函数早于创建时）、run（run 必被调用一次）、以及 icon/title/visible
    的 setter（类级生效，兼容已经建好的实例）。
    """
    try:
        import pystray  # noqa: PLC0415
    except Exception:
        return False
    cls = getattr(pystray, "Icon", None)
    if cls is None:
        return False
    try:
        _wrap_init(cls)
        _wrap_run(cls)
        _wrap_property_setters(cls)
    except Exception:
        # 某一层包不上不影响其它层；也绝不让调用方崩
        pass
    return True


def available() -> bool:
    return _icon is not None


def notify(title: str, body: str) -> bool:
    icon = _icon
    if icon is None:
        return False
    title = (title or "Evo").strip()[:80]
    body = (body or "").strip()[:500]
    try:
        icon.notify(body or title, title)
        return True
    except TypeError:
        try:
            icon.notify(body or title)
            return True
        except Exception:
            return False
    except Exception:
        return False
