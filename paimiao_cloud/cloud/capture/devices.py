"""Evo 派喵云同步 · 采集卡：列出这台电脑上的视频采集设备（Windows DirectShow）。

为什么用 ctypes 直接调 ole32 / quartz，而不是装 pygrabber 之类的库：
    1. 零新依赖（AGENT_RULES 要求新增依赖先说清理由，能不加就不加）；
    2. 返回值 `(友好名, 设备路径)` 的**顺序就是 OpenCV(DSHOW) 的索引顺序**
       —— 实测：两者都来自 combase 的 CreateClassEnumerator(CLSID_VideoInputDeviceCategory)；
    3. 主程序要先知道"摄像头有几个、采集卡叫什么"，再决定记录子进程该开第几号设备。

**只管枚举，不打开设备**：真正的取帧走 OpenCV(DirectShow)，见 `frame.py`。
打开设备会触发一次初始化（真机上是 0.5～1 秒），枚举不会。

踩过的坑（2026-10-23 实测）：
- `ole32.CoCreateInstance` 的 pUnkOuter 参数类型必须写 c_void_p，写成 BOOL 会被
  按 32 位截断，VARIANT 起始的 vt 字段等于被写坏；
- VARIANT 取值用 VariantChangeType 转成 BSTR 再读，比手算 LPWSTR 偏移省事；
- 枚举器 `Next(1)` 返回 S_FALSE(1) 时到结尾，**不能**当错误处理。
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_void_p, wintypes

HRESULT = wintypes.LONG

CLSID_SystemDeviceEnum = "{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}"
IID_ICreateDevEnum = "{29840822-5B84-11D0-BD3B-00A0C911CE86}"
CLSID_VideoInputDeviceCategory = "{860BB310-5D01-11D0-BD3B-00A0C911CE86}"
IID_IEnumMoniker = "{00000102-0000-0000-C000-000000000046}"
IID_IPropertyBag = "{55272A00-42CB-11CE-8135-00AA004BB851}"

CLSCTX_INPROC_SERVER = 0x1
VT_BSTR = 8
VT_EMPTY = 0

# 友好名属性：{FriendlyName GUID, PID=0}
_FRIENDLY = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
_FRIENDLY_PID = 14
_DEVICE_PATH = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
_DEVICE_PATH_PID = 15

_ole32 = ctypes.WinDLL("ole32")
_oleaut32 = ctypes.WinDLL("oleaut32")   # SysAllocString / SysFreeString 在 oleaut32，不在 ole32


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


# 这几个函数的参数类型必须写死：CoCreateInstance 的 pUnkOuter 写成别的类型会被截断，
# VARIANT 的起始字段（vt）就会被写坏，后面读什么都错（2026-10-23 实测）。
_ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, wintypes.DWORD, POINTER(GUID), POINTER(c_void_p)]
_ole32.CoCreateInstance.restype = HRESULT
_ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, POINTER(GUID)]
_ole32.CLSIDFromString.restype = HRESULT
_ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = HRESULT
_oleaut32.SysAllocString.argtypes = [wintypes.LPCWSTR]
_oleaut32.SysAllocString.restype = c_void_p
_oleaut32.SysFreeString.argtypes = [c_void_p]
_oleaut32.SysFreeString.restype = None


# 这几个函数的参数类型必须写死：CoCreateInstance 的 pUnkOuter 写成别的类型会被截断，
# VARIANT 的起始字段（vt）就会被写坏，后面读什么都错（2026-10-23 实测）。
_ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, wintypes.DWORD, POINTER(GUID), POINTER(c_void_p)]
_ole32.CoCreateInstance.restype = HRESULT
_ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, POINTER(GUID)]
_ole32.CLSIDFromString.restype = HRESULT
_ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = HRESULT
_oleaut32.SysAllocString.argtypes = [wintypes.LPCWSTR]
_oleaut32.SysAllocString.restype = c_void_p
_oleaut32.SysFreeString.argtypes = [c_void_p]
_oleaut32.SysFreeString.restype = None


class _BStrBlob(ctypes.Structure):
    """BSTR 在 VARIANT 里的布局：首字是长度（字节数），后面紧跟 UTF-16 字符。"""

    _fields_ = [("len", wintypes.DWORD), ("first", wintypes.WCHAR)]


class _VARIANT(ctypes.Structure):
    """VARIANT 只用得到 vt（类型）和 BSTR 那一格。

    64 位下 BSTR 在偏移 16（vt 2 字节 + 3 个保留字 + 8 字节的指针格），
    所以这里留两份空白再放指针，读数时用 `value` 取 BSTR。
    """

    _fields_ = [("vt", wintypes.WORD), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
                ("r3", wintypes.WORD), ("pad", c_void_p), ("value", c_void_p)]


def _release(ptr) -> None:
    try:
        _call(ptr, 2, wintypes.ULONG)(ptr)
    except Exception:
        pass


def guid(text: str) -> GUID:
    out = GUID()
    _ole32.CLSIDFromString(ctypes.c_wchar_p("{%s}" % text.strip("{}")), byref(out))
    return out


def _hr_ok(hr: int) -> bool:
    return (hr & 0xFFFFFFFF) < 0x80000000


def _call(ptr, index: int, restype, *argtypes):
    """COM 虚表调用：接口指针 -> 第 index 个方法。"""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])


def _release(ptr) -> None:
    try:
        _call(ptr, 2, wintypes.ULONG)(ptr)
    except Exception:
        pass


def _property(prop_bag, prop_id: int, value: int = 0) -> str:
    """读 IPropertyBag.Read(BSTR 键名，VARIANT*，LPOLESTR*)。

    键名是**属性 ID 的字符串**（"14" = 友好名），不是 GUID。
    VARIANT 不能写成 Buffer 传：那样 ctypes 会把它当数组再包一层，
    lParam 指向的就不是 VARIANT 本身了（实测读出来永远是空）。
    """
    key = _oleaut32.SysAllocString(ctypes.c_wchar_p(str(prop_id)))
    variant = _VARIANT()
    hr = _call(prop_bag, 3, HRESULT,
               c_void_p, c_void_p, c_void_p)(prop_bag, key, byref(variant), None)
    _oleaut32.SysFreeString(key)
    if not _hr_ok(hr) or variant.vt != VT_BSTR:
        return ""
    bstr = variant.value
    if not bstr:
        return ""
    chars = ctypes.cast(bstr, POINTER(_BStrBlob)).contents
    # BSTR 的 len 是**字节数**；离奇地大就退化为按 NUL 结尾读
    if 0 < chars.len < 8192:
        text = ctypes.wstring_at(bstr, chars.len // 2)
    else:
        text = ctypes.wstring_at(bstr)
    _oleaut32.SysFreeString(bstr)
    return text.strip("\x00 ")


def list_devices() -> list[dict]:
    """返回 [{"name": 友好名, "path": 设备路径, "index": DirectShow 序号}, ...]。

    顺序 = OpenCV(DSHOW) 的摄像头序号。读不到就返回空列表（调用方要能接受"没有设备"）。
    """
    # COM 要先初始化这个线程（0x800401F0 = CO_E_NOTINITIALIZED 就是没初始化）。
    # 线程里已经初始化过会返回 S_FALSE，那也是成功，不用补偿。
    co_hr = _ole32.CoInitializeEx(None, 0x2)      # COINIT_APARTMENTTHREADED
    if co_hr in (0x80010106,):                     # RPC_E_CHANGED_MODE：别的线程先设成了 MTA，照样能用
        co_hr = 0
    if co_hr not in (0, 1):
        raise OSError(f"CoInitializeEx 失败: 0x{co_hr & 0xFFFFFFFF:08X}")

    out: list[dict] = []
    enum_ptr = c_void_p()
    clsid, iid = guid(CLSID_SystemDeviceEnum), guid(IID_ICreateDevEnum)
    hr = _ole32.CoCreateInstance(
        byref(clsid), None, CLSCTX_INPROC_SERVER, byref(iid), byref(enum_ptr))
    if not _hr_ok(hr) or not enum_ptr:
        raise OSError(f"CoCreateInstance(SystemDeviceEnum) 失败: 0x{hr & 0xFFFFFFFF:08X}")

    dev_enum = c_void_p()
    try:
        category = guid(CLSID_VideoInputDeviceCategory)
        # ICreateDevEnum::CreateClassEnumerator(REFCLSID, IEnumMoniker**, DWORD)
        hr = _call(enum_ptr, 3, HRESULT, POINTER(GUID), POINTER(c_void_p), wintypes.DWORD)(
            enum_ptr, byref(category), byref(dev_enum), 0)
        if hr == 0x80070490:        # 这一类设备一个都没有
            return out
        if not _hr_ok(hr):
            raise OSError(f"CreateClassEnumerator 失败: 0x{hr & 0xFFFFFFFF:08X}")
        if not dev_enum:
            return out

        index = 0
        while True:
            moniker = c_void_p()
            fetched = wintypes.ULONG()
            # IEnumMoniker::Next(celt, IMoniker**, ULONG*)：S_FALSE 表示到结尾了
            hr = _call(dev_enum, 3, HRESULT, wintypes.ULONG, POINTER(c_void_p), POINTER(wintypes.ULONG))(
                dev_enum, 1, byref(moniker), byref(fetched))
            if not _hr_ok(hr) or not fetched.value or not moniker:
                break
            name = path = ""
            bag = c_void_p()
            try:
                iid_prop = guid(IID_IPropertyBag)
                # IMoniker::BindToStorage(pbc, pmkToLeft, riid, void**)
                hr = _call(moniker, 9, HRESULT, c_void_p, c_void_p, POINTER(GUID), POINTER(c_void_p))(
                    moniker, None, None, byref(iid_prop), byref(bag))
                if _hr_ok(hr) and bag:
                    name = _property(bag, _FRIENDLY_PID)
                    path = _property(bag, _DEVICE_PATH_PID)
            finally:
                if bag:
                    _release(bag)
                _release(moniker)
            out.append({"index": index, "name": name or f"设备 {index}", "path": path})
            index += 1
    finally:
        if dev_enum:
            _release(dev_enum)
        _release(enum_ptr)
    return out
