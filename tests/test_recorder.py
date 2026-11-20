"""录像的录制核心（code/recorder.py、process_audio.py、process_info.kill_with_me）。

不开游戏、不抓屏幕：画面用 ffmpeg 自带的测试画面（testsrc2，按真实时间出帧），声音用假的录音（按给定的时刻写 WAV）。
- 算码率、对齐声音（audio_offset、align_wav）、起文件名、读进度、抓屏滤镜、超过上限删最旧的：不用 ffmpeg
- 录 3 秒拼成 MP4：时长、大小、.json 里事件的时刻（MP4 里的位置）
- 中途 ffmpeg 退出：自己重开，断掉的那段记进 .json，MP4 里空档原样保留（断 11 秒也保留：ffmpeg 默认会把 10 秒以上的空档挤掉）
- 声音：假录音在第 2 秒"嘀"一声，MP4 里嘀声的位置对得上画面（30 毫秒以内）
- 上次崩了没拼的分段文件：拼成"_未完成"的 MP4
- 作业对象：开 ffmpeg 的程序被强行结束，ffmpeg 也跟着退
- 找窗口：按进程号找到一个测试窗口（不打开那个进程），只抓这个窗口抓得到
- 接进记录程序（match_recording.py）：开关默认关；一局开始开录、事件（倒地、被扶起、成盒、被复活、冠军、击倒对手、击杀）、右上角变了的时刻、
  结束再录 3 秒、同一局不重开、靶场也录、录不了只说一次
要 ffmpeg（找法见 recorder.find_ffmpeg）和显卡硬件编码器；没有就只跑不用 ffmpeg 的那些。

运行：python tests/test_recorder.py
"""

import json
import math
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import process_info
import recorder

TEST_SOURCE = "lavfi:testsrc2=s=640x360:r=60,realtime"  # realtime：按真实时间出帧，和抓屏一样
TOOLS = Path(__file__).resolve().parent.parent / "tools"


def tone_wav(path, seconds, beep_at, rate=48000):
    """写一段立体声 WAV：静音，第 beep_at 秒开始嘀 0.1 秒（1 kHz）。"""
    frames = bytearray()
    for i in range(int(seconds * rate)):
        t = i / rate
        v = int(8000 * math.sin(2 * math.pi * 1000 * t)) if beep_at <= t < beep_at + 0.1 else 0
        frames += struct.pack("<hh", v, v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


class FakeAudio:
    """假的按进程录音：stop() 时写一段 WAV，声音从 start_qpc 开始，第 beep_at 秒（从 start_qpc 算）嘀一声。"""

    def __init__(self, beep_at):
        self.beep_at = beep_at

    def __call__(self, pid, wav_path):
        self.wav_path = Path(wav_path)
        self.ready = threading.Event()
        self.ready.set()
        self.error = None
        self.gaps = 0
        self.packets = []
        return self

    def start(self):
        self.start_qpc = time.perf_counter()

    def stop(self):
        seconds = time.perf_counter() - self.start_qpc
        tone_wav(self.wav_path, seconds, self.beep_at)
        self.packets = [(i * 480, self.start_qpc + i * 0.01) for i in range(int(seconds * 100))]  # 每包 10 毫秒


def pure_checks(tmp):
    out = {}
    rules = recorder.load_rules()
    out["码率：2560×1440 60 帧 H.264 高画质约 22 Mbps，1080p 约 12 Mbps，AV1 打六折"] = (
        round(recorder.bitrate(rules, 2560, 1440, 60, "h264", "best") / 1e6) == 22
        and round(recorder.bitrate(rules, 1920, 1080, 60, "h264", "best") / 1e6) == 12
        and round(recorder.bitrate(rules, 1920, 1080, 60, "h264", "standard") / 1e5) == 56
        and recorder.bitrate(rules, 1920, 1080, 60, "av1", "best") == int(1920 * 1080 * 60 * 0.1 * 0.6))
    default = recorder.options_from({}, rules)
    old = recorder.options_from({"recording_quality": "low", "recording_size": "4k", "recording_fps": "144",
                                 "recording_codec": "h265"}, rules)
    kept = recorder.options_from({"recording_quality": "small", "recording_size": "720p", "recording_fps": "30"}, rules)
    out["画质设置：默认 1080p 60 帧标准；以前的、乱填的那一档用默认；设过的照用（帧数存成字符串也认）"] = (
        (default.size, default.fps, default.quality, default.codec) == ("1080p", 60, "standard", "h264")
        and (old.size, old.fps, old.quality, old.codec) == ("1080p", 60, "standard", "h264")
        and (kept.size, kept.fps, kept.quality) == ("720p", 30, "small"))
    window = recorder.source_filter(recorder.Options(source="window", size="1080p"), rules, (1920, 1080), hwnd=123)
    no_window = recorder.source_filter(recorder.Options(source="window"), rules)
    desktop = recorder.source_filter(recorder.Options(source="desktop"), rules)
    out["抓屏滤镜：只抓游戏窗口时用窗口句柄（不按程序名找：ffmpeg 会打开每个窗口的进程）、抓的时候缩放；"
        "找不到窗口不抓；桌面复制不缩放"] = (
        window.startswith("gfxcapture=hwnd=123:") and "window_exe" not in window and "width=1920:height=1080" in window
        and "resize_mode=scale_aspect" in window and no_window is None
        and desktop.startswith("ddagrab=") and "width" not in desktop)
    packets = [(0, 100.0), (48000, 101.0), (96000, 102.002)]  # 第三包比按采样数算的晚 2 毫秒（声卡时钟慢）
    out["对齐声音：用最近的一包算（声卡时钟和电脑时钟差开也对），画面比声音晚开始是正数、早开始是负数"] = (
        abs(recorder.audio_offset(packets, 48000, 100.5) - 0.5) < 1e-9
        and abs(recorder.audio_offset(packets, 48000, 102.5) - (2 + 0.498)) < 1e-6
        and abs(recorder.audio_offset(packets, 48000, 99.75) + 0.25) < 1e-9)
    src = tmp / "a.wav"
    tone_wav(src, 2.0, 1.0)
    recorder.align_wav(src, tmp / "cut.wav", 0.5)
    recorder.align_wav(src, tmp / "pad.wav", -0.25)

    def onset(path):
        with wave.open(str(path)) as w:
            data = w.readframes(w.getnframes())
            n = w.getnframes()
        first = next(i for i in range(0, len(data), 4) if data[i:i + 2] != b"\0\0") // 4
        return n, first

    (n_cut, beep_cut), (n_pad, beep_pad) = onset(tmp / "cut.wav"), onset(tmp / "pad.wav")
    out["挪 WAV：去掉开头 0.5 秒 / 前面补 0.25 秒静音，嘀声跟着挪"] = (
        n_cut == 72000 and abs(beep_cut - 24000) <= 1 and n_pad == 108000 and abs(beep_pad - 60000) <= 1)
    out["文件名：去掉 Windows 文件名里不能有的字符"] = recorder.safe_name('排位/恶灵: "R-301"') == "排位_恶灵_R-301"
    prog = tmp / "progress.txt"
    prog.write_text("frame=10\nfps=60\nprogress=continue\nframe=70\nfps=60\nprogress=continue\nframe=1", encoding="utf-8")
    out["读进度：只认最后一段完整的（正在写的那段不算）"] = recorder.read_progress(prog).get("frame") == "70"
    lib = tmp / "lib"
    for i, size in enumerate([300, 200, 100]):  # 最旧的最大
        month = lib / "2026-11"
        month.mkdir(parents=True, exist_ok=True)
        video = month / f"v{i}.mp4"
        video.write_bytes(b"\0" * size)
        video.with_suffix(".json").write_text("{}", encoding="utf-8")
        video.with_suffix(".jpg").write_bytes(b"jpg")  # 缩略图
        t = time.time() - 1000 + i * 10
        import os
        os.utime(video, (t, t))
    removed = recorder.enforce_limit(lib, 350, log=lambda *_: None)
    out["超过上限：从最旧的删起，MP4 和 .json、.jpg 一起删，删到不超过就停"] = (
        [p.name for p in removed] == ["v0.mp4"] and not (lib / "2026-11" / "v0.json").exists()
        and not (lib / "2026-11" / "v0.jpg").exists() and (lib / "2026-11" / "v1.mp4").exists()
        and (lib / "2026-11" / "v1.jpg").exists())
    return out


def probe_duration(ffmpeg, path):
    """MP4 里每一路（v / a）的 (开始, 时长)，用 ffmpeg 读（不另外要 ffprobe）。"""
    _, err = recorder.run_ffmpeg(ffmpeg, ["-i", str(path), "-f", "null", "-"])
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def frame_times(ffmpeg, path):
    _, err = recorder.run_ffmpeg(ffmpeg, ["-i", str(path), "-map", "0:v", "-vf", "showinfo", "-f", "null", "-"], timeout=120)
    return [float(x) for x in re.findall(r"pts_time:([\d.]+)", err or "")]


def beep_time(ffmpeg, path):
    """MP4 里声音第一次响的时刻（秒）。"""
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                       capture_output=True)
    data = r.stdout
    for i in range(0, len(data) - 1, 2):
        if abs(struct.unpack_from("<h", data, i)[0]) > 2000:
            return i / 2 / 48000
    return None


def record_checks(ffmpeg, tmp):
    out = {}
    rules = recorder.load_rules()
    quiet = []
    # 画质固定"很高"，不跟着默认变：测试画面只有 640×360，默认的"标准"码率约 0.6 Mbps，下面开录 1.5 秒就结束 ffmpeg 的
    # 那两项里 ffmpeg 还没把画面写进文件，第一段是空的（2026-11-20 默认改成标准时遇到）
    best = {"source": TEST_SOURCE, "quality": "best"}

    # 1. 录 3 秒，中间记两个事件
    rec = recorder.Recording(ffmpeg, recorder.Options(**best, audio=False), tmp / "lib", log=quiet.append,
                             rules=rules)
    rec.start()
    time.sleep(1.0)
    rec.mark("knocked")
    time.sleep(1.0)
    rec.mark("kill", kills=1)
    for _ in range(10):
        rec.poll()
        time.sleep(0.1)
    video = rec.finish({"mode": "排位", "legend": "恶灵"})
    meta = json.loads(video.with_suffix(".json").read_text(encoding="utf-8")) if video else {}
    duration = probe_duration(ffmpeg, video) if video else None
    out["录 3 秒拼成 MP4：在 录像文件夹\\年-月\\ 里，文件名带模式和传奇，分段文件删了"] = (
        video is not None and video.parent.parent == (tmp / "lib").resolve() and video.name.endswith("_排位_恶灵.mp4")
        and not any((tmp / "lib" / recorder.WORK_DIR_NAME).glob("*/*.ts")))
    out[f"MP4 约 3 秒（{duration}）、640×360"] = duration is not None and 2.5 < duration < 3.6 and \
        meta.get("size") == [640, 360]
    events = meta.get("events", [])
    out[f".json 里的事件：时刻是 MP4 里的位置（倒地约 1 秒、击杀约 2 秒处；{[e['t'] for e in events]}），带着附加的说明"] = (
        [e["kind"] for e in events] == ["knocked", "kill"] and events[1].get("kills") == 1
        and 0.6 < events[0]["t"] < 1.1 and 1.6 < events[1]["t"] < 2.1 and meta.get("outages") == [])

    # 2. 中途 ffmpeg 退出：重开，空档保留（断 11 秒：超过 ffmpeg 默认挤掉空档的 10 秒）
    long_rules = {**rules, "restart_wait_seconds": [11]}
    rec = recorder.Recording(ffmpeg, recorder.Options(**best, audio=False), tmp / "lib2", log=quiet.append,
                             rules=long_rules)
    rec.start()
    time.sleep(1.5)
    rec.proc.kill()
    end = time.monotonic() + 16
    while time.monotonic() < end and not rec.outages:
        rec.poll()
        time.sleep(0.2)
    rec.mark("after_gap")
    time.sleep(1.0)
    video2 = rec.finish()
    meta2 = json.loads(video2.with_suffix(".json").read_text(encoding="utf-8")) if video2 else {}
    gap = (meta2.get("outages") or [[0, 0]])[0]
    times = frame_times(ffmpeg, video2) if video2 else []
    jump = max((b - a for a, b in zip(times, times[1:])), default=0)
    out[f"ffmpeg 中途退出：自己重开，两个分段，断掉的那段记进 .json（{gap}）"] = (
        meta2.get("parts") == 2 and 10.5 < gap[1] - gap[0] < 13)
    out[f"MP4 里空档原样保留（画面里最大的间隔 {jump:.1f} 秒），空档后的事件位置对得上"] = (
        10.5 < jump < 13 and abs(meta2["events"][0]["t"] - gap[1]) < 1.0)

    # 3. 声音：假录音在第 2 秒嘀一声
    fake = FakeAudio(beep_at=2.0)
    rec = recorder.Recording(ffmpeg, recorder.Options(**best), tmp / "lib3", log=quiet.append, rules=rules,
                             audio_factory=fake)
    rec.start(apex_pid=1234)
    time.sleep(3.5)
    rec.poll()
    video3 = rec.finish()
    meta3 = json.loads(video3.with_suffix(".json").read_text(encoding="utf-8")) if video3 else {}
    beep = beep_time(ffmpeg, video3) if video3 else None
    # 嘀声在 MP4 里的位置 = 嘀声的时刻 - MP4 第一帧的时刻（都换成 QPC：第一帧 = t0 + first_frame）
    want = (fake.start_qpc + 2.0) - (rec.t0_qpc + meta3["first_frame"]) if "first_frame" in meta3 else None
    out[f"声音：合进 MP4，嘀声在 MP4 里的位置对得上画面（在 {beep} 秒，应在 {want and round(want, 3)} 秒）"] = (
        meta3.get("audio") is not None and beep is not None and want is not None and abs(beep - want) < 0.03)

    # 4. 上次崩了没拼的分段文件
    rec = recorder.Recording(ffmpeg, recorder.Options(**best, audio=False), tmp / "lib4", log=quiet.append,
                             rules=rules)
    rec.start()
    time.sleep(1.5)
    rec._kill()  # 像 Evo 崩了一样：不收尾
    done = recorder.recover_leftovers(ffmpeg, tmp / "lib4", log=quiet.append)
    out["上次崩了没拼的分段文件：拼成名字带'_未完成'的 MP4，分段文件删掉"] = (
        len(done) == 1 and done[0].name.endswith("_未完成.mp4") and done[0].stat().st_size > 0
        and not any((tmp / "lib4" / recorder.WORK_DIR_NAME).iterdir()))

    # 5. 设置里选了 AV1、这块显卡编不了（这里用一个不存在的编码器假装）：改用 H.264 照样录，不是整局不录
    no_av1 = {**rules, "encoders": {**rules["encoders"], "av1": ["av1_evo_test_missing"]}}
    said = []
    rec = recorder.Recording(ffmpeg, recorder.Options(**best, codec="av1", audio=False), tmp / "lib5",
                             log=said.append, rules=no_av1)
    rec.start()
    time.sleep(1.0)
    video5 = rec.finish()
    out[f"选了 AV1 但编不了：改用 H.264 录（{rec.encoder}），日志里写着"] = (
        video5 is not None and rec.encoder in rules["encoders"]["h264"] and rec.options.codec == "h264"
        and any("改用 h264" in line for line in said))
    return out


class FakeInMatch:
    """InMatchWatcher 里 match_recording 要看的那几项。"""

    def __init__(self):
        self.started, self.active, self.done, self.state, self.tops, self.legend = None, False, False, None, [], None
        self.hud = type("H", (), {"legends": type("L", (), {"name": staticmethod(lambda k: {7: "恶灵"}.get(k))})()})()

    @property
    def pending(self):
        return self.started is not None and not self.done


class FakeRecording:
    made = []

    def __init__(self, ffmpeg, options, out_dir, log=None, rules=None):
        self.options, self.out_dir, self.events, self.hud_changes, self.polls = options, out_dir, [], 0, 0
        self.finished = None
        self.fail = FakeRecording.fail
        FakeRecording.made.append(self)

    def start(self, pid):
        if self.fail:
            raise recorder.RecordingUnavailable(self.fail)
        self.pid = pid

    def mark(self, kind, **info):
        self.events.append({"kind": kind, **info})

    def hud_change(self):
        self.hud_changes += 1

    def poll(self):
        self.polls += 1

    def since_t0(self):
        return time.monotonic()

    def finish(self, info):
        self.finished = info
        return None


def match_checks(tmp):
    import match_recording
    out = {}
    clock = [0.0]
    settings = {}
    logs = []

    def make():
        FakeRecording.made, FakeRecording.fail = [], None
        return match_recording.MatchRecorder(logs.append, settings=lambda: dict(settings), data_dir=tmp,
                                             factory=FakeRecording, clock=lambda: clock[0], find_pid=lambda: 4321)

    def step(m, screen, im, seconds=0.3):
        clock[0] += seconds
        m.update(screen, im)

    # 开关关着：开局画面、自己的界面都不录
    m, im = make(), FakeInMatch()
    step(m, "match_start", im)
    im.started, im.active, im.state = 1, True, "存活"
    step(m, None, im)
    out["开关关着（默认）：一局开始、对局中都不录"] = FakeRecording.made == []

    # 开着：一整局
    settings.update({"recording_enabled": True, "recording_size": "1080p"})
    m, im = make(), FakeInMatch()
    step(m, "match_start", im)
    step(m, "match_start", im)  # 开局横幅还在
    im.started, im.active, im.state, im.legend = 1, True, "存活", 7
    step(m, None, im)
    rec = FakeRecording.made[0] if FakeRecording.made else None
    for state in ("倒地", "存活", "成盒", "存活"):
        im.state = state
        step(m, None, im)
    im.tops = ["a"]
    step(m, None, im)
    step(m, None, im)  # 右上角没变
    im.tops = ["a", "b"]
    step(m, None, im)
    step(m, "champion", im)
    step(m, "champion", im)
    im.done, im.active = True, False
    step(m, "summary", im)
    still = rec is not None and rec.finished is None
    step(m, "summary", im, seconds=3.5)  # 结束以后再录 3 秒
    m.close()
    out["开着：看到开局画面就开录（只开一次，横幅还在也不重开），设置里的分辨率带过去、录 Apex 的声音"] = (
        len(FakeRecording.made) == 1 and rec.options.size == "1080p" and rec.pid == 4321)
    out["事件：倒地、被扶起、成盒、被复活、冠军（冠军只记一次）"] = rec is not None and [e["kind"] for e in rec.events] == [
        "knocked", "revived", "dead", "respawned", "champion"]
    out["右上角变了才记时刻（没变不记）"] = rec is not None and rec.hud_changes == 2
    out["一局结束再录 3 秒才停，拼 MP4 时带上传奇（文件名用）；对局中按时看 ffmpeg"] = (
        still and rec.finished == {"legend": "恶灵"} and rec.polls >= 2)
    step(m, None, im)  # 同一局结束以后：不再开录
    out["同一局结束以后不重开"] = len(FakeRecording.made) == 1
    im2 = FakeInMatch()  # 下一局：开局画面（InMatchWatcher 已经清空）
    step(m, "match_start", im2)
    out["下一局开局：再开录"] = len(FakeRecording.made) == 2
    m.close()

    # 靶场：没有开局画面，看到自己的界面也录
    m, im = make(), FakeInMatch()
    im.started, im.active, im.state = 5, True, "存活"
    step(m, None, im)
    out["没有开局画面（靶场）：看到自己的界面就开录"] = len(FakeRecording.made) == 1
    m.close()

    # 击杀：kill_events（击杀数只会变多，读错的不标）、add_events（写进 .json，换成 MP4 里的位置）
    ke = match_recording.kill_events
    out["找击杀：击杀数变大才算，读不出的跳过，一下子多两个记两个"] = ke(
        [(1, 0), (2, None), (3, 1), (4, 1), (5, 3), (6, 3)]) == [(3, 1, 1), (5, 3, 2)]
    out["找击杀：读错的不标（紧接着下一块比它小、一下子多出太多）"] = ke(
        [(1, 0), (2, 7), (3, 1), (4, 1), (5, 40), (6, 2)], max_jump=5) == [(3, 1, 1), (6, 2, 1)]
    sidecar = tmp / "kills.json"
    sidecar.write_text(json.dumps({"first_frame": 0.5, "events": [{"t": 10.0, "kind": "knock"}]}), encoding="utf-8")
    ok = match_recording.add_events(tmp / "kills.mp4", [{"t": 12.5, "kind": "kill", "kills": 1, "count": 1},
                                                       {"t": 8.5, "kind": "kill", "kills": 2, "count": 1}])
    events = json.loads(sidecar.read_text(encoding="utf-8"))["events"]
    out["找击杀：写进录像旁边的 .json，时刻换成 MP4 里的位置（减掉第一帧），按时间排好"] = ok and [
        (e["kind"], e["t"]) for e in events] == [("kill", 8.0), ("knock", 10.0), ("kill", 12.0)]

    # 击杀：对局中击杀数那一格变了才存，回到大厅读，读完写进 .json
    m, im = make(), FakeInMatch()
    cells = {"a": np.zeros((28, 26), np.uint8), "b": np.full((28, 26), 255, np.uint8)}
    plan = []  # 每一帧：(击杀那一格, 读出来的击杀数)
    im.hud.kp_snip = lambda frame: plan[0]
    im.hud.kill_cell = lambda snip: None if snip[0] is None else cells[snip[0]]
    im.hud.read_kills = lambda snip: snip[1]
    im.hud.knock, im.hud.cfg = (lambda frame: None), {}
    step(m, "match_start", im)
    im.started, im.active, im.state = 8, True, "存活"
    rec = FakeRecording.made[-1]
    for n, (cell, kills) in enumerate([(None, 0), ("a", 0), ("a", 0), ("b", 1), ("b", 1), ("a", 2)]):
        plan[:] = [(cell, kills)]
        im.tops = list(im.tops) + [n]  # InMatchWatcher 存了新的一块右上角
        clock[0] += 0.3
        m.update(None, im, frame="截图")
    stored = len(m.kp_snips)
    video = tmp / "lib-kill" / "match.mp4"
    video.parent.mkdir(exist_ok=True)
    video.write_bytes(b"0")
    video.with_suffix(".json").write_text(json.dumps({"first_frame": 0.0, "events": []}), encoding="utf-8")
    rec.finish = lambda info: video
    im.done, im.active = True, False
    step(m, "summary", im)
    step(m, "summary", im, seconds=3.5)
    for w in m.workers:
        w.join(5)
    before = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))["events"]
    for _ in range(3):
        step(m, "lobby", im)
    kills = [e for e in json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))["events"] if e["kind"] == "kill"]
    out[f"击杀：对局中击杀数那一格变了才存（存了 {stored} 块），结算画面上不读，回到大厅读完写进 .json（{kills}）"] = (
        stored == 3 and before == [] and [(e["kills"], e["count"]) for e in kills] == [(1, 1), (2, 1)]
        and not m.pending and any("标上了 2 次击杀" in line for line in logs))
    m.close()

    # 击倒对手：中间那一行的"击倒"（hud.knock 给的位置）
    m, im = make(), FakeInMatch()
    script = []
    im.hud.knock = lambda frame: script.pop(0) if script else None
    im.hud.cfg = {"knock": {"rearm_seconds": 0.6, "moved": 20}}
    step(m, "match_start", im)
    im.started, im.active, im.state = 7, True, "存活"
    rec = FakeRecording.made[0]
    seen = [None, 299, 299, 300, 299, None, None, None, 299, 299, 352, 352, None]
    for x in seen:
        script.append(x)
        clock[0] += 0.3
        m.update(None, im, frame="一张截图")
    clock[0] += 0.3
    m.update("summary", im, frame="结算画面")  # 不在对局中的画面不看
    knocks = [e for e in rec.events if e["kind"] == "knock"]
    out[f"击倒对手：同一行显示着只算一次；隔一会儿又出现算下一次；还在显示但左右挪了（换了名字）也算（{len(knocks)} 次）"] = (
        len(knocks) == 3)
    m.close()

    # 录不了：一局最多试两次（横幅、看到自己的界面），日志里只说一次，下一局再试
    m = make()
    FakeRecording.fail = "这台电脑上没有能用的显卡硬件编码器"
    im = FakeInMatch()
    for _ in range(5):
        step(m, "match_start", im)  # 横幅在屏幕上好几秒
    im.started, im.active, im.state = 9, True, "存活"
    for _ in range(3):
        step(m, None, im)
    first_match = len(FakeRecording.made)
    im.done, im.active = True, False
    step(m, "lobby", im)
    for _ in range(3):
        step(m, "match_start", FakeInMatch())
    said = [line for line in logs if "录不了" in line]
    out[f"录不了：一局最多试两次（横幅一次、看到自己的界面一次，试了 {first_match} 次），日志里说一次原因，下一局开始时再试"] = (
        first_match == 2 and len(said) == 1 and len(FakeRecording.made) == 3)
    return out


WINDOW_CHILD = r"""
import tkinter as tk
root = tk.Tk()
root.title("Evo 录像测试窗口")
root.geometry("400x300+40+40")
root.configure(bg="#3060c0")
root.after(8000, root.destroy)
print("ready", flush=True)
root.mainloop()
"""


def knock_checks():
    """hud.knock：在中间那条横带里找"击倒"两个字（参照图贴到不同的位置都找得到），没有就是 None。"""
    import numpy as np
    import hud
    reader = hud.Hud(None)
    k = reader.cfg["knock"]
    tpl = reader.knock_template
    out = {}
    if tpl is None:
        return {"中间那一行的'击倒'：有参照图 config/templates/hud/knock.png": False}
    found = []
    for at in (300, 450):  # 名字长短不同，这一行左右挪
        frame = np.full((1080, 1920, 3), 30, np.uint8)
        x0, y0 = k["band"][0] + at, k["band"][1] + 31
        frame[y0:y0 + tpl.shape[0], x0:x0 + tpl.shape[1]] = tpl[:, :, None]
        found.append(reader.knock(frame))
    blank = reader.knock(np.full((1080, 1920, 3), 30, np.uint8))
    out[f"中间那一行的'击倒'：贴在横带里不同的位置都找得到（{found}），没有时是空"] = (
        found[0] is not None and abs(found[0] - 300) <= 2 and found[1] is not None and abs(found[1] - 450) <= 2
        and blank is None)
    return out


def window_checks(ffmpeg):
    """开一个测试窗口：按进程号找到它（不打开那个进程），只抓这个窗口抓得到。"""
    base = getattr(sys, "_base_executable", sys.executable)  # 虚拟环境的 python.exe 会再开一个真正的 Python：窗口在那个进程里
    child = subprocess.Popen([base, "-c", WINDOW_CHILD], stdout=subprocess.PIPE, text=True)
    try:
        child.stdout.readline()
        time.sleep(0.5)
        hwnd = recorder.find_window(child.pid)
        size, why = recorder.capture_size(ffmpeg, recorder.Options(source="window"), recorder.load_rules(), hwnd)
        nobody = recorder.find_window(4)  # System 进程：没有窗口
    finally:
        child.kill()
    return {f"找窗口：按进程号找到测试窗口（{hwnd}），只抓这个窗口抓得到（{size or why}）；没有窗口的进程返回空":
            bool(hwnd) and size is not None and size[0] >= 390 and nobody is None}


JOB_CHILD = r"""
import subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import process_info
p = subprocess.Popen([sys.argv[2], "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc2=r=5",
                      "-f", "null", "-"], stdin=subprocess.DEVNULL)
print(p.pid, process_info.kill_with_me(p), flush=True)
time.sleep(60)
"""


def job_checks(ffmpeg):
    child = subprocess.Popen([sys.executable, "-c", JOB_CHILD, str(Path(recorder.__file__).parent), ffmpeg],
                             stdout=subprocess.PIPE, text=True)
    pid, ok = child.stdout.readline().split()
    alive_before = int(pid) in process_info.snapshot()
    child.kill()
    child.wait()
    time.sleep(1.5)
    return {"作业对象：开 ffmpeg 的程序被强行结束，ffmpeg 也跟着退": ok == "True" and alive_before
            and int(pid) not in process_info.snapshot()}


def main():
    print("录像的录制核心")
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        checks = pure_checks(tmp)
        checks.update(match_checks(tmp))
        checks.update(knock_checks())
        ffmpeg = recorder.find_ffmpeg()
        if not ffmpeg:
            print("  （电脑上没有 ffmpeg：跳过真的录的那些）")
            return report(checks)
        checks.update(job_checks(ffmpeg))
        if not recorder.pick_encoder(ffmpeg, recorder.Options(source=TEST_SOURCE), recorder.load_rules()):
            print("  （这台电脑上没有能用的显卡硬件编码器：跳过真的录的那些）")
            return report(checks)
        checks.update(window_checks(ffmpeg))
        checks.update(record_checks(ffmpeg, tmp))
    return report(checks)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
