"""通知模块注入入口：应用内弹窗（模板注入）+ Windows 托盘通知 + 拉取线程。

全部幂等、失败静默降级：注入不上只是不弹通知，绝不影响 Evo 正常记录。
"""
from __future__ import annotations

from flask import Blueprint, abort, current_app, request

from cloud.notify import store, ui, win, worker

bp = Blueprint("notify", __name__, url_prefix="/cloud/notify")

_SCRIPTS_MARK = "{% block scripts %}"
_INJECT_MARK = "evo_notice_dialog()"   # 模板源码里看得见的幂等标记


@bp.post("/ack")
def ack():
    """用户已读一条通知。动作/关闭都调这里，记进本地 notify_state.json。"""
    expected = str(current_app.config.get("TOKEN") or "")
    token = request.form.get("token") or ""
    if expected and token != expected:
        abort(403)
    notice_id = (request.form.get("id") or "").strip()[:60]
    action = (request.form.get("action") or "dismiss").strip()[:20]
    if notice_id:
        try:
            store.mark_seen(notice_id, action)
        except Exception:
            pass
    return ("", 204)


def _inject_source(source: str) -> str:
    if not source or _INJECT_MARK in source:
        return source
    at = source.find(_SCRIPTS_MARK)
    if at < 0:
        return source
    block = "{% if evo_notice_pending() %}{{ evo_notice_dialog() | safe }}{% endif %}"
    return source[:at] + block + source[at:]


def _install_preprocess(app) -> None:
    env = app.jinja_env
    if getattr(env, "_paimiao_notify_preprocess", False):
        return
    original = env.preprocess

    def preprocess(source, name, filename=None):
        try:
            source = original(source, name, filename)
        except TypeError:
            source = original(source, name)
        return _inject_source(source)

    env.preprocess = preprocess
    env._paimiao_notify_preprocess = True
    env.globals["evo_notice_pending"] = ui.pending
    env.globals["evo_notice_dialog"] = ui.render_dialog


def register(app, module=None) -> bool:
    _install_preprocess(app)
    if "notify" not in app.blueprints:
        app.register_blueprint(bp)
    # 托盘进程里 pystray 一般已导入；这里补一次捕获（记录子进程不会走到 register）
    try:
        win.capture_icon()
    except Exception:
        pass
    try:
        from cloud import state as cloud_state
        cloud_state.log("通知模块已接入（应用内弹窗 + Windows 通知）")
    except Exception:
        pass
    # 打开面板时补一次启动。真正的后台启动在 cloud/notify/boot.py（托盘 Icon.run），
    # 这里只是兜底：即使 boot 那一步没赶上，用户打开面板也照样能收到后续通知。
    worker.start()
    return True


def install():
    """早期（sitecustomize 阶段）只在 pystray 已经导入时才包图标。

    记录子进程也会跑 install()，这里**不能**去 import pystray（会给记录进程白加一堆依赖）；
    真正托盘进程会在 register() 里补一次 capture_icon。
    """
    try:
        import sys
        if "pystray" in sys.modules:
            win.capture_icon()
    except Exception:
        pass
    return True
