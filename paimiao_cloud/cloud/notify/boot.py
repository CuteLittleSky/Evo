"""通知模块在托盘进程里的早期启动（不 import Flask，也不要求面板被打开）。

2026-11-08 代码审查 P2：
    原来发送线程只在 `cloud.notify.inject.register()` 里起，而 register() 由
    `cloud.bootstrap.attach()` 在 `panel_app` 被导入时才触发。开机自启用的是
    `--background`（code/evo_app.py 的 setup() 里 `if not args.background` 才
    panel.open()），panel_app 永远不导入 → 发送线程永远不启动 → **只在后台跑、
    从没打开过面板的用户收不到任何 Windows 托盘通知**，而这类用户恰恰只有
    托盘气泡能看到提醒。

本模块在**托盘进程**里做两件事（记录子进程不创建 pystray 图标，走不到这里，
所以不会误开通知线程）：

1. 尽可能早地 `win.capture_icon()`：在图标创建之前就把 `__init__` / `run` /
   property 的包装装好，保证图标实例一定被抓到（否则 `win.notify()` 恒返回
   False，一条气泡都发不出）；
2. 包 `pystray.Icon.run`：托盘真正跑起来时 `worker.start()` 启动发送线程（幂等）。
   选 Icon.run 而不是在 bootstrap 早期直接开线程，是为了只在托盘进程里开一次。

失败静默降级：pystray 不在 / 包不上，只是通知发不出，绝不影响 Evo 正常记录。
"""
from __future__ import annotations

_RUN_MARK = "_paimiao_notify_boot_run"
_INSTALLED = False


def install() -> bool:
    """幂等（重复调用直接返回，不重复套壳）。bootstrap 早期可反复调用。"""
    global _INSTALLED
    if _INSTALLED:
        return True

    # 先尽量早地装图标捕获包装（拿不到 pystray 时 capture_icon 自己会返回 False）。
    # 这一步顺带会把 Icon.run 包一层（记实例），所以下面再基于它包我们自己的启动钩子。
    try:
        from cloud.notify import win  # noqa: PLC0415
        win.capture_icon()
    except Exception:
        pass

    try:
        import pystray  # noqa: PLC0415
    except Exception:
        # pystray 现在拿不到（极少见）：不设 _INSTALLED，等下次（比如 attach）再试。
        return False

    cls = getattr(pystray, "Icon", None)
    if cls is None:
        return False
    real_run = getattr(cls, "run", None)
    if real_run is None or getattr(real_run, _RUN_MARK, False):
        return False

    def run_wrapper(self, *args, **kwargs):
        # 走到这里说明在托盘进程里、图标马上开始 run：启动发送线程（幂等只起一次）。
        try:
            from cloud.notify import worker  # noqa: PLC0415
            worker.start()
        except Exception:
            pass
        return real_run(self, *args, **kwargs)

    setattr(run_wrapper, _RUN_MARK, True)
    try:
        cls.run = run_wrapper
    except Exception:
        return False
    _INSTALLED = True
    return True
