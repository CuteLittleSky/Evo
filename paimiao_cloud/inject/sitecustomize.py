"""注入点：Python 启动时自动导入这个文件（靠 PYTHONPATH 指向本目录）。

这里只做一件事：装一个"等 panel_app 被导入后再动手"的钩子。
不直接 import 项目代码——sitecustomize 在解释器启动最早期运行，
这时项目的模块还不一定能用，而且记录子进程也会继承 PYTHONPATH 跑一遍这个文件。

为什么不在启动时判断"是不是 Evo 主程序"：
    实测 spawn 子进程的 sys.argv[0] 和父进程一样，环境变量也会继承，
    靠它们区分不可靠。而 panel_app 只在主程序里被导入
    （evo_app.py:410 的 import panel_app 在 PanelServer._start_server 里），
    所以"panel_app 被导入"本身就是最准的判据。

注意：这条路径只对**源码运行**有效。打包成 exe 后 site 模块不初始化，
      sitecustomize 不会被导入，那条路走 runtime_hook.py（见 README）。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import cloud.bootstrap as _bootstrap
    _bootstrap.install()
except Exception:
    # 注入失败绝不能影响 Evo 本身：静默降级，Evo 照常运行
    pass
