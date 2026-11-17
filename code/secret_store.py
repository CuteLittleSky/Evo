"""在这台电脑上加密保存一小段东西（现在只有 B站的登录信息）：用 Windows 自带的数据保护（DPAPI），不另加依赖。

加密用的钥匙由 Windows 按"这台电脑上的这个 Windows 用户"保管：别的用户、别的电脑解不开，文件被拷走也没用
（2026-11-17 用户定的：登录信息这样存）。换电脑、重装系统以后解不开，当作没有，重新登录就行。
存的是一个 JSON 能表示的东西（字典）；文件里是加密后的字节，不是文字。

    save(path, {"cookie": "…"})   存
    load(path)                    读；没有这个文件、解不开、坏了都返回 None
    forget(path)                  删掉
"""

import ctypes
import json
from ctypes import wintypes
from pathlib import Path

ENTROPY = b"Evo Tracker"       # 加密时掺进去的一段固定的字：别的程序直接拿 Windows 的解密功能解不开（要知道这段字）
UI_FORBIDDEN = 0x1             # 不许弹出任何窗口


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ARGS = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
         ctypes.POINTER(_Blob)]
_crypt32.CryptProtectData.argtypes = _ARGS
_crypt32.CryptUnprotectData.argtypes = _ARGS
_crypt32.CryptProtectData.restype = _crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer  # buffer 要留着，不然内存被收走


def _call(function, data):
    source, keep = _blob(data)
    entropy, keep_too = _blob(ENTROPY)
    out = _Blob()
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "Windows 的数据保护没有成功")
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        _kernel32.LocalFree(out.data)
        del keep, keep_too


def protect(data):
    """加密一段字节。"""
    return _call(_crypt32.CryptProtectData, bytes(data))


def unprotect(data):
    """解密；不是这台电脑上这个用户加密的、被改过的，抛 OSError。"""
    return _call(_crypt32.CryptUnprotectData, bytes(data))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")  # 先写临时文件再换过去：写到一半断电也不会留下半个文件
    temp.write_bytes(protect(json.dumps(value, ensure_ascii=False).encode("utf-8")))
    temp.replace(path)


def load(path):
    try:
        return json.loads(unprotect(Path(path).read_bytes()).decode("utf-8"))
    except (OSError, ValueError):
        return None


def forget(path):
    try:
        Path(path).unlink()
    except OSError:
        pass
