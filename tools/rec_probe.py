"""录屏第 0 步实测（开发工具）：用 ffmpeg 录屏，同时每 0.3 秒给 Evo 送一张原图，量 CPU、内存、码率、掉帧，试声音对不对得上、录像里的小字读不读得准。

步骤、怎么看结果见 docs/录屏实测.md。录下的东西在 captures/rec_probe/<时间>/（不进 git）。

  python tools/rec_probe.py check                       这台电脑能用哪些抓屏方式、哪些显卡编码器
  python tools/rec_probe.py record --minutes 10         边打边录，结束时打印结果（Ctrl+C 提前停）
  python tools/rec_probe.py avsync                      声音和画面对不对得上（屏幕左上角闪白、同时嘀一声）
  python tools/rec_probe.py ocr <录像文件夹>             录像里的右上角和原图读出来一样吗
  python tools/rec_probe.py clip <录像文件夹> --last 30  剪最后 30 秒（有声音就带上）看看画质、声音
  python tools/rec_probe.py fps --off a.csv --on b.csv   比开 / 关录屏的帧数（PresentMon 录的）
  python tools/rec_probe.py dxcam --minutes 2           对照：现在的 dxcam 每 0.3 秒截一张占多少 CPU

画面的时间：每一帧的时间戳 = 抓到它时电脑的时钟（ffmpeg 的 RTCTIME）减去开始时刻 t0，所以分段列表、原图、
声音都在同一条时间线上（秒，从 t0 算起）。声音的时刻是 QPC（time.perf_counter 同一个钟），开始时一起记下两个钟的对应。
"""

import argparse
import csv
import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import numpy as np

import code_path  # noqa: F401  让下面能 import 程序代码
import process_info

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "captures" / "rec_probe"
TOOLS = Path(__file__).resolve().parent
FRAME_INTERVAL = 0.3  # 给 Evo 送图的间隔（和对局中一样）
SEGMENT_SECONDS = 2
STALL_SECONDS = 5  # ffmpeg 这么久没出新帧就算卡住：杀掉重开
APEX_EXES = ("r5apex.exe", "r5apex_dx12.exe")
ENCODERS = ["h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_amf", "hevc_amf", "av1_amf", "h264_qsv", "hevc_qsv", "av1_qsv"]
AUTO_ORDER = ["h264_nvenc", "h264_amf", "h264_qsv"]  # NVIDIA > AMD > Intel（Overwolf 的建议），先用 H.264：WebView2 一定能放
CREATE_NEW_PROCESS_GROUP = 0x00000200


# ---------- ffmpeg 在哪、怎么拼命令 ----------

def find_ffmpeg():
    """ffmpeg.exe 的路径：环境变量 EVO_FFMPEG > PATH > winget 装的（Gyan.FFmpeg）。找不到返回 None。"""
    path = os.environ.get("EVO_FFMPEG") or shutil.which("ffmpeg")
    if path:
        return path
    pattern = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe")
    found = sorted(glob.glob(pattern))
    return found[-1] if found else None


def family(encoder):
    return encoder.rsplit("_", 1)[1]


def gpu_chain(encoder, width=None, height=None):
    """显卡上抓到的画面（D3D11 纹理）交给编码器之前的处理，不经过内存。

    原分辨率：NVENC、AMF 直接收抓到的 BGRA 纹理（null = 原样交过去）；Intel 要先映射成 QSV 的格式。
    缩到别的分辨率用 scale_d3d11（2026-11-19 在 RTX 5060 Ti + ffmpeg 9.0.2 上建不起来：Could not create the texture）。"""
    chain = []
    if width:
        chain.append(f"scale_d3d11=width={width}:height={height}:format=nv12")
    if family(encoder) == "qsv":
        chain.append("hwmap=derive_device=qsv,format=qsv")
    return ",".join(chain) or "null"


def memory_chain(width=None, height=None):
    """备用：画面先拷进内存、在 CPU 上转格式再编码（慢、占 CPU，只在 check 里看看能不能用）。"""
    return f"hwdownload,format=bgra,scale={width or 'iw'}:{height or 'ih'},format=nv12"


def default_bitrate(encoder, width, height, fps):
    """按分辨率和帧数估一个码率（bps）：H.264 每像素 0.1 位，HEVC / AV1 打六折。2560×1440 60 帧 H.264 约 22 Mbps。"""
    bpp = 0.1 if encoder.startswith("h264") else 0.06
    return int(width * height * fps * bpp)


def encoder_args(encoder, fps, bitrate):
    args = ["-c:v", encoder, "-b:v", str(bitrate), "-maxrate", str(int(bitrate * 1.5)), "-bufsize", str(bitrate * 2),
            "-g", str(fps * SEGMENT_SECONDS), "-bf", "0",
            "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})"]  # 每 2 秒一个关键帧：每段都能单独开头播放
    fam = family(encoder)
    if fam == "nvenc":
        args += ["-preset", "p4", "-rc", "vbr", "-forced-idr", "1"]
    elif fam == "amf":
        args += ["-quality", "balanced", "-rc", "vbr_peak"]
    elif fam == "qsv":
        args += ["-preset", "medium"]
    return args


def source_filter(source, fps, output_idx=0, window="r5apex"):
    if source == "gfxcapture":
        return f"gfxcapture=window_exe={window}:max_framerate={fps}:capture_cursor=0"
    if source == "gfxcapture-monitor":
        return f"gfxcapture=monitor_idx={output_idx}:max_framerate={fps}:capture_cursor=0"
    return f"ddagrab=output_idx={output_idx}:framerate={fps}:draw_mouse=0"


def run_quiet(cmd, timeout=30):
    """跑一条 ffmpeg 命令，返回 (成功, 输出的最后几行)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "超时"
    text = (r.stderr or "").strip().splitlines()
    return r.returncode == 0, " / ".join(line.strip() for line in text[-3:])


def grab_size(ffmpeg, source):
    """用这种抓法抓一帧，返回 (宽, 高) 或错误信息（字符串）。"""
    r = subprocess.run([ffmpeg, "-hide_banner", "-filter_complex", f"{source},hwdownload,format=bgra,showinfo",
                        "-frames:v", "1", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30)
    m = re.search(r" s:(\d+)x(\d+)", r.stderr or "")
    if m:
        return int(m[1]), int(m[2])
    lines = [line for line in (r.stderr or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"退出码 {r.returncode}"


# ---------- Evo 崩了 ffmpeg 跟着退：Windows 的"作业对象" ----------

class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_job = None


def tie_to_me(proc):
    """把子进程放进一个"本程序退出就全部结束"的作业对象：本程序崩了、被结束，ffmpeg 也跟着退，不会一直往硬盘写。"""
    global _job
    if _job is None:
        _job = _kernel32.CreateJobObjectW(None, None)
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _kernel32.SetInformationJobObject(_job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = 扩展限制
    return bool(_kernel32.AssignProcessToJobObject(_job, int(proc._handle)))


# ---------- 录 ----------

class FrameFeed(threading.Thread):
    """一直从 ffmpeg 读给 Evo 的原图（BMP，自带宽高：游戏改分辨率也读得对），只留最新一张。

    ffmpeg 那边只做"从显卡拷出来"、不转格式（BGRA 原样包成 BMP）：转格式在 ffmpeg 抓屏的同一个线程里，
    会拖慢录像（2026-11-19 实测转成 BGR 时录像有几秒只出 42 帧）。去掉第 4 个通道在这边做（只是换个看法，不拷贝）。

    要一直读：Evo 读结算页时引擎要跑好几秒，这几秒不读，管道一满 ffmpeg 就整个卡住，录像也跟着断。"""

    def __init__(self, stream, start_time):
        super().__init__(daemon=True)
        self.stream, self.start_time = stream, start_time
        self.latest = None  # (这一张的时间（秒，从 t0 算起）, BGR 图)
        self.count = 0
        self.arrivals = []  # 每一张到的时刻（perf_counter）

    def _read(self, n):
        buf = bytearray(n)
        view, got = memoryview(buf), 0
        while got < n:
            k = self.stream.readinto(view[got:])
            if not k:
                return None
            got += k
        return buf

    def run(self):
        while True:
            head = self._read(14)
            if head is None or head[:2] != b"BM":
                return
            total = int.from_bytes(head[2:6], "little")
            rest = self._read(total - 14)
            if rest is None:
                return
            offset = int.from_bytes(head[10:14], "little") - 14
            width = int.from_bytes(rest[4:8], "little", signed=True)
            height = int.from_bytes(rest[8:12], "little", signed=True)
            channels = int.from_bytes(rest[14:16], "little") // 8  # 32 位 = BGRA（第 4 个通道不用）
            stride = (width * channels + 3) // 4 * 4
            rows = np.frombuffer(rest, np.uint8, abs(height) * stride, offset).reshape(abs(height), stride)
            image = rows[:, :width * channels].reshape(abs(height), width, channels)[:, :, :3]
            if height > 0:  # BMP 默认从下往上存
                image = image[::-1]
            self.latest = (self.start_time + self.count * FRAME_INTERVAL, image)
            self.count += 1
            self.arrivals.append(time.perf_counter())


def read_progress(path):
    """ffmpeg -progress 写的最后一段：{键: 值}。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    blocks = text.strip().split("progress=")
    if len(blocks) < 2:
        return {}
    block = blocks[-2] if text.rstrip().endswith(("continue", "end")) else blocks[-1]
    return dict(line.split("=", 1) for line in block.strip().splitlines() if "=" in line)


class Recorder:
    """开一个 ffmpeg：录屏写进 2 秒一段的 MPEG-TS（可选：同时每 0.3 秒送一张原图）。卡住、退出了就重开（记下断了多久）。"""

    def __init__(self, ffmpeg, run_dir, args, t0_us, log):
        self.ffmpeg, self.dir, self.args, self.t0_us, self.log = ffmpeg, run_dir, args, t0_us, log
        self.proc = None
        self.feed = None
        self.runs = 0  # 开过几次 ffmpeg
        self.events = []  # (时刻, 发生了什么, 断了几秒)
        self.last_frame = (0, time.perf_counter())  # (progress 里的帧数, 它最后一次变的时刻)
        self.stopping = False
        (run_dir / "segments").mkdir(parents=True, exist_ok=True)

    def since_t0(self):
        return time.time() - self.t0_us / 1e6

    def command(self):
        a = self.args
        n = self.runs
        width = height = None
        if a.size == "1080p":
            width, height = 1920, 1080
        source = source_filter(a.source, a.fps, a.output_idx, a.window)
        stamp = f"settb=AVTB,setpts=RTCTIME-{self.t0_us}"  # 时间戳 = 抓到这一帧时电脑的时钟 - t0（微秒）
        start = max(0.0, round(self.since_t0() / FRAME_INTERVAL + 1) * FRAME_INTERVAL)
        if a.frames:
            graph = (f"{source},{stamp},split=2[rec][raw];[rec]{gpu_chain(a.encoder, width, height)}[enc];"
                     f"[raw]fps=10/3:start_time={start:.1f},hwdownload,format=bgra[img]")
        else:
            graph = f"{source},{stamp},{gpu_chain(a.encoder, width, height)}[enc]"
        segments = len(list((self.dir / "segments").glob("*.ts")))
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", "-stats_period", "1",
               "-progress", str(self.dir / f"progress_{n}.txt"), "-filter_complex", graph,
               "-map", "[enc]", *encoder_args(a.encoder, a.fps, a.bitrate), "-fps_mode", "vfr", "-enc_time_base:v", "filter",
               "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_time_delta", "0.05",
               "-segment_format", "mpegts", "-segment_list", str(self.dir / f"segments_{n}.csv"),
               "-segment_list_type", "csv", "-segment_start_number", str(segments), "-reset_timestamps", "0",
               str(self.dir / "segments" / "seg_%05d.ts")]
        if a.frames:
            cmd += ["-map", "[img]", "-fps_mode", "passthrough", "-c:v", "bmp", "-f", "image2pipe", "pipe:1"]
        return cmd, start

    def start(self):
        cmd, start = self.command()
        (self.dir / f"ffmpeg_cmd_{self.runs}.txt").write_text(subprocess.list2cmdline(cmd), encoding="utf-8")
        err = open(self.dir / f"ffmpeg_{self.runs}.log", "wb")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE if self.args.frames else None,
                                     stderr=err, bufsize=0, creationflags=CREATE_NEW_PROCESS_GROUP)
        err.close()
        if not tie_to_me(self.proc):
            self.log("⚠ 没能把 ffmpeg 放进作业对象：本程序被强行结束时它不会跟着退")
        if self.args.frames:
            self.feed = FrameFeed(self.proc.stdout, start)
            self.feed.start()
        self.progress_path = self.dir / f"progress_{self.runs}.txt"
        self.runs += 1
        self.last_frame = (0, time.perf_counter())

    def frames(self):
        return int(read_progress(self.progress_path).get("frame", 0) or 0)

    def watch(self):
        """每秒调用一次：ffmpeg 退出了或卡住了就重开。返回这一次进度里的帧数（给 stats 用）。"""
        frames = self.frames()
        now = time.perf_counter()
        if frames != self.last_frame[0]:
            if self.events and self.events[-1][2] is None:  # 重开以后第一次有新帧：记下断了多久
                when, what, _ = self.events[-1]
                self.events[-1] = (when, what, round(now - self._down_since, 1))
                self.log(f"  → 接上了，断了 {now - self._down_since:.1f} 秒")
            self.last_frame = (frames, now)
        if self.stopping:
            return frames
        dead = self.proc.poll() is not None
        stuck = now - self.last_frame[1] > STALL_SECONDS
        if dead or stuck:
            what = f"ffmpeg 退出了（退出码 {self.proc.returncode}）" if dead else f"ffmpeg {STALL_SECONDS} 秒没出新帧（卡住了）"
            tail = Path(self.dir / f"ffmpeg_{self.runs - 1}.log").read_text(encoding="utf-8", errors="replace").strip()
            self.log(f"[{datetime.now():%H:%M:%S}] {what}，重开。ffmpeg 最后说：{tail.splitlines()[-1] if tail else '（没说）'}")
            if not (self.events and self.events[-1][2] is None):
                self._down_since = self.last_frame[1]
                self.events.append((round(self.since_t0(), 1), what, None))
            self.kill()
            self.start()
        return frames

    def stop(self):
        """让 ffmpeg 收尾（写完最后一段）再退。"""
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.kill()
        if self.feed:
            self.feed.join(5)

    def kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)


def nvidia_usage():
    """NVIDIA 显卡的（整体占用 %, 编码器占用 %），没有 nvidia-smi 返回 None。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=utilization.gpu,utilization.encoder", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        gpu, enc = out.strip().splitlines()[0].split(",")
        return float(gpu), float(enc)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


class GpuSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.samples = []
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.wait(5):
            usage = nvidia_usage()
            if usage is None:
                return
            self.samples.append(usage)


def find_pid(names):
    for p in process_info.snapshot().values():
        if p.name in names:
            return p.pid
    return None


def new_run_dir(label):
    name = datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{label}" if label else "")
    path = RUNS / name
    path.mkdir(parents=True)
    return path


def pick_encoder(ffmpeg, wanted, args):
    if wanted != "auto":
        return wanted
    for enc in AUTO_ORDER:
        ok, _ = test_encoder(ffmpeg, enc, args.output_idx)
        if ok:
            return enc
    sys.exit("这台电脑上 NVIDIA / AMD / Intel 的显卡编码器都用不了（先跑 check 看原因）")


def test_encoder(ffmpeg, encoder, output_idx=0, via_memory=False):
    chain = memory_chain() if via_memory else gpu_chain(encoder)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-filter_complex",
           f"{source_filter('ddagrab', 30, output_idx)},{chain}", "-frames:v", "30",
           *encoder_args(encoder, 30, 8_000_000), "-f", "null", "-"]
    return run_quiet(cmd)


def record(args, label=None, audio_pid=None, on_ready=None):
    """录 args.minutes 分钟（或 Ctrl+C）。返回录像文件夹。on_ready()：开始录以后调用一次（avsync 用来开始闪）。"""
    ffmpeg = find_ffmpeg() or sys.exit("找不到 ffmpeg.exe（见 docs/录屏实测.md 第 1 步）")
    args.encoder = pick_encoder(ffmpeg, args.encoder, args)
    size = grab_size(ffmpeg, source_filter(args.source, args.fps, args.output_idx, args.window))
    if isinstance(size, str):
        sys.exit(f"抓不到画面：{size}")
    width, height = (1920, 1080) if args.size == "1080p" else size
    if not args.bitrate:
        args.bitrate = default_bitrate(args.encoder, width, height, args.fps)
    run_dir = new_run_dir(label or args.label)
    log_file = open(run_dir / "log.txt", "a", encoding="utf-8")

    def log(text):
        print(text, flush=True)
        log_file.write(text + "\n")
        log_file.flush()

    t0_us, t0_qpc = time.time_ns() // 1000, time.perf_counter()
    meta = {"t0_us": t0_us, "t0_qpc": t0_qpc, "ffmpeg": ffmpeg, "encoder": args.encoder, "source": args.source,
            "capture_size": list(size), "video_size": [width, height], "fps": args.fps, "bitrate": args.bitrate,
            "frames": args.frames, "ocr_every": args.ocr_every, "started": datetime.now().isoformat(timespec="seconds")}
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"录像文件夹：{run_dir}")
    log(f"抓屏 {args.source} {size[0]}×{size[1]} → 录 {width}×{height} {args.fps} 帧，{args.encoder}，"
        f"{args.bitrate / 1e6:.0f} Mbps；{'每 0.3 秒送一张原图' if args.frames else '不送原图'}")

    audio = None
    if audio_pid:
        import rec_audio
        audio = rec_audio.ProcessAudio(audio_pid, run_dir / "audio.wav")
        audio.start()
        audio.ready.wait(15)
        if audio.error:
            log(f"⚠ 声音录不了：{audio.error}")
            audio = None
        else:
            log(f"同时录进程 {audio_pid} 的声音")

    rec = Recorder(ffmpeg, run_dir, args, t0_us, log)
    rec.start()
    gpu = GpuSampler()
    gpu.start()
    engine = None
    if args.frames and args.ocr_every:
        import ocr
        engine = ocr.create_engine()
    frames_dir = run_dir / "frames"
    frames_dir.mkdir()
    me = os.getpid()
    stats = open(run_dir / "stats.csv", "w", newline="", encoding="utf-8")
    writer = csv.writer(stats)
    writer.writerow(["t", "ffmpeg_cpu_core", "ffmpeg_mb", "probe_cpu_core", "probe_mb", "frames", "raw_frames", "ocr"])
    ocr_windows = []  # (开始, 结束)，秒，从 t0 算起
    end = time.perf_counter() + args.minutes * 60
    last_tick = last_save = last_ocr = time.perf_counter()
    prev = None
    ocr_since_tick = False  # 上一秒以来模拟过读结算页（这一行的 CPU 含引擎）
    if on_ready:
        on_ready()
    log(f"开始录，{args.minutes:g} 分钟后自动停（Ctrl+C 提前停）")
    try:
        while time.perf_counter() < end:
            now = time.perf_counter()
            if rec.feed and rec.feed.latest is not None:  # 像 Evo 一样每 0.3 秒看一眼最新的那张
                when, image = rec.feed.latest
                _ = image[::16, ::16].mean()
                if now - last_save >= args.save_every:
                    last_save = now
                    import cv2
                    cv2.imwrite(str(frames_dir / f"t{round(when * 1000):09d}.png"), image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                if engine is not None and now - last_ocr >= args.ocr_every:  # 模拟读结算页：引擎读整张图，要好几秒
                    last_ocr = now
                    a = rec.since_t0()
                    engine(np.ascontiguousarray(image))
                    ocr_windows.append((round(a, 2), round(rec.since_t0(), 2)))
                    ocr_since_tick = True
                    log(f"[{datetime.now():%H:%M:%S}] 模拟读结算页：引擎读了一整张图 {ocr_windows[-1][1] - a:.1f} 秒")
            if now - last_tick >= 1:
                frames = rec.watch()
                procs = process_info.snapshot()
                ff = procs.get(rec.proc.pid)
                mine = procs.get(me)
                if prev and ff and mine and prev[0] and prev[1]:
                    dt = now - prev[2]
                    writer.writerow([round(rec.since_t0(), 1), round((ff.cpu - prev[0].cpu) / dt * 100, 1)
                                     if ff.pid == prev[0].pid else "", round(ff.memory, 1),
                                     round((mine.cpu - prev[1].cpu) / dt * 100, 1), round(mine.memory, 1), frames,
                                     rec.feed.count if rec.feed else "", int(ocr_since_tick)])
                    stats.flush()
                    ocr_since_tick = False
                prev = (ff, mine, now)
                last_tick = now
            time.sleep(0.05 if not rec.feed else max(0.0, FRAME_INTERVAL - (time.perf_counter() - now)))
    except KeyboardInterrupt:
        log("Ctrl+C：停下")
    finally:
        rec.stop()
        gpu.stop_flag.set()
        stats.close()
        if audio:
            audio.stop()
            (run_dir / "audio.json").write_text(json.dumps({
                "first_qpc": audio.first_time, "gaps": audio.gaps, "error": audio.error, "rate": 48000,
                "packets": audio.packets[::50] + audio.packets[-1:]}, indent=0), encoding="utf-8")
    meta.update({"stopped": datetime.now().isoformat(timespec="seconds"), "wall_seconds": round(rec.since_t0(), 1),
                 "ocr_windows": ocr_windows, "events": rec.events, "runs": rec.runs,
                 "raw_frames": rec.feed.count if rec.feed else None,
                 "raw_arrivals": [round(t - t0_qpc, 3) for t in rec.feed.arrivals] if rec.feed else [],
                 "gpu_samples": gpu.samples})
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log_file.close()
    return run_dir


# ---------- 看结果 ----------

def first_pts(path):
    """一个 .ts 文件里第一帧的时间戳（秒，文件里写的，含 TS 自己加的偏移）。读不到返回 None。"""
    probe = Path(find_ffmpeg()).with_name("ffprobe.exe")
    r = subprocess.run([str(probe), "-v", "error", "-select_streams", "v", "-show_entries", "packet=pts_time",
                        "-read_intervals", "%+#1", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.replace(",", " ").split()[0])
    except (IndexError, ValueError):
        return None


def count_frames(path):
    """一个 .ts 文件里有几帧（读不到返回 None）。"""
    probe = Path(find_ffmpeg()).with_name("ffprobe.exe")
    r = subprocess.run([str(probe), "-v", "error", "-select_streams", "v", "-count_packets", "-show_entries",
                        "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return int(r.stdout.replace(",", " ").split()[0])
    except (IndexError, ValueError):
        return None


def load_segments(run_dir):
    """[(文件, 开始秒, 结束秒)]，按时间排好（秒，从 t0 算起）。

    ffmpeg 的分段列表把每次开 ffmpeg 后的第一段都写成从 0 开始（实际从第一帧开始），这一段按文件里的时间戳改正：
    TS 文件里的时间 = 原来的时间 + 一个固定偏移（实测 1.4 秒），偏移用第二段算出来。"""
    segs = []
    for path in sorted(run_dir.glob("segments_*.csv")):
        rows = [r for r in csv.reader(path.open(encoding="utf-8")) if len(r) >= 3]
        run = [(run_dir / "segments" / r[0], float(r[1]), float(r[2])) for r in rows]
        if run:
            offset = 1.4
            if len(run) >= 2 and (p := first_pts(run[1][0])) is not None:
                offset = p - run[1][1]
            p0 = first_pts(run[0][0])
            if p0 is not None:
                run[0] = (run[0][0], round(p0 - offset, 6), run[0][2])
        segs += run
    return sorted(segs, key=lambda s: s[1])


def summarize(run_dir):
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    segs = load_segments(run_dir)
    lines = []
    out = lines.append
    wall = meta.get("wall_seconds") or 0
    out(f"=== 结果：{run_dir.name} ===")
    out(f"抓屏 {meta['source']}，录 {meta['video_size'][0]}×{meta['video_size'][1]} {meta['fps']} 帧，{meta['encoder']}，"
        f"码率设定 {meta['bitrate'] / 1e6:.0f} Mbps")
    if segs:
        covered = sum(e - s for _, s, e in segs)
        size = sum(f.stat().st_size for f, _, _ in segs if f.exists())
        out(f"录了 {wall / 60:.1f} 分钟，{len(segs)} 段，录到的画面 {covered:.0f} 秒（第一段从 {segs[0][1]:.2f} 秒开始）")
        out(f"文件 {size / 2**20:.0f} MB：平均 {size * 8 / max(covered, 1) / 1e6:.1f} Mbps，每分钟 {size / 2**20 / max(covered, 1) * 60:.0f} MB，"
            f"一小时约 {size / 2**30 / max(covered, 1) * 3600:.1f} GB")
    rows = list(csv.DictReader((run_dir / "stats.csv").open(encoding="utf-8")))

    def avg(key, cond=lambda r: True):
        vals = [float(r[key]) for r in rows if r[key] not in ("", None) and cond(r)]
        return (sum(vals) / len(vals), max(vals)) if vals else (None, None)

    ff_cpu, ff_cpu_max = avg("ffmpeg_cpu_core")
    ff_mb, ff_mb_max = avg("ffmpeg_mb")
    if ff_cpu is not None:
        out(f"ffmpeg：CPU 平均 {ff_cpu:.1f}% 个核（最高 {ff_cpu_max:.0f}%），内存平均 {ff_mb:.0f} MB（最高 {ff_mb_max:.0f} MB）")
    pr_cpu, _ = avg("probe_cpu_core", lambda r: r["ocr"] == "0")
    if pr_cpu is not None and meta["frames"]:
        out(f"Python 这边收图（不算模拟读结算页的那几秒）：CPU 平均 {pr_cpu:.1f}% 个核")
    gpu = meta.get("gpu_samples") or []
    if gpu:
        out(f"NVIDIA 显卡：整体占用平均 {sum(g[0] for g in gpu) / len(gpu):.0f}%，编码器占用平均 {sum(g[1] for g in gpu) / len(gpu):.0f}%"
            "（这两个数含游戏自己；比开、关录屏时的差）")
    # 录像里实际有多少帧：数每一段的帧数（最后一段可能没写完，不算）。少于设定的 90% 的段 = 卡了
    rates = [(s, n / (e - s)) for f, s, e in segs[:-1] if e > s and (n := count_frames(f)) is not None]
    slow = [(s, r) for s, r in rates if r < meta["fps"] * 0.9]
    if rates:
        out(f"录像里实际每秒 {sum(r for _, r in rates) / len(rates):.1f} 帧（设定 {meta['fps']}），"
            + ("没有哪一段明显少" if not slow else f"{len(slow)} 段少于 90%："
               + "，".join(f"{s:.0f} 秒处 {r:.0f} 帧" for s, r in slow[:8]) + (" …" if len(slow) > 8 else "")))
    windows = meta.get("ocr_windows") or []
    if windows:
        hit = [t for t, _ in slow if any(a - SEGMENT_SECONDS <= t <= b for a, b in windows)]
        out(f"模拟读结算页 {len(windows)} 次（每次 {sum(b - a for a, b in windows) / len(windows):.1f} 秒）：其间录像"
            + ("没受影响" if not hit else f"有 {len(hit)} 段帧数明显少（管道堵住了？）"))
    if meta["frames"]:
        arrivals = meta.get("raw_arrivals") or []
        expected = wall / FRAME_INTERVAL
        gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
        out(f"送给 Evo 的原图：收到 {meta.get('raw_frames')} 张（应约 {expected:.0f} 张），两张之间最长隔 "
            f"{max(gaps) if gaps else 0:.2f} 秒")
    events = meta.get("events") or []
    if events:
        out("断了又重开：" + "；".join(f"{t:.0f} 秒处 {what}，断了 {d if d is not None else '?'} 秒" for t, what, d in events))
    else:
        out("没断过")
    audio = run_dir / "audio.json"
    if audio.exists():
        info = json.loads(audio.read_text(encoding="utf-8"))
        out(f"声音：{'录到了' if info.get('first_qpc') else '一包都没收到（程序没出声？）'}，补静音 {info.get('gaps')} 次"
            + (f"，出错：{info['error']}" if info.get("error") else ""))
    text = "\n".join(lines)
    (run_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    return text


# ---------- 子命令 ----------

def cmd_check(args):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("找不到 ffmpeg.exe：先装（见 docs/录屏实测.md 第 1 步），或设环境变量 EVO_FFMPEG 指向它")
        return 1
    version = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    print(f"ffmpeg：{ffmpeg}\n  {version}")
    filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    for name in ("ddagrab", "gfxcapture", "scale_d3d11"):
        print(f"  {name}：{'有' if re.search(rf'\b{name}\b', filters) else '没有（ffmpeg 太旧？要 8.0 以上）'}")

    print("\n屏幕（ddagrab = 桌面复制，和 dxcam 同一个接口）：")
    for idx in range(4):
        size = grab_size(ffmpeg, source_filter("ddagrab", 10, idx))
        print(f"  output_idx={idx}：{f'{size[0]}×{size[1]}' if isinstance(size, tuple) else '没有 / ' + size[:80]}")
        if isinstance(size, str) and idx > 0:
            break
    size = grab_size(ffmpeg, source_filter("gfxcapture-monitor", 10, 0))
    print(f"  gfxcapture（Windows Graphics Capture）整个屏幕：{f'{size[0]}×{size[1]}' if isinstance(size, tuple) else '不行：' + size[:100]}")
    if find_pid(APEX_EXES):
        size = grab_size(ffmpeg, source_filter("gfxcapture", 10))
        print(f"  gfxcapture 只抓 Apex 的窗口：{f'{size[0]}×{size[1]}' if isinstance(size, tuple) else '不行：' + size[:100]}")
    else:
        print("  gfxcapture 只抓 Apex 的窗口：Apex 没开，开着游戏时再跑一次 check 看")

    print("\n显卡编码器（画面不出显卡，直接交给编码器）：")
    working = []
    for enc in ENCODERS:
        ok, why = test_encoder(ffmpeg, enc, args.output_idx)
        if ok:
            working.append(enc)
            print(f"  ✓ {enc}")
            continue
        ok2, _ = test_encoder(ffmpeg, enc, args.output_idx, via_memory=True)
        print(f"  {'△' if ok2 else '✗'} {enc}" + ("：只能先拷进内存再编码（多占 CPU，双显卡电脑上的另一块显卡常这样）" if ok2 else f"：{why[:90]}"))

    print("\n只录一个程序的声音（Windows 的按进程录音）：", end="")
    try:
        import rec_audio
        test = rec_audio.ProcessAudio(os.getpid(), RUNS.parent / "rec_probe_audio_check.wav")
        RUNS.parent.mkdir(exist_ok=True)
        test.start()
        test.ready.wait(15)
        print("可以" if not test.error else f"不行：{test.error}")
        test.stop()
        (RUNS.parent / "rec_probe_audio_check.wav").unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        print(f"不行：{e!r}")

    print("Evo 崩了 ffmpeg 会不会跟着退（作业对象）：", end="", flush=True)
    child = subprocess.Popen([sys.executable, __file__, "_jobtest"], stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline().strip()
    if line.isdigit():
        child.kill()
        child.wait()
        time.sleep(1.5)
        print("会" if int(line) not in process_info.snapshot() else "不会！ffmpeg 还在跑（要手动结束它）")
    else:
        child.kill()
        print(f"没测成：{line}")
    print("NVIDIA 显卡占用（nvidia-smi）：", "能读" if nvidia_usage() else "读不到（不是 N 卡或没装 nvidia-smi，跳过）")
    if working:
        best = next((e for e in AUTO_ORDER if e in working), working[0])
        print(f"\n下一步：python tools/rec_probe.py record --minutes 10   （会用 {best}）")
    return 0


def cmd_jobtest(_args):
    ffmpeg = find_ffmpeg()
    proc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-filter_complex", "ddagrab=framerate=5",
                             "-f", "null", "-"], stdin=subprocess.DEVNULL)
    tie_to_me(proc)
    print(proc.pid, flush=True)
    time.sleep(60)


def cmd_record(args):
    audio_pid = None
    if args.audio == "apex":
        audio_pid = find_pid(APEX_EXES)
        if not audio_pid:
            print("Apex 没开：这次不录声音（要录声音就先开游戏再跑）")
    elif args.audio and args.audio.isdigit():
        audio_pid = int(args.audio)
    run_dir = record(args, audio_pid=audio_pid)
    print()
    print(summarize(run_dir))
    print(f"\n（存在 {run_dir / 'summary.txt'}）")
    return 0


FLASH_REGION = 200  # 左上角这么大一块（像素）用来认闪白


def cmd_flasher(args):
    """avsync 用：左上角一个黑方块，收到"开始"（标准输入的一行）后每 2 秒闪白 0.1 秒、同时嘀一声。

    嘀声由本进程发出（avsync 只录本进程的声音）。到时间一定关掉（出错也关），不会把方块留在屏幕上。"""
    import math
    import struct
    import tempfile
    import tkinter as tk
    import wave
    import winsound
    beep = Path(tempfile.gettempdir()) / "evo_rec_probe_beep.wav"
    with wave.open(str(beep), "wb") as w:  # winsound 不能异步播放内存里的声音，先写成文件
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 1000 * i / 48000))) for i in range(4800)))
    go = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.readline(), go.set()), daemon=True).start()
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.geometry("320x320+0+0")
    root.configure(bg="black")
    root.after(int((args.seconds + 30) * 1000), root.destroy)  # 保险：最多开这么久
    state = {"end": None}
    asked = []

    def black():
        root.configure(bg="black")

    def flash():
        try:
            if time.perf_counter() > state["end"]:
                root.destroy()
                return
            asked.append(time.perf_counter())  # 让它闪、让它响的时刻（QPC）
            root.configure(bg="white")
            root.update_idletasks()
            winsound.PlaySound(str(beep), winsound.SND_FILENAME | winsound.SND_ASYNC)
            root.after(100, black)
        finally:
            root.after(2000, flash)

    def wait_for_go():
        if go.is_set():
            state["end"] = time.perf_counter() + args.seconds
            root.after(1000, flash)
        else:
            root.after(50, wait_for_go)

    wait_for_go()
    root.mainloop()
    if args.log:
        Path(args.log).write_text(json.dumps(asked), encoding="utf-8")


def detect_flashes(ffmpeg, run_dir, segs):
    """录像里左上角变白的时刻（秒，从 t0 算起）。

    把 .ts 文件首尾相连地读（concat: 协议），用文件里原来的时间戳（-copyts）。不用 ffmpeg 的拼接列表（concat 分离器）：
    它按每段的"时长"排下一段，帧间隔不均匀时每段差两三毫秒，越接越偏（2026-11-19 实测 10 秒偏了十几毫秒）。"""
    offset = first_pts(segs[1][0]) - segs[1][1] if len(segs) > 1 else 1.4  # TS 文件里的时间比原来的多出这么多
    joined = "concat:" + "|".join(f.name for f, _, _ in segs)
    r = subprocess.run([ffmpeg, "-hide_banner", "-copyts", "-i", joined, "-vf",
                        f"crop={FLASH_REGION}:{FLASH_REGION}:0:0,format=gray,showinfo", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=segs[0][0].parent)
    times, bright = [], False
    for m in re.finditer(r"pts_time:([\d.]+).*?mean:\[(\d+)", r.stderr, re.S):
        t, mean = float(m[1]), int(m[2])
        if mean > 128 and not bright:
            times.append(t - offset)
        bright = mean > 128
    return times


def detect_beeps(run_dir, t0_qpc):
    """声音里嘀声开始的时刻（秒，从 t0 算起）：按每一包 Windows 给的时刻算，不靠数采样（避开声卡时钟的漂移）。"""
    import wave
    info = json.loads((run_dir / "audio.json").read_text(encoding="utf-8"))
    with wave.open(str(run_dir / "audio.wav")) as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, 2)[:, 0].astype(np.int32)
    if not len(samples) or not info.get("packets"):
        return []
    loud = np.flatnonzero(np.abs(samples) > max(100, np.abs(samples).max() * 0.3))
    if not len(loud):
        return []
    onsets = [loud[0]] + [j for i, j in zip(loud, loud[1:]) if j - i > 48000 * 0.5]
    packets = info["packets"]
    idx = np.array([p[0] for p in packets])
    qpc = np.array([p[1] for p in packets])
    out = []
    for o in onsets:
        k = max(0, np.searchsorted(idx, o, side="right") - 1)
        out.append(qpc[k] + (o - idx[k]) / 48000 - t0_qpc)
    return out


def cmd_avsync(args):
    ffmpeg = find_ffmpeg() or sys.exit("找不到 ffmpeg.exe")
    asked_log = RUNS / f"flasher_{os.getpid()}.json"
    RUNS.mkdir(parents=True, exist_ok=True)
    flasher = subprocess.Popen([sys.executable, __file__, "_flasher", "--seconds", str(args.seconds), "--log", str(asked_log)],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    print(f"左上角会出现一个黑方块，每 2 秒闪白、嘀一声，共 {args.seconds} 秒。期间别把别的窗口拖到它上面。")
    rec_args = argparse.Namespace(minutes=(args.seconds + 4) / 60, encoder=args.encoder, fps=60, size="native",
                                  source="ddagrab", output_idx=args.output_idx, window="", bitrate=None, frames=False,
                                  ocr_every=0, save_every=10 ** 9, label="avsync")

    def go():  # 开始录了才开始闪
        flasher.stdin.write("go" + chr(10))
        flasher.stdin.flush()

    try:
        run_dir = record(rec_args, label="avsync", audio_pid=flasher.pid, on_ready=go)
    finally:
        try:
            flasher.wait(10)
        except subprocess.TimeoutExpired:
            flasher.kill()
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    segs = load_segments(run_dir)
    flashes = detect_flashes(ffmpeg, run_dir, segs)
    beeps = detect_beeps(run_dir, meta["t0_qpc"])
    asked = []
    if asked_log.exists():
        asked = [t - meta["t0_qpc"] for t in json.loads(asked_log.read_text(encoding="utf-8"))]
        asked_log.replace(run_dir / "flasher.json")
    pairs = []  # (闪白的时刻, 声音晚多少, 画面从"让它闪"到录进去多久, 声音从"让它响"到录进去多久)
    for f in flashes:
        near = [b for b in beeps if abs(b - f) < 0.5]
        req = [a for a in asked if -0.05 < f - a < 0.5]
        if near:
            b = min(near, key=lambda x: abs(x - f))
            pairs.append((f, b - f, f - req[-1] if req else None, b - req[-1] if req else None))
    print(f"\n认出闪白 {len(flashes)} 次、嘀声 {len(beeps)} 次，配上 {len(pairs)} 对（第一对不算：第一次播放声音启动慢）")
    pairs = pairs[1:]
    if len(pairs) < 3:
        print("配上的太少，没法算（看看录像文件夹里的 audio.wav 有没有声音、左上角是不是被挡住了）")
        return 1
    offsets = np.array([p[1] for p in pairs]) * 1000
    times = np.array([p[0] for p in pairs])
    lines = [f"录像里声音比画面晚：中位数 {np.median(offsets):.0f} 毫秒（{offsets.min():.0f} ~ {offsets.max():.0f}，{len(pairs)} 次）"]
    video = np.array([p[2] * 1000 for p in pairs if p[2] is not None])
    audio = np.array([p[3] * 1000 for p in pairs if p[3] is not None])
    if len(video) and len(audio):
        lines.append(f"  画面：从让方块变白到录进去 中位数 {np.median(video):.0f} 毫秒（{video.min():.0f} ~ {video.max():.0f}）")
        lines.append(f"  声音：从让它嘀到录进去   中位数 {np.median(audio):.0f} 毫秒（{audio.min():.0f} ~ {audio.max():.0f}）")
        lines.append("  （这两段是测试用的方块、嘀声自己的延迟，游戏里也有类似的；哪边忽快忽慢，录像里的差就跟着跳）")
    if times[-1] - times[0] >= 60:
        slope = np.polyfit(times, offsets, 1)[0]
        lines.append(f"越录越漂：每 10 分钟 {slope * 600:+.0f} 毫秒")
    else:
        lines.append("（要看会不会越录越漂，用 --seconds 600 录 10 分钟）")
    lines.append("参考：声音比画面晚 100 毫秒以内、早 30 毫秒以内，一般看不出来")
    text = "\n".join(lines)
    print(text)
    (run_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    return 0


def frame_at(ffmpeg, segs, t):
    """录像里 t 秒（从 t0 算起）的那一帧（BGR），找不到返回 None。"""
    import cv2
    for f, s, e in segs:
        if s <= t < e:
            # -ss 放在 -i 后面（从头解到那一帧）：放在前面时 2 秒一段的 TS 文件常常跳不过去、一帧都拿不到
            r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(f), "-ss", f"{t - s:.3f}",
                                "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True)
            if r.stdout:
                return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
    return None


def cmd_ocr(args):
    import cv2
    import hud
    import ocr
    run_dir = Path(args.run).resolve()
    ffmpeg = find_ffmpeg() or sys.exit("找不到 ffmpeg.exe")
    segs = load_segments(run_dir)
    shots = sorted((run_dir / "frames").glob("t*.png"))
    if not shots:
        sys.exit("这个文件夹里没有原图（frames/），录的时候要送原图（不加 --no-frames）")
    reader = hud.Hud(ocr.create_engine(ocr.SMALL_CROP_PARAMS), lang=args.lang)  # 和 Evo 读右上角时一样的参数
    same = differ = 0
    rows = []
    for shot in shots:
        t = int(shot.stem[1:]) / 1000
        raw = cv2.imread(str(shot))
        video = frame_at(ffmpeg, segs, t)
        if video is None:
            continue
        a, _, shown_a = reader.read_top(reader.top_snip(raw))
        if not shown_a:
            continue  # 这一张不在对局中（右上角什么都没有）
        b, _, _ = reader.read_top(reader.top_snip(video))
        diff = float(np.mean(cv2.absdiff(cv2.resize(video, (raw.shape[1], raw.shape[0])), raw)))
        for key in hud.TOP_FIELDS:
            if a.get(key) is None and b.get(key) is None:
                continue
            if a.get(key) == b.get(key):
                same += 1
            else:
                differ += 1
        mark = "一样" if all(a.get(k) == b.get(k) for k in hud.TOP_FIELDS) else "不一样"
        rows.append(f"  {t:7.1f} 秒  原图 {fmt_top(a)}\n            录像 {fmt_top(b)}  → {mark}（整张图平均差 {diff:.1f}）")
    print(f"{len(rows)} 张在对局中的原图（不在对局中的跳过）：")
    print("\n".join(rows))
    if same + differ:
        print(f"\n逐项比：一样 {same} 项，不一样 {differ} 项（{same / (same + differ) * 100:.0f}% 一样）")
    return 0


def fmt_top(values):
    import hud
    return " ".join(f"{hud.TOP_LABELS.get(k, k)}={values.get(k)}" for k in ("kills", "assists", "damage", "squads_left", "rp_delta")
                    if values.get(k) is not None) or "（读不出）"


def cmd_clip(args):
    run_dir = Path(args.run).resolve()
    ffmpeg = find_ffmpeg() or sys.exit("找不到 ffmpeg.exe")
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    segs = load_segments(run_dir)
    if not segs:
        sys.exit("没有录像分段")
    if args.at is not None:
        a, b = args.at - args.before, args.at + args.after
    else:
        b = segs[-1][2]
        a = b - args.last
    chosen = [s for s in segs if s[2] > a and s[1] < b]  # 只在 2 秒一段的边上切：宁可多一点
    if not chosen:
        sys.exit("这段时间没有录像")
    out = run_dir / f"clip_{chosen[0][1]:.0f}-{chosen[-1][2]:.0f}.mp4"
    # 分段首尾相连地读（concat: 协议，不用拼接列表：见 detect_flashes），画面从第一段的第一帧算起
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", "concat:" + "|".join(f.name for f, _, _ in chosen)]
    audio = run_dir / "audio.json"
    info = json.loads(audio.read_text(encoding="utf-8")) if audio.exists() else {}
    if info.get("packets"):
        # 片段开头那一刻对应 WAV 里第几个采样：用离它最近的一包声音的时刻算（不从 WAV 开头数，避开声卡时钟慢慢漂）
        start = chosen[0][1] + meta["t0_qpc"]
        index, when = min(info["packets"], key=lambda p: abs(p[1] - start))
        offset = (index + (start - when) * info["rate"]) / info["rate"]  # 秒
        cmd += (["-ss", f"{offset:.4f}"] if offset >= 0 else ["-itsoffset", f"{-offset:.4f}"]) + ["-i", str(run_dir / "audio.wav")]
        cmd += ["-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "copy", "-movflags", "+faststart", "-t", f"{chosen[-1][2] - chosen[0][1]:.3f}", str(out.resolve())]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=chosen[0][0].parent)
    if r.returncode:
        print(r.stderr.strip()[-500:])
        return 1
    print(f"剪好了（不重新编码）：{out}  {out.stat().st_size / 2**20:.0f} MB")
    os.startfile(out) if args.open else None
    return 0


def read_presentmon(path):
    """PresentMon 的 CSV → 每帧间隔（毫秒）的 numpy 数组（只要 Apex 的行）。"""
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    key = next((k for k in ("MsBetweenPresents", "msBetweenPresents", "FrameTime") if rows and k in rows[0]), None)
    if not key:
        sys.exit(f"{path} 里没有 MsBetweenPresents 这一列（是 PresentMon 录的吗？）")
    apex = [r for r in rows if r.get("Application", "").lower() in APEX_EXES] or rows
    return np.array([float(r[key]) for r in apex if r[key] not in ("", "NA")])


def fps_numbers(ms):
    return 1000 / ms.mean(), 1000 / np.percentile(ms, 99)


def cmd_fps(args):
    groups = {"关录屏": args.off, "开录屏": args.on}
    result = {}
    for name, files in groups.items():
        all_ms = []
        for f in files:
            ms = read_presentmon(f)
            avg, low = fps_numbers(ms)
            print(f"  {name}  {Path(f).name}：平均 {avg:.0f} 帧，1% 低帧 {low:.0f}（{len(ms)} 帧）")
            all_ms.append(ms)
        result[name] = fps_numbers(np.concatenate(all_ms))
    (a_avg, a_low), (b_avg, b_low) = result["关录屏"], result["开录屏"]
    print(f"\n关录屏：平均 {a_avg:.0f} 帧，1% 低帧 {a_low:.0f}")
    print(f"开录屏：平均 {b_avg:.0f} 帧，1% 低帧 {b_low:.0f}")
    print(f"开了以后：平均帧数 {(b_avg - a_avg) / a_avg * 100:+.1f}%，1% 低帧 {(b_low - a_low) / a_low * 100:+.1f}%")
    return 0


def cmd_dxcam(args):
    import capture
    camera, how = capture.open_camera(log=print, restart=False)
    me = os.getpid()
    print(f"用 {how['截法']} 每 0.3 秒截一张，{args.minutes:g} 分钟……")
    start = process_info.snapshot()[me].cpu
    t = time.perf_counter()
    end = t + args.minutes * 60
    n = 0
    while time.perf_counter() < end:
        camera.grab(new_frame_only=False)
        n += 1
        time.sleep(FRAME_INTERVAL)
    busy = process_info.snapshot()[me].cpu - start
    wall = time.perf_counter() - t
    print(f"截了 {n} 张：CPU 平均 {busy / wall * 100:.1f}% 个核")
    start, t = process_info.snapshot()[me].cpu, time.perf_counter()
    time.sleep(5)
    print(f"停截以后（截屏对象还开着）5 秒：{(process_info.snapshot()[me].cpu - start) / (time.perf_counter() - t) * 100:.1f}% 个核"
          "（todo 里说的源码版 dxcam 空转：这里高就是它）")
    camera.release()
    return 0


def main():
    p = argparse.ArgumentParser(description="录屏第 0 步实测（见 docs/录屏实测.md）")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="能用哪些抓屏方式、显卡编码器")
    c.add_argument("--output-idx", type=int, default=0)
    r = sub.add_parser("record", help="录（边打边录）")
    r.add_argument("--minutes", type=float, default=10)
    r.add_argument("--encoder", default="auto", help="auto 或 check 里打勾的，如 hevc_nvenc、av1_nvenc")
    r.add_argument("--fps", type=int, default=60)
    r.add_argument("--size", choices=["native", "1080p"], default="native", help="录原分辨率还是缩到 1080p（在显卡上缩）")
    r.add_argument("--bitrate", type=int, default=None, help="码率（bps），不给就按分辨率估")
    r.add_argument("--source", choices=["ddagrab", "gfxcapture", "gfxcapture-monitor"], default="ddagrab",
                   help="ddagrab = 桌面复制（推荐）；gfxcapture = 只抓 Apex 的窗口")
    r.add_argument("--window", default="r5apex", help="gfxcapture 抓哪个程序的窗口（程序名的正则）")
    r.add_argument("--output-idx", type=int, default=0, help="抓哪个屏幕（check 里列的）")
    r.add_argument("--no-frames", dest="frames", action="store_false", help="不给 Evo 送原图（只录屏，量纯录屏的开销）")
    r.add_argument("--ocr-every", type=float, default=0,
                   help="每隔多少秒模拟一次读结算页（引擎读整张图，占好几个核：对局中别开）；0 = 不模拟")
    r.add_argument("--save-every", type=float, default=15, help="每隔多少秒存一张原图（ocr 子命令用）")
    r.add_argument("--audio", default="apex", help="apex = 录 Apex 的声音；none = 不录；或一个进程号")
    r.add_argument("--label", default="", help="录像文件夹名字后面加的说明，如 1080p")
    a = sub.add_parser("avsync", help="声音和画面对不对得上")
    a.add_argument("--seconds", type=int, default=60)
    a.add_argument("--encoder", default="auto")
    a.add_argument("--output-idx", type=int, default=0)
    o = sub.add_parser("ocr", help="录像里的右上角和原图读出来一样吗")
    o.add_argument("run")
    o.add_argument("--lang", default=None, help="游戏语言（不给 = 默认，简体中文）")
    k = sub.add_parser("clip", help="剪一段看看")
    k.add_argument("run")
    k.add_argument("--last", type=float, default=30)
    k.add_argument("--at", type=float, default=None, help="以第几秒（从开始录算起）为中心")
    k.add_argument("--before", type=float, default=12)
    k.add_argument("--after", type=float, default=4)
    k.add_argument("--open", action="store_true", help="剪好就打开播放")
    f = sub.add_parser("fps", help="比开 / 关录屏的帧数（PresentMon 的 CSV）")
    f.add_argument("--off", nargs="+", required=True)
    f.add_argument("--on", nargs="+", required=True)
    d = sub.add_parser("dxcam", help="对照：dxcam 每 0.3 秒截一张占多少 CPU")
    d.add_argument("--minutes", type=float, default=2)
    fl = sub.add_parser("_flasher")
    fl.add_argument("--seconds", type=int, default=60)
    fl.add_argument("--log", default=None)
    sub.add_parser("_jobtest")
    args = p.parse_args()
    if getattr(args, "audio", None) == "none":
        args.audio = None
    return {"check": cmd_check, "record": cmd_record, "avsync": cmd_avsync, "ocr": cmd_ocr, "clip": cmd_clip,
            "fps": cmd_fps, "dxcam": cmd_dxcam, "_flasher": cmd_flasher, "_jobtest": cmd_jobtest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
