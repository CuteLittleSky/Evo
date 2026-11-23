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

    # 段位名兜底：库里可能有英文段位（Silver/Unranked 等旧数据），上游 ranks.has_divisions
    # 会 KeyError，把打开面板/开始记录打挂。放在最前面，早于面板和记录程序用到 ranks。
    try:
        from cloud import ranks_safety
        ranks_safety.install()
    except Exception as exc:
        _log(f"段位名兜底注入失败（不影响使用）：{exc!r}")

    # 画面识别微调（总结页检查区域加高，16:10 / 4:3 下认得出总结页）。挂不上只是照上游识别。
    try:
        from cloud import screen_tune
        screen_tune.install()
    except Exception as exc:
        _log(f"画面识别微调注入失败（识别照上游）：{exc!r}")

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

    # 录像在主机模式下的适配（上游 0.7.x 的录像；选「OBS 投影」时录投影窗口）。挂不上只是照上游的行为录。
    try:
        from cloud.obs import recording as obs_recording
        obs_recording.install()
    except Exception as exc:
        _log(f"主机模式录像注入失败（录像照上游的行为）：{exc!r}")

    # 通知模块的托盘早期启动（2026-11-08 代码审查 P2）：必须在**面板被打开之前**
    # 就能起发送线程。开机自启（--background）时 setup() 不会调 panel.open()，
    # panel_app 永远不导入，原来只在 register()（attach 时）起线程的写法在后台模式下
    # 一条通知都发不出。这里用的 cloud.notify.boot 不 import Flask，不会把 Flask 带进
    # 轻量的记录子进程；真正开线程是在 pystray.Icon.run（只有托盘进程才调用）里，
    # 记录子进程不会误开通知线程、也不会去拉后端。
    try:
        from cloud.notify import boot as notify_boot
        notify_boot.install()
    except Exception as exc:
        _log(f"通知模块预注入失败（不影响使用）：{exc!r}")

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
        from cloud.matchdedup import inject as matchdedup_inject
        matchdedup_inject.install()
    except Exception as exc:
        _log(f"同一局去重注入失败（Evo 本身不受影响）：{exc!r}")

    # 上游 process_audio 把线程的 _stop() 盖上了一个 Event，录完再问 is_alive() 必崩（设置页测麦克风）
    try:
        from cloud import audio_stop
        audio_stop.install()
    except Exception as exc:
        _log(f"麦克风测试的线程注入失败（Evo 本身不受影响）：{exc!r}")

    # 上游 set_value(键, None) 写成 JSON 的 null，下游 get(键, 默认值) 就拿不到默认值（录像页 500、每局开录报错）
    try:
        from cloud import settings_defaults
        settings_defaults.install()
    except Exception as exc:
        _log(f"设置项默认值注入失败（Evo 本身不受影响）：{exc!r}")

    # 队友名字的检查区域裁得太紧（带下划线的名字读不出），整条队友数据都不写库
    try:
        from cloud import mate_names
        mate_names.install()
    except Exception as exc:
        _log(f"队友名字注入失败（Evo 本身不受影响）：{exc!r}")

    try:
        from cloud.stats import inject as stats_inject
        stats_inject.install()
    except Exception as exc:
        _log(f"匿名统计注入失败（Evo 本身不受影响）：{exc!r}")

    try:
        from cloud.boss_score import inject as boss_inject
        boss_inject.install()
    except Exception as exc:
        _log(f"老板分补算注入失败（Evo 本身不受影响）：{exc!r}")

    try:
        from cloud import season_align
        season_align.install()
    except Exception as exc:
        _log(f"赛季页识别自适应注入失败（按上游原样）：{exc!r}")

    # Evo 窗口省内存（WebView2 启动参数、最小化时低内存模式）：只在窗口进程导入 pywebview 时生效
    try:
        from cloud import webview_memory
        webview_memory.install()
    except Exception as exc:
        _log(f"窗口省内存注入失败（窗口照常）：{exc!r}")

    module = sys.modules.get("panel_app")
    if module is not None:
        _installed = True
        attach(module)
        return
    sys.meta_path.insert(0, _PanelAppFinder())
    _install_flask_hook()
    _installed = True

def _install_flask_hook():
    """等 flask 第一次被导入时再包 Flask.run（不主动 import）：记录子进程从不用 Flask，
    原来这里无条件导入，会把 flask / werkzeug / jinja2 整套带进每个子进程（2026-11-24 省内存）。"""
    try:
        from cloud import importhook
        importhook.on_import("flask", _patch_flask_run, "bootstrap Flask.run")
    except Exception:
        pass


def _patch_flask_run(flask_module):
    Flask = getattr(flask_module, "Flask", None)
    if Flask is None or getattr(Flask.run, "_paimiao_flask_run", False):
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

    run._paimiao_flask_run = True
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

    if not getattr(app, "_paimiao_stats_ctx", False):
        app._paimiao_stats_ctx = True

        @app.context_processor
        def _paimiao_stats_context():
            try:
                import app_settings as _app_settings
                on = not bool(_app_settings.get("paimiao_stats_opt_out"))
            except Exception:
                on = True
            return {"paimiao_stats_on": on}

    if not getattr(app, "_cloud_loader_installed", False):
        app.jinja_loader = splice_loader(app.jinja_loader, _TEMPLATES)
        app._cloud_loader_installed = True

    if not getattr(app, "_cloud_upstream_patched", False):
        app._cloud_upstream_patched = True
        upstream.apply(module, app.jinja_env)

    if not getattr(app, "_cloud_notice_installed", False):
        app._cloud_notice_installed = True
        cloud_notice.register(app, module)

    # 侧边栏（上游 0.7.x 的 _side.html）里给注入的导航项补图标。挂不上只是没图标。
    if not getattr(app, "_paimiao_side_icons_installed", False):
        app._paimiao_side_icons_installed = True
        try:
            from cloud import side_icons
            side_icons.install(app.jinja_env)
        except Exception as exc:
            _log(f"侧边栏图标注入失败（侧边栏照常显示）：{exc!r}")

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

    if not getattr(app, "_paimiao_notify_installed", False):
        app._paimiao_notify_installed = True
        try:
            from cloud.notify import inject as notify_inject
            notify_inject.register(app, module)
        except Exception as exc:
            _log(f"通知模块注入失败（不影响使用）：{exc!r}")

    _log(f"已接入：{app.name}")
