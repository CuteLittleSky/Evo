"""采集卡 / 虚拟摄像头相关（纯注入层，和上游 code/capture.py 无关）。

    devices  枚举 Windows DirectShow 的视频设备（只读，不打开）
    frame    用 OpenCV(CAP_DSHOW) 从 OBS 虚拟摄像头取帧，伪装成 dxcam 的相机

上游 `capture.open_camera()` 在记录子进程里由 `cloud/obs/capture.py` 接管，见那边说明。
"""
