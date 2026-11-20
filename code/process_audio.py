"""只录一个程序（连同它的子进程）发出的声音：Windows 的"按进程录音"（录像用，见 recorder.py、docs/录像.md）。

用的是 Windows 10 2004 / Windows 11 起有的 ActivateAudioInterfaceAsync + VAD\\Process_Loopback（和 OBS 的
"应用程序音频捕获"同一个接口）。只给 Windows 一个进程号，不打开、不碰那个进程（原则 1）。2026-11-19 实测：
EA 反作弊在运行时也录得到 Apex 的声音。

每一包声音带着 Windows 给的时刻（QPC，和 time.perf_counter 同一个钟），写进 WAV 的同时记下"第几个采样 ↔ 什么时刻"，
之后对齐画面用。程序没出声的那几段 Windows 可能不给数据：按时刻补上静音，WAV 的长度始终和真实时间对得上。
录音在自己的线程里（COM 用多线程模式初始化），不管主线程有没有先 import 过 comtypes（dxcam 会）都能用。

单独试：python code/process_audio.py <进程号> <秒数> <输出.wav>
"""

import ctypes
import sys
import threading
import time
import wave
from ctypes import POINTER, Structure, byref, c_int, c_longlong, c_uint32, c_uint64, c_ubyte, c_ulong, c_ushort, c_void_p
from ctypes import wintypes

sys.coinit_flags = 0  # 主线程也用多线程模式（只在还没 import 过 comtypes 时有用；录音线程自己会再初始化一次）
import comtypes  # noqa: E402
from comtypes import COMMETHOD, COMObject, GUID, HRESULT, IUnknown  # noqa: E402

RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2  # 16 位
FRAME_BYTES = CHANNELS * SAMPLE_BYTES

VT_BLOB = 65
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_PCM = 1
GAP_SECONDS = 0.02  # 两包之间比该有的多出这么久，就补静音（小于它的是正常的抖动）


class WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD), ("ProcessLoopbackMode", c_int)]


class ACTIVATION_PARAMS(Structure):
    _fields_ = [("ActivationType", c_int), ("ProcessLoopbackParams", PROCESS_LOOPBACK_PARAMS)]


class BLOB(Structure):
    _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]


class PROPVARIANT(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("blob", BLOB)]


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                  (["out"], POINTER(c_uint32), "pNumFramesToRead"), (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
                  (["out"], POINTER(c_uint64), "pu64DevicePosition"), (["out"], POINTER(c_uint64), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
    ]


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize", (["in"], c_int, "ShareMode"), (["in"], wintypes.DWORD, "StreamFlags"),
                  (["in"], c_longlong, "hnsBufferDuration"), (["in"], c_longlong, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"), (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "phnsLatency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_int, "ShareMode"), (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(c_longlong), "phnsDefaultDevicePeriod"),
                  (["out"], POINTER(c_longlong), "phnsMinimumDevicePeriod")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "eventHandle")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "riid"), (["out"], POINTER(POINTER(IUnknown)), "ppv")),
    ]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = [COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(c_int), "activateResult"),
                           (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"))]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = [COMMETHOD([], HRESULT, "ActivateCompleted",
                           (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation"))]


class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = []


class _Done(COMObject):
    """Windows 准备好录音接口以后回调这里（在它自己的线程里）。"""
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.event = threading.Event()
        self.client = None
        self.error = None

    def ActivateCompleted(self, activateOperation):
        try:
            hr, unknown = activateOperation.GetActivateResult()
            if hr < 0:
                self.error = f"HRESULT 0x{hr & 0xFFFFFFFF:08X}"
            else:
                self.client = unknown.QueryInterface(IAudioClient)
        except Exception as e:  # noqa: BLE001 回调里的错误带回主线程再报
            self.error = repr(e)
        self.event.set()
        return 0


_mmdevapi = ctypes.WinDLL("Mmdevapi.dll")
_activate = _mmdevapi.ActivateAudioInterfaceAsync
_activate.argtypes = [wintypes.LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT),
                      POINTER(IActivateAudioInterfaceCompletionHandler), POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
_activate.restype = HRESULT
_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def qpc_seconds():
    """现在的 QPC 时刻（秒）：和 GetBuffer 给的时刻、time.perf_counter 是同一个钟。"""
    return time.perf_counter()


def open_client(pid):
    """建一个只录 pid（连同子进程）声音的录音接口，返回 (IAudioClient, IAudioCaptureClient, 事件)。失败抛 RuntimeError。"""
    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    params = ACTIVATION_PARAMS(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
                               PROCESS_LOOPBACK_PARAMS(pid, PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE))
    prop = PROPVARIANT(vt=VT_BLOB)
    prop.blob.cbSize = ctypes.sizeof(params)
    prop.blob.pBlobData = ctypes.addressof(params)
    done = _Done()
    handler = done.QueryInterface(IActivateAudioInterfaceCompletionHandler)
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    _activate("VAD\\Process_Loopback", byref(IAudioClient._iid_), byref(prop), handler, byref(op))
    if not done.event.wait(10):
        raise RuntimeError("Windows 10 秒内没准备好录音接口")
    if done.client is None:
        raise RuntimeError(f"按进程录音用不了：{done.error}")
    fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, CHANNELS, RATE, RATE * FRAME_BYTES, FRAME_BYTES, SAMPLE_BYTES * 8, 0)
    client = done.client
    flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
             | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
    client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, 2_000_000, 0, byref(fmt), None)  # 缓冲 200 毫秒
    event = _kernel32.CreateEventW(None, False, False, None)
    client.SetEventHandle(event)
    capture = client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
    return client, capture, event


class ProcessAudio(threading.Thread):
    """在自己的线程里录 pid 的声音到 wav_path。stop() 停下。

    写完以后：first_time = WAV 第一个采样的时刻（QPC 秒，None = 一包都没收到）；packets = [(第几个采样, QPC 秒)]；
    gaps = 补了静音的次数；error = 出错的原因（None = 正常）。"""

    def __init__(self, pid, wav_path):
        super().__init__(daemon=True)
        self.pid, self.wav_path = pid, wav_path
        self.first_time = None
        self.packets = []
        self.gaps = 0
        self.error = None
        self.ready = threading.Event()  # 录音接口建好了（或建不起来，看 error）
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()
        self.join(5)

    def run(self):
        try:
            client, capture, event = open_client(self.pid)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.ready.set()
            return
        written = 0
        with wave.open(str(self.wav_path), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_BYTES)
            wav.setframerate(RATE)
            client.Start()
            self.ready.set()
            try:
                while not self._stop.is_set():
                    _kernel32.WaitForSingleObject(event, 100)
                    while capture.GetNextPacketSize():
                        data, frames, flags, _, qpc100ns = capture.GetBuffer()
                        when = qpc100ns / 1e7
                        if self.first_time is None:
                            self.first_time = when
                        expected = self.first_time + written / RATE
                        if when - expected > GAP_SECONDS:  # 中间有一段没给数据（程序没出声）：补静音
                            pad = round((when - expected) * RATE)
                            wav.writeframesraw(bytes(pad * FRAME_BYTES))
                            written += pad
                            self.gaps += 1
                        if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                            chunk = bytes(frames * FRAME_BYTES)
                        else:
                            chunk = ctypes.string_at(data, frames * FRAME_BYTES)
                        self.packets.append((written, when))
                        wav.writeframesraw(chunk)
                        written += frames
                        capture.ReleaseBuffer(frames)
            except Exception as e:  # noqa: BLE001
                self.error = repr(e)
            finally:
                client.Stop()
                _kernel32.CloseHandle(event)


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    pid, seconds, path = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
    rec = ProcessAudio(pid, path)
    rec.start()
    rec.ready.wait(15)
    if rec.error:
        print("录不了：", rec.error)
        return 1
    print(f"在录进程 {pid} 的声音，{seconds:.0f} 秒……")
    time.sleep(seconds)
    rec.stop()
    print(f"写好了 {path}：{len(rec.packets)} 包，补静音 {rec.gaps} 次，错误：{rec.error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
