import importlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = os.path.join(HERE, "evo.spec")
# 录像用的精简版 ffmpeg 预编好的产物（LGPL 2.1+，见 BUILD.txt / licenses/）。
# CI 的 runner 上没有 dist/ffmpeg（dist/ 被 gitignore），本机也常常没编；这里放一份编好的，
# 打包时 dist/ffmpeg 不存在就复制过去，免得 tools/build.py 的 add_ffmpeg() 直接 SystemExit。
# 换了新版 ffmpeg（python tools/build_ffmpeg.py）就把这里整份替换掉。
FFMPEG_ASSETS = os.path.join(ROOT, "assets", "ffmpeg")
FFMPEG_DIST = os.path.join(ROOT, "dist", "ffmpeg")


def _ensure_ffmpeg():
    """dist/ffmpeg 没有 ffmpeg.exe 时，从 assets/ffmpeg 复制一份。返回是否就绪。"""
    if os.path.isfile(os.path.join(FFMPEG_DIST, "ffmpeg.exe")):
        return True
    src_exe = os.path.join(FFMPEG_ASSETS, "ffmpeg.exe")
    if not os.path.isfile(src_exe):
        print(f"[ffmpeg] 既没有 {FFMPEG_DIST}，也没有 {FFMPEG_ASSETS}：打包会因为缺录像用的 ffmpeg 失败",
              file=sys.stderr)
        return False
    os.makedirs(FFMPEG_DIST, exist_ok=True)
    for name in os.listdir(FFMPEG_ASSETS):
        s, d = os.path.join(FFMPEG_ASSETS, name), os.path.join(FFMPEG_DIST, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    print(f"[ffmpeg] 从 {FFMPEG_ASSETS} 复制到 {FFMPEG_DIST}（{os.path.getsize(src_exe) / 1024 ** 2:.1f} MB）")
    return True


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
    _ensure_ffmpeg()
    build = _patch_build()
    try:
        build.main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"打包失败：{exc}")
    print(f"\n产物：{os.path.join(ROOT, 'dist', 'Evo', 'Evo.exe')}")

if __name__ == "__main__":
    main()
