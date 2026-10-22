"""Evo 派喵云同步 · 零侵入注入层。

设计约束（来自用户的硬要求）：
    对 Evo 上游仓库的改动必须是 **0 个文件、0 行**。
    所有新增内容都放在 paimiao_cloud/ 这一个文件夹里，靠注入接进去。

两种运行形态，注入方式不同（已实测，见 paimiao_cloud/README.md）：

  源码运行   run_evo.py 设 PYTHONPATH=paimiao_cloud/inject 再启动 code/evo_app.py
              → inject/sitecustomize.py 被解释器自动导入
  打包运行   PyInstaller 的 runtime_hooks 指向 paimiao_cloud/runtime_hook.py
              → 原因：打包后 site 模块根本不初始化（实测 site=False），
                PYTHONPATH 也不会进 sys.path，sitecustomize 这条路走不通

两条路最后都落到 install()。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # paimiao_cloud/
_TEMPLATES = os.path.join(_HERE, "templates")

_installed = False


class _PanelAppFinder:
    """在 panel_app 首次被导入时接进去。

    为什么用 meta_path 钩子而不是直接 import panel_app：
        直接 import 会把 panel_app 提前加载，等于改变 Evo 的加载顺序；
        而我们只想在"它已经自己加载完"之后动手。同时这个钩子对
        记录子进程（同样继承 PYTHONPATH）是无害的 —— 它从没导入 panel_app。
    """

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
    """包住 panel_app 真正的 loader，等模块执行完再调用 attach()。"""

    def __init__(self, inner):
        self._inner = inner

    def create_module(self, spec):
        make = getattr(self._inner, "create_module", None)
        return make(spec) if make else None

    def exec_module(self, module):
        self._inner.exec_module(module)
        try:
            attach(module)
        except Exception as exc:      # 注入失败绝不能让 Evo 起不来
            _log(f"注入失败（Evo 本身不受影响）：{exc!r}")


def install():
    """装钩子。源码版和打包版都调用这一个。"""
    global _installed
    if _installed:
        return
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    # 已经加载过就直接接（比如 panel_app 早于我们被导入）
    module = sys.modules.get("panel_app")
    if module is not None:
        _installed = True
        attach(module)
        return
    sys.meta_path.insert(0, _PanelAppFinder())
    _install_flask_hook()
    _installed = True


def _install_flask_hook():
    """第二个注入点：直接运行 code/panel_app.py 时，模块名是 __main__ 而不是 panel_app，
    meta_path 钩子不会触发（实测：这时页面上没有派喵云同步）。

    但那条路径最后一定要调 Flask.run()，而且那时 app.config["DB"] 已经设好了
    （panel_app.py:906），所以在这里补一刀最合适。
    """
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
    """panel_app 加载完，app 对象已经存在。在这里挂上我们的一切。"""
    from cloud import notice as cloud_notice
    from cloud import panel as cloud_panel
    from cloud import sync as cloud_sync
    from cloud import upstream
    from cloud.templates import splice_loader

    app = module.app

    # ① 模板：在 Evo 自己的目录后面加一个我们的目录，好让 render_template 找得到
    #    cloud_page.html。**不碰 settings.html、不拼接任何字符串。**
    if not getattr(app, "_cloud_loader_installed", False):
        app.jinja_loader = splice_loader(app.jinja_loader, _TEMPLATES)
        app._cloud_loader_installed = True

    # ①.5 派喵版差异：更新地址、源代码链接、去掉意见反馈、隐私措辞。
    #      全部在内存里改（常量改属性、模板文本走 jinja preprocess），磁盘文件不动。
    #      必须在建好 jinja 环境之后调用，钩子才有地方装。
    if not getattr(app, "_cloud_upstream_patched", False):
        app._cloud_upstream_patched = True
        upstream.apply(module, app.jinja_env)

    # ①.6 分支说明弹窗 + 设置页的许可卡片。同样纯内存注入，上游 0 文件改动。
    #      排在 upstream 之后：两层 preprocess 是叠加的，先上游措辞、后分支内容，
    #      顺序反过来没坏处，但"先上游后分支"读起来更清楚。
    if not getattr(app, "_cloud_notice_installed", False):
        app._cloud_notice_installed = True
        cloud_notice.register(app, module)

    # ② 路由：Blueprint 自动继承 panel_app 的 before_request 安全策略
    #    （只认 127.0.0.1/localhost；POST 必须带 token）。
    if not app.blueprints.get("cloud"):
        cloud_panel.MODULE = module        # nav_items() 要用它读 NAV
        app.register_blueprint(cloud_panel.build())
        # 面板可能用 --db 指向别的库（演示数据），同步要跟着那个库走，
        # 不能用 paths.DB_PATH
        cloud_sync.set_db_path(app.config.get("DB"))

    # ③ 后台同步线程：等第一个请求进来再启动，避免拖慢 Evo 启动
    if not getattr(app, "_cloud_sync_armed", False):
        app._cloud_sync_armed = True
        app.before_request(cloud_sync.arm_once)

    _log(f"已接入：{app.name}")
