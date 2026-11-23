"""编派喵版录像用的精简版 ffmpeg：python paimiao_cloud/build_ffmpeg.py → dist/ffmpeg/（和上游一样的位置，build.py 照常打进安装包）。

和上游 `tools/build_ffmpeg.py` 只差一处：多开 rawvideo（demuxer + decoder）。主机模式（采集卡、OBS 虚拟摄像头）的
录像由 Evo 把读到的画面经管道交给 ffmpeg（`cloud/obs/pipe_recording.py`），ffmpeg 要能读原始画面。
上游文件一个字节都不动：在内存里给上游的 ENABLE 清单加上这两项，再调上游的 main()（下载、核对、编译、许可证、自检全照上游）。
上游的精简版没有 rawvideo 时，采集卡 / 虚拟摄像头的录像在日志里说一次「录不了」，别的照常。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRA = {"demuxer": ["rawvideo"], "decoder": ["rawvideo"]}


def load_upstream():
    spec = importlib.util.spec_from_file_location("build_ffmpeg", ROOT / "tools" / "build_ffmpeg.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    upstream = load_upstream()
    for kind, names in EXTRA.items():
        listed = upstream.ENABLE.setdefault(kind, [])
        listed += [n for n in names if n not in listed]
    print("派喵版：在上游的清单上加 " + "、".join(f"{k} {', '.join(v)}" for k, v in EXTRA.items()))
    return upstream.main()


if __name__ == "__main__":
    sys.exit(main())
