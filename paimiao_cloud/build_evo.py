import importlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = os.path.join(HERE, "evo.spec")

def _patch_build():

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
