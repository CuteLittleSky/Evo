"""打一个带派喵云同步的 Evo（上游 tools/build.py 一个字不动）。

用法：
    python paimiao_cloud/build_evo.py

它做的事和 tools/build.py 一样（生成图标 → 准备 WebView2 → PyInstaller → 放许可证），
只有打包那一步换成我们的 paimiao_cloud/evo.spec —— 那个 spec 会 exec 上游的
tools/evo.spec，所以上游改了配置这里自动跟着变。

为什么不直接改 tools/build.py 加个 --spec 参数：
    那就要动上游文件，升级时多一处冲突点。这里用「导入上游模块、换掉它
    main() 里的那一步」的方式绕开 —— 见下面 _patch_build()。
"""

import importlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = os.path.join(HERE, "evo.spec")


def _patch_build():
    """导入 tools/build.py，把它 subprocess.run 里的 spec 换成我们的。

    上游 build.py:86 是
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "tools/evo.spec"], ...)
    我们不复制这段（上游改了参数这里会跟着变），而是在模块自己的命名空间里
    包一层 subprocess.run，只替换命令行里的 spec 路径。
    """
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    build = importlib.import_module("build")

    real_run = subprocess.run

    def patched(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any(str(x).endswith("evo.spec") for x in cmd):
            cmd = [SPEC if str(x) == "tools/evo.spec" else x for x in cmd]
            print(f"打包配置：{SPEC}")
        return real_run(cmd, *args, **kwargs)

    build.subprocess.run = patched
    return build


def main():
    if not os.path.isfile(SPEC):
        raise SystemExit(f"找不到 {SPEC}")
    build = _patch_build()
    try:
        build.main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"打包失败：{exc}")
    print(f"\n产物：{os.path.join(ROOT, 'dist', 'Evo', 'Evo.exe')}")


if __name__ == "__main__":
    main()
