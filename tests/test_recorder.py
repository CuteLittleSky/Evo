"""录像的录制核心（code/recorder.py、process_audio.py、process_info.kill_with_me）。

不开游戏、不抓屏幕：画面用 ffmpeg 自带的测试画面（testsrc2，按真实时间出帧），声音用假的录音（按给定的时刻写 WAV）。
- 算码率、对齐声音（audio_offset、align_wav）、起文件名、读进度、抓屏滤镜、超过上限删最旧的：不用 ffmpeg
- 录 3 秒拼成 MP4：时长、大小、.json 里事件的时刻（MP4 里的位置）
- 中途 ffmpeg 退出：自己重开，断掉的那段记进 .json，MP4 里空档原样保留（断 11 秒也保留：ffmpeg 默认会把 10 秒以上的空档挤掉）
- 声音：假录音在第 2 秒"嘀"一声，MP4 里嘀声的位置对得上画面（30 毫秒以内）
- 上次崩了没拼的分段文件：拼成"_未完成"的 MP4
- 作业对象：开 ffmpeg 的程序被强行结束，ffmpeg 也跟着退
- 接进记录程序（match_recording.py）：开关默认关；一局开始开录、事件（倒地、被扶起、成盒、被复活、冠军）、右上角变了的时刻、
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
        round(recorder.bitrate(rules, 2560, 1440, 60, "h264", "high") / 1e6) == 22
        and round(recorder.bitrate(rules, 1920, 1080, 60, "h264", "high") / 1e6) == 12
        and recorder.bitrate(rules, 1920, 1080, 60, "av1", "high") == int(1920 * 1080 * 60 * 0.1 * 0.6))
    window = recorder.source_filter(recorder.Options(source="window", size="1080p"), rules, (1920, 1080))
    desktop = recorder.source_filter(recorder.Options(source="desktop"), rules)
    out["抓屏滤镜：只抓游戏窗口时按 r5apex 找窗口、抓的时候缩放；桌面复制不缩放"] = (
        window.startswith("gfxcapture=window_exe=r5apex") and "width=1920:height=1080" in window
        and "resize_mode=scale_aspect" in window and desktop.startswith("ddagrab=") and "width" not in desktop)
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
        t = time.time() - 1000 + i * 10
        import os
        os.utime(video, (t, t))
    removed = recorder.enforce_limit(lib, 350, log=lambda *_: None)
    out["超过上限：从最旧的删起，MP4 和 .json 一起删，删到不超过就停"] = (
        [p.name for p in removed] == ["v0.mp4"] and not (lib / "2026-11" / "v0.json").exists()
        and (lib / "2026-11" / "v1.mp4").exists())
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

    # 1. 录 3 秒，中间记两个事件
    rec = recorder.Recording(ffmpeg, recorder.Options(source=TEST_SOURCE, audio=False), tmp / "lib", log=quiet.append,
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
    rec = recorder.Recording(ffmpeg, recorder.Options(source=TEST_SOURCE, audio=False), tmp / "lib2", log=quiet.append,
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
    rec = recorder.Recording(ffmpeg, recorder.Options(source=TEST_SOURCE), tmp / "lib3", log=quiet.append, rules=rules,
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
    rec = recorder.Recording(ffmpeg, recorder.Options(source=TEST_SOURCE, audio=False), tmp / "lib4", log=quiet.append,
                             rules=rules)
    rec.start()
    time.sleep(1.5)
    rec._kill()  # 像 Evo 崩了一样：不收尾
    done = recorder.recover_leftovers(ffmpeg, tmp / "lib4", log=quiet.append)
    out["上次崩了没拼的分段文件：拼成名字带'_未完成'的 MP4，分段文件删掉"] = (
        len(done) == 1 and done[0].name.endswith("_未完成.mp4") and done[0].stat().st_size > 0
        and not any((tmp / "lib4" / recorder.WORK_DIR_NAME).iterdir()))
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
        ffmpeg = recorder.find_ffmpeg()
        if not ffmpeg:
            print("  （电脑上没有 ffmpeg：跳过真的录的那些）")
            return report(checks)
        checks.update(job_checks(ffmpeg))
        if not recorder.pick_encoder(ffmpeg, recorder.Options(source=TEST_SOURCE), recorder.load_rules()):
            print("  （这台电脑上没有能用的显卡硬件编码器：跳过真的录的那些）")
            return report(checks)
        checks.update(record_checks(ffmpeg, tmp))
    return report(checks)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
