"""启动 Evo + 云同步注入。用法：python paimiao_cloud\\run_evo.py [Evo 原来的参数...]

两个必须做对的地方（都是实测出来的）：

1. **PYTHONPATH 必须在进 Python 之前就有**。在进程内设 os.environ["PYTHONPATH"]
   不会让 sitecustomize 追溯加载 —— site 模块早就初始化完了。
   所以这里既设环境变量（给 spawn 出来的记录子进程用），
   又**显式导入**自己那份 sitecustomize（给当前进程用）。
2. sys.path 要和直接运行 code/evo_app.py 时一致，否则 Evo 的 `import storage`
   这类平铺导入会失败。
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # paimiao_cloud/
PROJECT = ROOT.parent                           # Evo/
INJECT = ROOT / "inject"
ENTRY = PROJECT / "code" / "evo_app.py"


def main():
    if not ENTRY.exists():
        raise SystemExit(f"找不到 Evo 主程序：{ENTRY}")

    # ① 给子进程（multiprocessing spawn）留好环境变量
    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(INJECT)] + ([existing] if existing else [])
    os.environ["PYTHONPATH"] = os.pathsep.join(parts)

    # ② 当前进程：显式加载注入层（等价于 sitecustomize 被自动导入）
    if str(INJECT) not in sys.path:
        sys.path.insert(0, str(INJECT))
    try:
        import sitecustomize  # noqa: F401  就是 inject/sitecustomize.py
    except Exception as exc:
        print(f"[云同步] 注入失败，Evo 将照常运行：{exc!r}", file=sys.stderr)

    # ③ 和直接运行 code/evo_app.py 的 sys.path 一致
    if str(PROJECT / "code") not in sys.path:
        sys.path.insert(0, str(PROJECT / "code"))

    sys.argv[0] = str(ENTRY)
    runpy.run_path(str(ENTRY), run_name="__main__")


if __name__ == "__main__":
    main()
