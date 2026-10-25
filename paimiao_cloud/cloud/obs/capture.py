"""记录子进程里的截屏接管：把 dxcam 的相机换成「OBS 投影那块屏」。

上游 `live_collect.collect()` 里只有两句话碰截屏：
    camera = dxcam.create(output_color="BGR", processor_backend="numpy")   # live_collect.py:373
    frame = camera.grab(new_frame_only=False)                              # live_collect.py:414
外加日志里用的 `camera.width` / `camera.height`。

所以我们不去改这两行，而是**包住 dxcam.create**：在记录子进程里调用它时，
返回一个自己的相机对象，接口和 dxcam 的相机一样（width/height/grab/release/start/stop），
内部按当前探测结果去截 OBS 投影那块屏；探测不到投影时按设置决定「返回空帧」还是「退回整屏」。

为什么把这段逻辑放在子进程里、而不是主进程：
    主进程只管托盘和面板，探测窗口要用到 user32；子进程里做，主进程的开销不变，
    而且 `camera` 本来就是子进程建的。两者读的是同一份 app_settings，
    用户在设置页改了来源，子进程几秒内就能看到（每 LOOK_EVERY 秒重看一眼设置和窗口）。

为什么要缓存 camera 对象：dxcam 每次 create 都要初始化 DXGI，真有 0.3～1 秒的代价。
    投影窗口位置变了（用户换了块屏）才重建；只是暂时没找到投影就返回空帧，不重建。
"""
from __future__ import annotations

import time

from cloud.obs import projector
from cloud.obs import settings as obs_settings

# 多久重新看一眼「设置 + 投影窗口」。5 秒足够跟手，又不会每个 0.3 秒的帧都去枚举窗口。
LOOK_EVERY = 5.0


class ProjectorCamera:
    """伪装成 dxcam 的相机。接口只有一个子集，够 live_collect 用的那几处。

    grab() 返回 BGR 的 numpy 数组（H, W, 3），和 dxcam 的 grab 一致；
    拿不到画面时返回 None —— 上游的循环本来就写了 `if frame is not None:`，
    所以返回 None 等于「这一帧跳过」，不会污染识别。
    """

    def __init__(self, dxcam_module, real_create, log=print):
        """dxcam_module 只用它的类型/常量；真正建相机一律走 real_create。

        为什么要单独传 real_create 进来：install() 会把模块上的 dxcam.create 换成它自己，
        如果这里再调 self._dxcam.create(...)，调到的就是那个包装函数本身 ——
        无限递归（2026-10-23 实测踩过：RecursionError: maximum recursion depth exceeded）。
        """
        self._dxcam = dxcam_module
        self._real_create = real_create
        self._log = log
        self._paimiao_obs_camera = True   # 标记：projector 探测时看到它就绝不 release（会作废这台）
        self._camera = None          # 真正的 dxcam 相机（按 output_idx 建）
        self._output_idx = None      # 当前相机对应第几号显示器
        self._region = None          # 当前相机对应的矩形（相对那块屏）
        self._next_look = 0.0        # 下次探测的时间点
        self._state = None           # "projector" / "fallback" / "off"
        self._warned = set()         # 同一句提示只说一次，别刷屏

    # ---------- 上游会用到的属性 ----------

    @property
    def width(self):
        cam = self._camera
        return getattr(cam, "width", 0) if cam is not None else 0

    @property
    def height(self):
        cam = self._camera
        return getattr(cam, "height", 0) if cam is not None else 0

    # ---------- 主循环 ----------

    def grab(self, *args, **kwargs):
        now = time.monotonic()
        # 相机被放掉（或探测时被顺手 release）过：立刻重建，不等 LOOK_EVERY。
        if self._camera is not None and not self._camera_alive():
            self._once("revive", "[OBS 投影] 截屏相机已被释放，重新建一台")
            self._drop_camera()
        if now >= self._next_look or self._camera is None:
            self._next_look = now + LOOK_EVERY
            self._refresh()
        if self._camera is None:
            return None
        try:
            return self._camera.grab(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001  换分辨率/显示器休眠时会抛，跳过这一帧
            # 相机作废（被 release 过）不是偶发出错，丢掉它，下一帧重建
            if "released" in str(exc):
                self._drop_camera()
            self._once("grab_error", f"[OBS 投影] 截屏出错，跳过这一帧：{exc}")
            return None

    def _refresh(self):
        """重新看一眼设置和窗口，必要时重建 dxcam 相机。"""
        want_projector = obs_settings.enabled()
        if not want_projector:
            # 用户在设置页切回了「整屏」：回到上游的行为（output_idx 默认 = 主屏）
            if self._state == "projector":
                self._log("[OBS 投影] 已切回整屏截取")
            self._state = "off"
            self._rebuild_if_needed(output_idx=None, region=None)
            return

        try:
            found = projector.find_projector()
        except Exception as exc:  # noqa: BLE001
            self._once("probe_error", f"[OBS 投影] 探测投影窗口失败：{exc}")
            found = None
        # find_projector() 量显示器分辨率时要临时建相机、看完就 release。
        # 它取的是 dxcam.create —— 已经被换成「返回本类实例」了，所以它拿到的
        # 就是我们手上这台，release 之后这台就永久作废。
        # 下面这行是「万一还是被放掉了」的兜底：立刻丢掉，下面会重建。
        if self._camera is not None and not self._camera_alive():
            self._drop_camera()

        if found is not None:
            if self._state != "projector":
                self._log(f"[OBS 投影] 已找到投影窗口「{found['title']}」，"
                          f"截 {found['width']}x{found['height']} 那块屏")
                if not found.get("output_exact"):
                    # 分辨率没对上：可能两块屏分辨率一样看不出来，也可能 dxcam 那边认错了屏
                    self._log("[OBS 投影] 提示：没能按分辨率确认是哪块屏，如果记错了画面，"
                              "请在设置页看看是不是选错了屏")
            self._state = "projector"
            # 注意用 output_index（dxcam 的序号），不是 monitor_index（第几块屏）——
            # 这两个数不一定相等，2026-10-23 实测就差了 1
            self._rebuild_if_needed(output_idx=found["output_index"], region=found["region"])
            return

        # 没找到投影
        if self._state != "fallback":
            why = "投影窗口不见了（OBS 关了、投影关了，或者取消了「始终置顶」）"
            if obs_settings.pause_when_closed():
                self._log(f"[OBS 投影] {why}，暂停截屏直到它回来（设置页可以改成退回整屏）")
            else:
                self._log(f"[OBS 投影] {why}，按设置退回整屏截取")
        self._state = "fallback"
        if obs_settings.pause_when_closed():
            # 暂停：把相机放下，返回空帧
            self._drop_camera()
        else:
            self._rebuild_if_needed(output_idx=None, region=None)

    def _rebuild_if_needed(self, output_idx, region):
        if self._camera is not None and (output_idx, region) == (self._output_idx, self._region):
            return
        self._drop_camera()
        try:
            if output_idx is None:
                cam = self._real_create(output_color="BGR", processor_backend="numpy")
            else:
                cam = self._real_create(output_idx=output_idx, output_color="BGR",
                                        processor_backend="numpy")
        except Exception as exc:  # noqa: BLE001
            self._once("create_error", f"[OBS 投影] 建不了截屏相机（output_idx={output_idx}）：{exc}")
            return
        self._camera, self._output_idx, self._region = cam, output_idx, region
        if output_idx is not None:
            self._log(f"[OBS 投影] 截屏相机已就绪：{getattr(cam, 'width', '?')}"
                      f"x{getattr(cam, 'height', '?')}")

    def _camera_alive(self):
        """手上这台 dxcam 相机还能不能用。

        dxcam 的 release() 是单向的：放掉以后这台相机永久作废，再 grab 就抛
        「DXCamera has been released and cannot be reused」。
        2026-10-23 实测踩过：projector.find_projector() 量显示器分辨率时会建相机再
        release；如果它拿到的是我们这台（被 release 过就是死的），下一帧就永远出错，
        表现是「进了大厅读了段位，然后一整天不记录」。
        """
        cam = self._camera
        if cam is None:
            return False
        if getattr(cam, "_is_released", False):     # dxcam 自己的作废标记
            return False
        return getattr(cam, "width", 0) and getattr(cam, "height", 0)

    def _drop_camera(self):
        cam, self._camera, self._output_idx, self._region = self._camera, None, None, None
        if cam is not None:
            try:
                cam.release()
            except Exception:
                pass

    def release(self):
        self._drop_camera()
        # 放掉以后还要能用（上游只在 finally 里调一次，但设置页切来源、探测都会再来要画面），
        # 所以把「下次探测」提前，下一帧 grab 会自己重建相机。
        self._next_look = 0.0
        self._state = None

    def start(self, *args, **kwargs):
        return None

    def stop(self):
        return None

    # ---------- 日志 ----------

    def _once(self, key, message):
        """同一类提示只说一次，避免每 0.3 秒刷一行。"""
        if key in self._warned:
            return
        self._warned.add(key)
        self._log(message)


_patched = False


def install(dxcam_module, log=print):
    """把 dxcam.create 换成「记录子进程里返回我们的相机」。

    幂等：包过的函数带标记，重复调用直接返回。
    只在记录子进程里调用（见 cloud/capture.py 的 install_for_child）。
    """
    global _patched
    if _patched or getattr(dxcam_module.create, "_paimiao_obs", False):
        return False
    real_create = dxcam_module.create

    def create(*args, **kwargs):
        # 只在「用户选了投影来源」时接管；否则原样调用上游自己的 create，
        # 这样关掉功能以后行为（含画面比例、多显示器默认值）和上游一字不差。
        try:
            if not obs_settings.enabled():
                return real_create(*args, **kwargs)
        except Exception:
            return real_create(*args, **kwargs)
        return ProjectorCamera(dxcam_module, real_create, log=log)

    create._paimiao_obs = True   # type: ignore[attr-defined]
    create._paimiao_real = real_create  # type: ignore[attr-defined]
    dxcam_module.create = create
    _patched = True
    return True
