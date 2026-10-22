"""PyInstaller 运行时钩子（只对打包版生效）。

为什么不复用 sitecustomize：
    实测打包后 `site` 模块根本不初始化（site=False），PYTHONPATH 也不进
    sys.path，所以 Python 永远不会自动导入 sitecustomize.py。
    PyInstaller 的 runtime_hooks 才是打包版的对应机制 —— 它在
    程序主脚本执行**之前**跑，时机正好。

为什么不靠 __file__ 定位自己：
    打包后 runtime hook 是由 PyInstaller 编译进去执行的，__file__ 指向的
    不是我们放进包里的那个文件夹（实测会抛 NameError / 指到别处）。
    所以这里按候选目录依次找，找到哪个有 cloud/bootstrap.py 就用哪个。
"""

import os
import sys


def _candidates():
    """可能放着 cloud/ 包的地方，从最可能到最不可能。"""
    here = globals().get("__file__")
    if here:
        yield os.path.dirname(os.path.abspath(here))
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        yield os.path.join(meipass, "paimiao_cloud")     # evo.spec 把它放这里
        yield meipass
    if getattr(sys, "frozen", False):
        yield os.path.join(os.path.dirname(sys.executable), "_internal", "paimiao_cloud")
        yield os.path.dirname(sys.executable)
    yield os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "paimiao_cloud")


def _root():
    for folder in _candidates():
        if os.path.isfile(os.path.join(folder, "cloud", "bootstrap.py")):
            return folder
    return ""


try:
    _ROOT = _root()
    if _ROOT and _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    import cloud.bootstrap as _bootstrap
    _bootstrap.install()
except Exception:
    # 注入失败绝不能影响 Evo 本身
    pass
