"""主机模式下的托盘提示措辞（2026-11-06 用户要求，纯注入，上游 0 字节不改）。

上游 `evo_app.main()` 里有两处写死的文字：

    tips = {"waiting": "等待 Apex 启动", "recording": "正在记录", "paused": "已暂停记录"}   # evo_app.py:612
    text = tips[state] + (f"（已记 {recorder.saved} 局）" if recorder.saved else "")
    icon.title = f"{APP_NAME} · {text}"                                               # evo_app.py:628

主机（Switch / PS5 / Xbox）的 Apex 跑在游戏机里，电脑上没有 `r5apex.exe`，
Evo 等的是「OBS 投影窗口」。所以主机模式下：

    1. 「等待 Apex 启动」→「等待投影窗口」；
    2. 投影**没置顶**时，标题末尾带上「· 投影未置顶」——别让用户以为一切正常。

怎么做到不改上游：包一层 `pystray.Icon.title` 的 setter，赋值时改写文字再交给上游。
`Recorder._loop` 在主机模式下每 5 秒调一次 `_set_state` → `on_change` → `refresh()`，
`refresh()` 每次都会重新赋值 `icon.title`，所以这里读的是 `trigger` 里最近一次
投影探测的缓存（5 秒内有效），用户中途取消「始终置顶」最多 5 秒后就会看到提醒。

降级：pystray 换了实现（title 不再是 property）就原样返回、只写日志，绝不改坏托盘。
"""
from __future__ import annotations

MARK = "_paimiao_obs_tray_title"

WAITING_OLD = "等待 Apex 启动"
WAITING_NEW = "等待投影窗口"
WAITING_CAMERA = "等待虚拟摄像头"
NOT_TOPMOST_NOTE = " · 投影未置顶"


def install() -> bool:
    """包一层 pystray.Icon.title。幂等；装不上返回 False（调用方只记日志）。"""
    try:
        import pystray  # noqa: PLC0415
    except Exception:
        return False
    icon_cls = getattr(pystray, "Icon", None)
    if icon_cls is None:
        return False
    # title 属性不一定定义在 icon_cls 自己身上（Windows 上是 pystray._base.Icon），沿 MRO 找
    owner, prop = None, None
    for klass in getattr(icon_cls, "__mro__", ()):
        candidate = klass.__dict__.get("title")
        if isinstance(candidate, property):
            owner, prop = klass, candidate
            break
    if owner is None or prop.fget is None or prop.fset is None:
        return False
    if getattr(owner, MARK, False):
        return True

    def getter(self):
        return prop.fget(self)

    def setter(self, value):
        return prop.fset(self, format_title(value))

    owner.title = property(getter, setter, prop.fdel, prop.__doc__)
    setattr(owner, MARK, True)
    return True


def format_title(title):
    """把上游算出来的标题改成主机模式的措辞。非主机模式、非字符串一律原样返回。"""
    if not isinstance(title, str):
        return title
    try:
        from cloud.obs import settings as obs_settings  # noqa: PLC0415
        source = obs_settings.source()
    except Exception:
        return title

    if source == obs_settings.SOURCE_CAMERA:
        # 主机等的是 OBS 虚拟摄像头；相机没有「置顶」概念，不加那句提醒
        return title.replace(WAITING_OLD, WAITING_CAMERA)

    # 投影分支：source 判断为主；再 OR 一下旧的 enabled()，兼容老的验证脚本只改 enabled
    if source == obs_settings.SOURCE_PROJECTOR or obs_settings.enabled():
        out = title.replace(WAITING_OLD, WAITING_NEW)   # 等待投影窗口
        if "正在记录" in out:
            try:
                from cloud.obs import trigger  # noqa: PLC0415
                found = trigger.last_projector()
                if found is not None and not found.get("topmost"):
                    out += NOT_TOPMOST_NOTE
            except Exception:
                pass
        return out

    return title
