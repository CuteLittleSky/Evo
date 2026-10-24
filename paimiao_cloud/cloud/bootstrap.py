from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_TEMPLATES = os.path.join(_HERE, "templates")

_installed = False

class _PanelAppFinder:

    def find_module(self, fullname, path=None):
        return None

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "panel_app":
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            spec = find_spec(fullname, path, target)
            if spec is not None:
                spec.loader = _AfterImportLoader(spec.loader)
                return spec
        return None

class _AfterImportLoader:

    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        make = getattr(self._inner, "create_module", None)
        return make(spec) if make else None

    def exec_module(self, module):
        self._inner.exec_module(module)
        try:
            attach(module)
        except Exception as exc:
            _log(f"注入失败（Evo 本身不受影响）：{exc!r}")

def install():

    global _installed
    if _installed:
        return
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    try:
        from cloud.obs import inject as obs_inject
        obs_inject.install()
    except Exception as exc:
        _log(f"OBS 投影注入失败（Evo 本身不受影响）：{exc!r}")

    try:
        from cloud.obs import trigger as obs_trigger
        obs_trigger.install()
    except Exception as exc:
        _log(f"主机模式启动条件注入失败（不影响记录）：{exc!r}")

    try:
        from cloud.lobby_mode import inject as mode_inject
        mode_inject.install()
    except Exception as exc:
        _log(f"大厅模式识别注入失败（Evo 本身不受影响）：{exc!r}")

    try:
        from cloud.wildcard import inject as wildcard_inject
        wildcard_inject.install()
    except Exception as exc:
        _log(f"外卡模式注入失败（Evo 本身不受影响）：{exc!r}")

    try:
        from cloud.bullet import inject as bullet_inject
        bullet_inject.install()
    except Exception as exc:
        _log(f"子弹时间注入失败（Evo 本身不受影响）：{exc!r}")

    try:
        from cloud.legend_pick import inject as legend_pick_inject
        legend_pick_inject.install()
    except Exception as exc:
        _log(f"选取界面识别注入失败（Evo 本身不受影响）：{exc!r}")

    try:
        from cloud import cpu_guard_inject
        cpu_guard_inject.install()
    except Exception as exc:
        _log(f"OpenCV 线程数注入失败（Evo 本身不受影响）：{exc!r}")

    try:
        from cloud.summary_rows import detect as summary_rows_inject
        summary_rows_inject.install()
    except Exception as exc:
        _log(f"总结页读数注入失败（Evo 本身不受影响）：{exc!r}")

    module = sys.modules.get("panel_app")
    if module is not None:
        _installed = True
        attach(module)
        return
    sys.meta_path.insert(0, _PanelAppFinder())
    _install_flask_hook()
    _installed = True

def _install_flask_hook():

    try:
        from flask import Flask
    except Exception:
        return
    original = Flask.run

    def run(self, *args, **kwargs):
        try:
            main_module = sys.modules.get("__main__")
            path = str(getattr(main_module, "__file__", "") or "")
            if path.endswith(os.path.join("code", "panel_app.py")) and main_module.app is self:
                attach(main_module)
            elif path.endswith(os.path.join("code", "evo_app.py")):

                from cloud.obs import trigger as obs_trigger
                obs_trigger.install()
        except Exception as exc:
            _log(f"注入失败（Evo 本身不受影响）：{exc!r}")
        return original(self, *args, **kwargs)

    Flask.run = run

def _log(message):
    try:
        from cloud.state import log
        log(message)
    except Exception:
        pass

def attach(module):

    from cloud import notice as cloud_notice
    from cloud import panel as cloud_panel
    from cloud import sync as cloud_sync
    from cloud import upstream
    from cloud.templates import splice_loader

    app = module.app

    if not getattr(app, "_cloud_loader_installed", False):
        app.jinja_loader = splice_loader(app.jinja_loader, _TEMPLATES)
        app._cloud_loader_installed = True

    if not getattr(app, "_cloud_upstream_patched", False):
        app._cloud_upstream_patched = True
        upstream.apply(module, app.jinja_env)

    if not getattr(app, "_cloud_notice_installed", False):
        app._cloud_notice_installed = True
        cloud_notice.register(app, module)

    if not getattr(app, "_cloud_obs_page_installed", False):
        app._cloud_obs_page_installed = True
        try:
            from cloud.obs import page as obs_page
            obs_page.register(app)
        except Exception as exc:
            _log(f"OBS 设置栏注入失败（不影响记录）：{exc!r}")

    try:
        from cloud.obs import trigger as obs_trigger
        obs_trigger.install()
    except Exception as exc:
        _log(f"主机模式启动条件注入失败（不影响记录）：{exc!r}")

    if not getattr(app, "_paimiao_mode_labels_installed", False):
        app._paimiao_mode_labels_installed = True
        try:

            from cloud.lobby_mode import inject as mode_inject
            mode_inject._patch_mode_labels()
        except Exception as exc:
            _log(f"模式中文名注入失败（不影响记录）：{exc!r}")

    if not getattr(app, "_paimiao_wildcard_page_installed", False):
        app._paimiao_wildcard_page_installed = True
        try:
            from cloud.wildcard import page as wildcard_page
            wildcard_page.register(app, module)
        except Exception as exc:
            _log(f"外卡模式页面注入失败（不影响记录）：{exc!r}")

    if not getattr(app, "_paimiao_bullet_page_installed", False):
        app._paimiao_bullet_page_installed = True
        try:
            from cloud.bullet import page as bullet_page
            bullet_page.register(app, module)
        except Exception as exc:
            _log(f"子弹时间页面注入失败（不影响记录）：{exc!r}")

    if not app.blueprints.get("cloud"):
        cloud_panel.MODULE = module
        app.register_blueprint(cloud_panel.build())

        cloud_sync.set_db_path(app.config.get("DB"))

    if not getattr(app, "_cloud_sync_armed", False):
        app._cloud_sync_armed = True
        app.before_request(cloud_sync.arm_once)

    _log(f"已接入：{app.name}")
