"""采集卡 / OBS 虚拟摄像头的录像：把 Evo 自己读到的画面经管道交给 ffmpeg 编码（2026-11-23，纯注入）。

为什么这样做：这两种来源的画面不在任何窗口里，是 Evo 用 OpenCV（DirectShow）直接读设备的
（`cloud/capture/frame.py` 的 DirectShowCamera）。设备被 Evo 占着，ffmpeg 再去开同一台设备会失败；
而且录像用的精简版 ffmpeg 也没有 DirectShow。所以由 Evo 把读到的画面原样送过去，最省事也最稳：

- 相机切到「连续读」（`start_pump`）：后台线程按设备的帧率一直读，最新一帧给 Evo 识别和录像共用；
  一局录完就切回去（`stop_pump`）。没开录像的玩家什么都不变。
- 送画面的线程按设置的帧数（30 / 60）往 ffmpeg 的 stdin 写 yuv420p 的原始画面（比 BGR 少一半数据），
  采集卡没信号、在重连时写黑帧：录像不断，时间轴也不乱。
- 其余全部沿用上游 `recorder.Recording`：硬件编码（NVENC / AMF / QSV）、每次开 ffmpeg 一段 .ts、
  断了重开、拼 MP4、声音对齐、事件、剪高光。

怎么接进上游（不改 recorder.py）：
1. 开录前把 `options.source` 设成 `lavfi:color=…`（和设备一样大、一样的帧数的黑画面）：上游量画面大小、
   挑编码器的那几步按「测试画面」走，原样能跑。
2. 包 `Recording._command`：上游拼好的命令里，把开头那段测试画面换成 `[0:v]`，前面加 `-f rawvideo … -i pipe:0`。
3. 包 `Recording._launch`：ffmpeg 开起来以后开送画面的线程（断了重开时换新的线程）。
4. 包 `Recording._stop_ffmpeg`：上游是往 stdin 写 "q" 让 ffmpeg 收尾，这里 stdin 是画面的管道 ——
   改成先停送画面的线程、再关掉 stdin，ffmpeg 读到结尾自己收尾。
5. 包 `Recording.finish`：录完让相机停止连续读。

需要 ffmpeg 有 rawvideo（demuxer + decoder）。上游的精简版没开这两项：用 `python paimiao_cloud/build_ffmpeg.py`
编（在上游的清单上加这两项）。没有时开录报 RecordingUnavailable，上游在日志里说一次。
"""
from __future__ import annotations

import subprocess
import threading
import time

MARK = "_paimiao_pipe"
CREATE_NO_WINDOW = 0x08000000
FIRST_FRAME_WAIT = 3.0  # 开录时等相机连续读出第一帧（拿画面大小），最多这么久
_rawvideo_ok = {}  # ffmpeg 路径 → 有没有 rawvideo


def has_rawvideo(ffmpeg):
    """这个 ffmpeg 能不能从管道读原始画面（rawvideo 的 demuxer 和 decoder 都要有）。结果记住。"""
    if ffmpeg not in _rawvideo_ok:
        ok = False
        try:
            def listed(flag):
                out = subprocess.run([ffmpeg, "-hide_banner", flag], capture_output=True, text=True, timeout=15,
                                     creationflags=CREATE_NO_WINDOW).stdout
                return any(line.split()[1:2] == ["rawvideo"] for line in out.splitlines() if line.strip())
            ok = listed("-demuxers") and listed("-decoders")
        except Exception:  # noqa: BLE001
            ok = False
        _rawvideo_ok[ffmpeg] = ok
    return _rawvideo_ok[ffmpeg]


def _camera():
    try:
        from cloud.capture import frame  # noqa: PLC0415
        return frame.active()
    except Exception:
        return None


def prepare(rec, what, unavailable, log=None):
    """开录前调用（rec.start 之前）：开相机的连续读、量画面大小，把来源换成同样大小的测试画面。
    录不了抛 unavailable。"""
    if not rec.ffmpeg:
        raise unavailable("找不到 ffmpeg")
    if not has_rawvideo(rec.ffmpeg):
        raise unavailable("录像用的 ffmpeg 收不了 Evo 送过去的画面（缺 rawvideo），"
                          "开发者要用 paimiao_cloud/build_ffmpeg.py 重新编")
    camera = _camera()
    if camera is None:
        raise unavailable(f"{what}还没打开")
    camera.start_pump()
    deadline = time.monotonic() + FIRST_FRAME_WAIT
    frame = camera.latest()
    while frame is None and time.monotonic() < deadline:
        time.sleep(0.05)
        frame = camera.latest()
    if frame is not None:
        height, width = frame.shape[:2]
    else:
        width, height = camera.width or 1920, camera.height or 1080
    width, height = max(2, width // 2 * 2), max(2, height // 2 * 2)  # yuv420p 要偶数
    opts = rec.options
    fps = int(getattr(opts, "fps", 60) or 60)
    state = {"camera": camera, "size": (width, height), "fps": fps, "what": what,
             "probe": f"color=c=black:s={width}x{height}:r={fps}", "feeder": None, "stop": None}
    opts.source = "lavfi:" + state["probe"]
    setattr(rec, MARK, state)
    if log:
        log(f"录像：画面来自{what}（Evo 读到的画面，{width}×{height} {fps} 帧"
            f"{'' if frame is not None else '；还没读到画面，先按设备的大小录'}）")
    return state


def release(rec):
    """录完（或开录失败）：停送画面的线程，相机停止连续读。"""
    state = getattr(rec, MARK, None)
    if not state:
        return
    _stop_feeder(state)
    try:
        state["camera"].stop_pump()
    except Exception:
        pass


# ---------- 送画面 ----------

def _feeder(state, proc, stop):
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    width, height = state["size"]
    period = 1.0 / state["fps"]
    black = np.empty(width * height * 3 // 2, dtype=np.uint8)
    black[:width * height] = 16  # yuv420p 的黑：Y = 16，U = V = 128
    black[width * height:] = 128
    black = black.tobytes()
    camera = state["camera"]
    due = time.monotonic()
    while not stop.is_set():
        frame = camera.latest()
        if frame is None:
            data = black
        else:
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            data = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).tobytes()
        try:
            proc.stdin.write(data)
        except (OSError, ValueError):  # ffmpeg 退出了：上游的 poll 会发现并重开
            return
        due += period
        now = time.monotonic()
        if now - due > 1.0:  # 落后太多（电脑卡了一下）：不追，从现在重新数
            due = now
        elif due > now:
            stop.wait(due - now)


def _stop_feeder(state):
    stop, feeder = state.get("stop"), state.get("feeder")
    if stop is not None:
        stop.set()
    if feeder is not None:
        feeder.join(3)
    state["stop"] = state["feeder"] = None


# ---------- 包上游 recorder.Recording ----------

def patch(recorder):
    """包 Recording 的 _command / _launch / _stop_ffmpeg / finish。幂等，返回包上了没有。"""
    cls = getattr(recorder, "Recording", None)
    names = ("_command", "_launch", "_stop_ffmpeg", "finish")
    if cls is None or not all(callable(getattr(cls, n, None)) for n in names):
        return False
    if getattr(cls._command, MARK, False):
        return True
    real_command, real_launch, real_stop, real_finish = (getattr(cls, n) for n in names)

    def _command(self):
        cmd = real_command(self)
        state = getattr(self, MARK, None)
        if not state or cmd is None or "-filter_complex" not in cmd:
            return cmd
        at = cmd.index("-filter_complex")
        graph = cmd[at + 1]
        if not graph.startswith(state["probe"]):
            return cmd  # 上游改了滤镜的写法：照上游（录到的是黑画面，日志里看得出）
        width, height = state["size"]
        source = ["-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}", "-r", str(state["fps"]),
                  "-thread_queue_size", "64", "-i", "pipe:0"]
        return cmd[:at] + source + ["-filter_complex", "[0:v]" + graph[len(state["probe"]):].lstrip(",")] + cmd[at + 2:]

    def _launch(self):
        state = getattr(self, MARK, None)
        if state:
            _stop_feeder(state)
        real_launch(self)
        if state and getattr(self, "proc", None) is not None:
            stop = threading.Event()
            state["stop"] = stop
            state["feeder"] = threading.Thread(target=_feeder, args=(state, self.proc, stop),
                                               name="recording-feeder", daemon=True)
            state["feeder"].start()

    def _stop_ffmpeg(self):
        state = getattr(self, MARK, None)
        if not state:
            return real_stop(self)
        _stop_feeder(state)
        proc = getattr(self, "proc", None)
        if proc and proc.poll() is None:
            try:
                proc.stdin.close()  # 画面送完了：ffmpeg 读到结尾，写完最后几帧自己退
            except OSError:
                pass
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                self._kill()

    def finish(self, info=None):
        try:
            return real_finish(self, info)
        finally:
            release(self)

    for name, fn, real in (("_command", _command, real_command), ("_launch", _launch, real_launch),
                           ("_stop_ffmpeg", _stop_ffmpeg, real_stop), ("finish", finish, real_finish)):
        setattr(fn, MARK, True)
        fn.__wrapped__ = real  # type: ignore[attr-defined]
        setattr(cls, name, fn)
    return True
