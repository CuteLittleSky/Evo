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
            # 跳过注入层自己的 finder（cloud/importhook.py 的 _SharedFinder）：
            # 2026-10-25 实测，"全体玩家关闭"把 panel_app 登记进 _WAITING 后，
            # _SharedFinder 会转发 panel_app 的查找，而它又转发回这里 → 无限递归
            # （RecursionError，Evo 起不来）。它已经会替我们触发回调，不需要再问一遍。
            if getattr(finder, "_paimiao_shared_import_hook", False):
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

    # 更新检查的地址（2026-11-04 用户报「一更新就装成官方版」）：托盘进程不会 import
    # panel_app，attach() 里的 apply() 轮不到它，所以必须在这里（进程启动时）先补上，
    # 比托盘 15 秒后的第一次检查早。
    #
    # 源码运行时 install() 比 run_evo.py 把 code/ 加进 sys.path 还早（sitecustomize 在
    # 解释器启动时就跑了），所以这里先把项目里的 code/ 补上，否则 import update_check
    # 会失败、补丁悄悄不生效。打包版里 code/ 在 PyInstaller 的归档里，本来就能 import，
    # 这个目录不存在就跳过。
    _code_dir = os.path.join(os.path.dirname(_ROOT), "code")
    if os.path.isdir(_code_dir) and _code_dir not in sys.path:
        sys.path.insert(0, _code_dir)
    try:
        from cloud import upstream as cloud_upstream
        cloud_upstream.patch_update_source()
    except Exception as exc:
        _log(f"更新地址注入失败（可能去官方源查更新，需人工检查）：{exc!r}")

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

    try:
        from cloud import season_align
        season_align.install()
    except Exception as exc:
        _log(f"赛季页识别自适应注入失败（按上游原样）：{exc!r}")

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

    if not getattr(app, "_paimiao_global_stats_installed", False):
        app._paimiao_global_stats_installed = True
        try:
            from cloud.global_stats import inject as global_stats_inject
            global_stats_inject.register(app)
        except Exception as exc:
            _log(f"全体玩家关闭注入失败（Evo 本身不受影响）：{exc!r}")

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

    if not getattr(app, "_paimiao_pickrate_installed", False):
        app._paimiao_pickrate_installed = True
        try:
            from cloud.pickrate import inject as pickrate_inject
            pickrate_inject.register(app, module)
        except Exception as exc:
            _log(f"榜单页注入失败（不影响记录）：{exc!r}")

    if not getattr(app, "_paimiao_map_rotation_installed", False):
        app._paimiao_map_rotation_installed = True
        try:
            from cloud import map_rotation_inject
            map_rotation_inject.register(app, module)
        except Exception as exc:
            _log(f"地图轮换注入失败（不影响记录）：{exc!r}")

    # 设置页 1.4 秒的元凶：上游 folder_usage() 每渲染一次就 rglob + stat 一遍
    # captures/live（见 cloud/folder_usage_inject.py）。挂不上只是慢，不影响功能。
    if not getattr(app, "_paimiao_folder_usage_installed", False):
        app._paimiao_folder_usage_installed = True
        try:
            from cloud import folder_usage_inject
            folder_usage_inject.install(module)
        except Exception as exc:
            _log(f"设置页提速注入失败（设置页照常能开）：{exc!r}")

    if not app.blueprints.get("cloud"):
        cloud_panel.MODULE = module
        app.register_blueprint(cloud_panel.build())

        cloud_sync.set_db_path(app.config.get("DB"))

    # 总览页段位卡片里的「从派喵获取的段位」那一小块（2026-11-04）。
    # 注册失败只是这一块不显示，其他页面照常。
    if "paimiaorank" not in app.blueprints:
        try:
            from cloud.paimiao_rank import inject as paimiao_rank_inject
            paimiao_rank_inject.register(app)
        except Exception as exc:
            _log(f"「从派喵获取的段位」那一块注入失败（总览页照常）：{exc!r}")

    if not getattr(app, "_cloud_sync_armed", False):
        app._cloud_sync_armed = True
        app.before_request(cloud_sync.arm_once)

    _log(f"已接入：{app.name}")
