"""Evo 窗口（WebView2）省内存（2026-11-24 用户决定）。纯注入，上游 app_window.py / pywebview 一个字节不动。

1. 启动参数：窗口进程里设 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS（WebView2 官方环境变量，优先于代码里的
   AdditionalBrowserArguments），关掉面板用不到的后台功能。pywebview 自己加的 ElasticOverscroll 也带上，行为不变。
   不限制 JS 堆、不限渲染进程数（图表页、登录小窗口照常）。
2. 窗口最小化时 MemoryUsageTargetLevel = Low（页面不重载），还原时回到 Normal。
   WebView2 太旧没有这个属性就跳过。

只在 webview.platforms.edgechromium 被导入时才动手（importhook），托盘、记录子进程不受影响。
任何一步失败都静默降级：窗口照常打开。
"""
from __future__ import annotations

import os

_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
_DISABLED_FEATURES = (
    "ElasticOverscroll",            # pywebview 原本就加的
    "Translate", "msEdgeTranslate",  # 翻译提示
    "msSmartScreenProtection",      # SmartScreen（只访问本机面板）
    "AutofillServerCommunication",
    "MediaRouter",                  # 投屏发现
)
ARGS = "--disable-features=" + ",".join(_DISABLED_FEATURES) + " --disable-component-update"
_MARK = "_paimiao_webview_memory"


def _log(message):
    try:
        from cloud.state import log  # noqa: PLC0415
        log(message)
    except Exception:  # noqa: BLE001
        pass


def install():
    try:
        from cloud import importhook  # noqa: PLC0415
        importhook.on_import("webview.platforms.edgechromium", _patch, "webview_memory")
    except Exception as exc:  # noqa: BLE001
        _log(f"窗口省内存注入失败（窗口照常）：{exc!r}")


def _patch(module):
    cls = getattr(module, "EdgeChrome", None)
    if cls is None or getattr(cls, _MARK, False):
        return
    # 用户自己设过这个环境变量（调试等）就不覆盖
    if not os.environ.get(_ENV):
        os.environ[_ENV] = ARGS
    real_init, real_ready = cls.__init__, getattr(cls, "on_webview_ready", None)

    def __init__(self, *args, **kwargs):
        if not os.environ.get(_ENV):
            os.environ[_ENV] = ARGS
        return real_init(self, *args, **kwargs)

    cls.__init__ = __init__
    if real_ready is not None:
        def on_webview_ready(self, sender, args):
            result = real_ready(self, sender, args)
            try:
                if args.IsSuccess:
                    _watch_minimize(self)
            except Exception as exc:  # noqa: BLE001
                _log(f"窗口低内存模式挂不上（窗口照常）：{exc!r}")
            return result

        cls.on_webview_ready = on_webview_ready
    setattr(cls, _MARK, True)


def _watch_minimize(chrome):
    core = chrome.webview.CoreWebView2
    if core is None or not hasattr(core, "MemoryUsageTargetLevel"):
        _log("WebView2 较旧，不支持低内存模式，跳过")
        return
    from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level  # noqa: PLC0415
    from System.Windows.Forms import FormWindowState  # noqa: PLC0415

    form = chrome.form

    def on_resize(_sender, _args):
        try:
            low = form.WindowState == FormWindowState.Minimized
            want = Level.Low if low else Level.Normal
            if core.MemoryUsageTargetLevel != want:
                core.MemoryUsageTargetLevel = want
        except Exception:  # noqa: BLE001 窗口正在关闭等
            pass

    form.Resize += on_resize
