"""OBS 画面来源 · 设置页那一栏（纯注入，上游 settings.html 一个字节都不动）。

三种来源：整屏（上游默认）/ OBS 投影 / OBS 虚拟摄像头（2026-11-07 加）。
做法和 `cloud/notice.py` 一样：在 `app.jinja_env` 上再叠一层 preprocess 钩子，
对 settings.html 的**源码字符串**插一段 `<section>`，渲染期数据由
`@bp.app_context_processor` 提供（和导航第 4 项同一套办法）。

插入位置：`{# 提示音（sounds.py` 这个注释之前（见 ANCHOR），这一条不变。

为什么「能不能用」必须放在渲染期判断（不能放进 preprocess）：
    Jinja 会把编译结果缓存起来，preprocess 只跑一次；投影有没有、设备在不在
    是随时会变的，所以源码里只写 `{% if obs.is_projector %}` 这种条件，
    真正的数据每次都从 app_context_processor 现取。
"""
from __future__ import annotations

import base64
import json
import time

from flask import Blueprint, Response, flash, redirect, request, url_for
from markupsafe import Markup

from cloud import state as cloud_state
from cloud.capture import frame as vcam_frame
from cloud.obs import projector
from cloud.obs import settings as obs_settings

bp = Blueprint("obs", __name__, url_prefix="/obs")

# settings.html 里的锚点：提示音那块前面的注释。
ANCHOR = "{# 提示音（sounds.py"
SECTION_ID = 'id="h-obs"'


def register(app):
    """挂上蓝图 + 模板钩子。幂等：重复调用不会重复注册。"""
    if not app.blueprints.get("obs"):
        app.register_blueprint(bp)
    _install_preprocess(app.jinja_env)


def _install_preprocess(env):
    if getattr(env, "_paimiao_obs_preprocess", False):
        return
    original = env.preprocess

    def preprocess(source, name, filename=None):
        try:
            source = original(source, name, filename)
        except TypeError:      # 老版 jinja 的签名没有 filename
            source = original(source, name)
        return _patch(source)

    env.preprocess = preprocess
    env._paimiao_obs_preprocess = True


def _patch(source: str) -> str:
    """对 settings.html 做两处插入：OBS 那一栏 + 开机自启动那块的浅色提示。都可重复调用。"""
    source = _insert_obs_section(source)
    source = _insert_autostart_note(source)
    return source


def _insert_obs_section(source: str) -> str:
    """把 OBS 那一栏插进去。找不到锚点 / 已经插过 / 锚点不唯一 → 原样返回。"""
    if SECTION_ID in source:              # 幂等：插过了
        return source
    if ANCHOR not in source:
        return source
    if source.count(ANCHOR) != 1:         # 不唯一：宁可不插，也不能插错位置
        return source
    at = source.index(ANCHOR)
    return source[:at] + SECTION + "\n\n    " + source[at:]


# 「开机自动启动」那一块的浅色提示（用户 2026-11-07 要求）：OBS 功能要用户自己打开，
# Evo 只自动检测。锚点是 autostart.failed 那一整行，在 settings.html 里唯一。
AUTOSTART_ANCHOR = ('{% if autostart.failed %}<div class="setting-desc update-error" '
                    'id="autostart-error" role="alert">{{ autostart.failed }}</div>{% endif %}')
AUTOSTART_NOTE_ID = 'id="obs-vcam-note"'
AUTOSTART_NOTE = ('\n          <div class="setting-desc" id="obs-vcam-note">'
                  '使用 OBS 虚拟摄像头或 OBS 源投影时，需要手动打开 OBS 对应功能'
                  '（启动虚拟摄像机 / 全屏投影），软件会自动检测。</div>')


def _insert_autostart_note(source: str) -> str:
    if AUTOSTART_NOTE_ID in source:        # 幂等
        return source
    if source.count(AUTOSTART_ANCHOR) != 1:
        return source
    at = source.index(AUTOSTART_ANCHOR) + len(AUTOSTART_ANCHOR)
    return source[:at] + AUTOSTART_NOTE + source[at:]


# ---------- 渲染期数据 ----------

@bp.app_context_processor
def inject():
    """所有页面都能拿到 `obs`。只有设置页的模板会用到它。"""
    return {"obs": view()}


def view():
    """设置页那一栏要显示的全部内容。永不抛异常（渲染期炸了整个设置页就白屏了）。"""
    source = obs_settings.source()
    is_projector = source == obs_settings.SOURCE_PROJECTOR
    is_camera = source == obs_settings.SOURCE_CAMERA
    is_card = source == obs_settings.SOURCE_CARD

    available, reason, warning, where = False, "", "", ""
    ready = False
    devices: list = []
    obs_running = False
    topmost = False
    monitor_count = 0
    ws: dict = {}
    ws_ok = ws_active = False

    if is_camera or is_card:
        desc = {"available": False, "reason": "读不到状态", "device": "",
                "index": None, "obs_running": False, "devices": []}
        try:
            if is_card:
                desc = vcam_frame.describe_card(obs_settings.card_device())
            else:
                desc = vcam_frame.describe_camera(obs_settings.camera_device())
        except Exception as exc:  # noqa: BLE001
            desc["reason"] = f"检测画面来源时出错：{exc}"
        available = bool(desc.get("available"))
        reason = desc.get("reason", "")
        where = desc.get("device") or ""
        devices = desc.get("devices") or []
        obs_running = bool(desc.get("obs_running")) if is_camera else False
        if is_camera:
            try:
                from cloud.obs import obs_ws  # noqa: PLC0415
                ws = obs_ws.status()
            except Exception:  # noqa: BLE001
                ws = {}
            ws_ok = bool(ws.get("ok"))
            ws_active = bool(ws.get("active"))
        # 「现在能不能真的截到」：相机要虚拟摄像头开着；采集卡要设备在（有没有帧由触发那边按「有帧才算」管）
        ready = available and ((ws_active if ws_ok else True) if is_camera else True)
        if is_camera and available and not obs_running:
            warning = ("找到 OBS 虚拟摄像头，但没检测到 OBS 在运行；请先打开 OBS 并点「启动虚拟摄像机」。")
    else:
        status = {"available": False, "reason": "读不到状态", "warning": "", "monitors": [],
                  "projector": None, "topmost": False, "candidates": []}
        try:
            status = projector.describe()
        except Exception as exc:  # noqa: BLE001
            status["reason"] = f"检测投影窗口时出错：{exc}"
        available = bool(status.get("available"))
        reason = status.get("reason", "")
        warning = status.get("warning", "")
        found = status.get("projector")
        topmost = bool((found or {}).get("topmost"))
        monitor_count = len(status.get("monitors") or [])
        where = (f"{_describe_place(found)}（{found['width']} × {found['height']}）" if found else "")
        ready = available

    hint = ""
    if is_camera or is_projector or is_card:
        # 主机链路（OBS）：抓到的画面全黑就提示检查 OBS/采集卡
        try:
            from cloud.obs import blackwatch  # noqa: PLC0415
            if blackwatch.status().get("black"):
                hint = ("⚠ 最近抓到的画面是全黑的：请检查 OBS 虚拟摄像机有没有开、采集卡有没有信号、"
                        "游戏有没有在抓（看 OBS 预览里有没有画面）。")
        except Exception:  # noqa: BLE001
            hint = ""
    else:
        # 整屏进程（PC）：没检测到游戏启动就红字提示可能原因
        running = True
        try:
            from game_detect import game_running  # noqa: PLC0415  上游模块，只读进程名
            running = bool(game_running())
        except Exception:  # noqa: BLE001 判断不了就别误报
            running = True
        if not running:
            hint = ("⚠ 未检测到游戏启动：没找到 r5apex.exe / r5apex_dx12.exe 进程。"
                    "可能还没打开 Apex、或玩的不是 Apex；整屏模式还要求游戏画面显示在主屏上（别最小化、别放副屏）。")
        else:
            # 游戏在跑，但整屏截到的画面全黑（独占全屏 / 最小化 / 副屏 / 桌面复制没生效等）
            try:
                from cloud.obs import blackwatch  # noqa: PLC0415
                if blackwatch.status().get("black"):
                    hint = ("⚠ 游戏在运行，但抓到的画面是全黑的：最常见是游戏用了「独占全屏」"
                            "（桌面复制截不到独占全屏，会全黑）——把游戏改成「无边框窗口」或窗口化；"
                            "也可能游戏最小化了、被别的窗口挡住、或画面在副屏（整屏模式只截主屏）。")
            except Exception:  # noqa: BLE001
                pass

    stored = obs_settings.camera_device()
    cam_devices = [
        {"index": d.get("index"), "name": d.get("name", ""),
         "selected": ((stored and d.get("name") == stored)
                      or (not stored and "obs virtual camera" in (d.get("name", "").lower())))}
        for d in devices
    ]
    stored_card = obs_settings.card_device()
    card_devices = [
        {"index": d.get("index"), "name": d.get("name", ""),
         "selected": ((stored_card and d.get("name") == stored_card)
                      or (not stored_card and i == 0))}
        for i, d in enumerate(devices)
    ] if is_card else []

    # 「当前状态」那行字（首屏和轮询返回同一句，措辞只有一处）
    if source == obs_settings.SOURCE_SCREEN:
        status_value = ""
    elif is_camera or is_card:
        if available:
            if is_card:
                status_value = f"设备：{where}。Evo 开着就一直记录，画面掉了会自动重连恢复。"
            elif ws_ok:
                if ws_active:
                    status_value = f"设备：{where}。虚拟摄像头已开启，可以截到画面。"
                else:
                    status_value = f"设备：{where}。虚拟摄像头未开启，开启后才能截到画面。"
            else:
                status_value = f"设备：{where}。" + (warning if warning else "可以截到画面（按 OBS 进程判断）。")
        else:
            status_value = reason
    else:
        status_value = ((f"{where}，{warning}" if warning
                         else f"{where}，已置顶，现在就能截到画面。") if available else reason)

    return {
        "source": source,
        "source_screen": obs_settings.SOURCE_SCREEN,
        "source_projector": obs_settings.SOURCE_PROJECTOR,
        "source_camera": obs_settings.SOURCE_CAMERA,
        "source_card": obs_settings.SOURCE_CARD,
        "is_projector": is_projector,
        "is_camera": is_camera,
        "is_card": is_card,
        "enabled": is_projector,          # 兼容旧键（部分验证脚本按它判断投影）
        "available": available,
        "ready": ready,
        "reason": reason,
        "warning": warning,
        "where": where,
        "obs_running": obs_running,
        "topmost": topmost,
        "monitor_count": monitor_count,
        "camera_devices": cam_devices,
        "camera_device": stored,
        "card_devices": card_devices,
        "card_device": stored_card,
        "ws": ws,
        "ws_text": _ws_text(ws),
        "ws_port": obs_settings.ws_port(),
        "ws_has_password": bool(obs_settings.ws_password()),
        "hint": hint,
        "pause_when_closed": obs_settings.pause_when_closed(),
        "status_script": _status_script(),
        "status_every": STATUS_EVERY,
        "_status": {
            "enabled": source != obs_settings.SOURCE_SCREEN,
            "ok": ready,
            "text": status_value,
            "where": where,
            "reason": reason,
        },
    }


# ---------- 动作 ----------

def _ws_text(ws: dict) -> str:
    """「精确触发（OBS WebSocket）」那一行说明。"""
    if not ws:
        return ""
    if not ws.get("enabled"):
        return ("精确触发（OBS WebSocket）：未启用。在 OBS「工具 → WebSocket Server Settings」"
                "勾选启用后，就能按「虚拟摄像头开/关」精确记录；现在按 OBS 进程判断。")
    if ws.get("ok"):
        src = "手动填写" if ws.get("source") == "manual" else "自动读取 OBS 配置"
        return ("精确触发（OBS WebSocket）：已连接（密码：" + src + "），虚拟摄像头"
                + ("已开启。" if ws.get("active") else "未开启（开启后开始记录）。"))
    if ws.get("reason") == "连接中…":
        return "精确触发（OBS WebSocket）：连接中…"
    return (f"精确触发（OBS WebSocket）：连不上（{ws.get('reason', '')}），现在按 OBS 进程判断。")


def _describe_place(found: dict) -> str:
    """把「投影在哪」说成用户看得懂的话。不报序号，只说相对于主屏的位置。"""
    mon = found.get("monitor") or {}
    if mon.get("primary"):
        return "主屏"
    left, top = mon.get("left", 0), mon.get("top", 0)
    if left < 0:
        return "主屏左边那块屏"
    if left > 0:
        return "主屏右边那块屏"
    if top < 0:
        return "主屏上方那块屏"
    if top > 0:
        return "主屏下方那块屏"
    return "另一块屏"


def _settings_url() -> str:
    # 设置页的路由名是 panel_app 里的 `settings`，不要写死路径
    return url_for("settings") + "#obs"


# ---------- 「当前状态」的自动刷新 ----------

STATUS_EVERY = 3000          # 毫秒


@bp.get("/status")
def status():
    """给设置页的脚本用：现在能不能截、截哪儿、为什么不能。只读，不改设置。"""
    return _status_view()


def _status_view() -> dict:
    data = view()
    return {"enabled": True, "ok": data.get("ready", data["available"]), "text": status_text(data),
            "where": data["where"], "reason": data["reason"], "warning": data.get("warning", ""),
            "ws_text": data.get("ws_text", ""),
            "ws_ok": bool((data.get("ws") or {}).get("ok")),
            "hint": data.get("hint", "")}


def status_text(data: dict) -> str:
    """「当前状态」那行字。首屏模板和轮询返回的都是这一句，只有一处措辞。"""
    return (data.get("_status") or {}).get("text", "")


@bp.post("/source")
def set_source():
    """切换画面来源：整屏 / OBS 投影 / OBS 虚拟摄像头 / 采集卡直读。"""
    value = request.form.get("source") or ""
    if value not in obs_settings.SOURCES:
        flash("没认出这个画面来源。", "error")
        return redirect(_settings_url())
    if value == obs_settings.SOURCE_CAMERA:
        device = request.form.get("device")
        if device is not None:
            obs_settings.set_camera_device(device)
    elif value == obs_settings.SOURCE_CARD:
        device = request.form.get("device")
        if device is not None:
            obs_settings.set_card_device(device)
    if not obs_settings.set_source(value):
        flash("没保存成功，请再试一次。", "error")
        return redirect(_settings_url())

    if value == obs_settings.SOURCE_CAMERA:
        try:
            desc = vcam_frame.describe_camera(obs_settings.camera_device())
        except Exception as exc:  # noqa: BLE001
            desc = {"available": False, "reason": str(exc), "device": "", "obs_running": False}
        if desc.get("available"):
            note = "" if desc.get("obs_running") else "（没检测到 OBS 在运行，记得开 OBS 并启动虚拟摄像机）"
            flash(f"已改用 OBS 虚拟摄像头：设备「{desc.get('device')}」{note}。正在记录的话几秒内生效。", "ok")
        else:
            flash(f"已改用 OBS 虚拟摄像头，但现在还读不到：{desc.get('reason', '')}", "error")
    elif value == obs_settings.SOURCE_CARD:
        try:
            desc = vcam_frame.describe_card(obs_settings.card_device())
        except Exception as exc:  # noqa: BLE001
            desc = {"available": False, "reason": str(exc), "device": ""}
        if desc.get("available"):
            flash(f"已改用采集卡（直读）：设备「{desc.get('device')}」。Evo 开着就一直记录，掉画面会自动恢复（OBS 要松开这块卡）。", "ok")
        else:
            flash(f"已改用采集卡（直读），但现在找不到设备：{desc.get('reason', '')}", "error")
    elif value == obs_settings.SOURCE_PROJECTOR:
        try:
            status = projector.describe()
        except Exception as exc:  # noqa: BLE001
            status = {"available": False, "reason": str(exc)}
        if status.get("available"):
            found = status.get("projector") or {}
            note = status.get("warning") or "正在记录的话，几秒内生效。"
            flash(f"已改用 OBS 投影：正在截{_describe_place(found)}。{note}", "ok")
        else:
            flash(f"已改用 OBS 投影，但现在还截不到画面：{status.get('reason', '')}", "error")
    else:
        flash("已改用整屏截取（Evo 原来的方式）。", "ok")
    cloud_state.log(f"画面来源改成：{value}")
    return redirect(_settings_url())


@bp.post("/pause-when-closed")
def set_pause():
    """投影消失时：暂停截屏（默认）/ 退回整屏。"""
    on = request.form.get("on") == "1"
    if not obs_settings.set_pause_when_closed(on):
        flash("没保存成功，请再试一次。", "error")
    return redirect(_settings_url())


@bp.post("/ws")
def set_ws():
    """保存手填的 OBS WebSocket 端口/密码（都留空 = 改回自动读 OBS 配置）。"""
    port = (request.form.get("port") or "").strip()
    password = request.form.get("password") or ""
    if port and not port.isdigit():
        flash("端口要填数字（默认 4455）。", "error")
        return redirect(_settings_url())
    if not obs_settings.set_ws(port, password):
        flash("没保存成功，请再试一次。", "error")
        return redirect(_settings_url())
    flash("已保存 OBS WebSocket 设置" + ("（手动填的端口/密码）" if (port or password) else "（改回自动读取 OBS 配置）") + "。", "ok")
    cloud_state.log(f"OBS WebSocket 设置：端口={'手填' if port else '自动'}，密码={'手填' if password else '自动'}")
    return redirect(_settings_url())


@bp.route("/test-shot", methods=["GET", "POST"])
def test_shot():
    """按当前画面来源截一张，直接把图片返回给浏览器（新标签打开）。只截一张，不记录、不上传。"""
    source = obs_settings.source()
    try:
        png = _capture_once(source)
    except Exception as exc:  # noqa: BLE001 把原因原样给用户看（多半是没开虚拟摄像头/被占用）
        message = f"测试截图失败：{exc}"
        return Response(message, status=200, mimetype="text/plain; charset=utf-8",
                        headers={"Cache-Control": "no-store"})
    ctype = "image/png" if png[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return Response(png, mimetype=ctype, headers={"Cache-Control": "no-store"})


def _encode_png(frame) -> bytes:
    import cv2  # noqa: PLC0415
    ok, buf = cv2.imencode(".png", frame)
    if not ok:
        raise RuntimeError("把截图编码成 PNG 失败")
    return buf.tobytes()


def _obs_ws_screenshot():
    """通过 OBS WebSocket 截 OBS 当前画面（GetSourceScreenshot），返回 PNG 字节或 None。

    好处：不用打开 OBS 虚拟摄像头设备，所以「虚拟摄像头正被记录子进程占用」时也能截。
    """
    try:
        from cloud.obs import obs_ws  # noqa: PLC0415
        cfg = obs_ws.load_config()
        if not cfg or not cfg.get("enabled"):
            return None
        conn = obs_ws.ObsWS(port=cfg.get("port", 4455), password=cfg.get("password", ""))
        conn.connect()
        try:
            info = conn.request("GetCurrentProgramScene")
            scene = info.get("currentProgramSceneName") or info.get("sceneName")
            if not scene:
                return None
            resp = conn.request("GetSourceScreenshot", {"sourceName": scene, "imageFormat": "png"})
            data = resp.get("imageData") or ""
            if data.startswith("data:"):
                data = data.split(",", 1)[1]
            return base64.b64decode(data) if data else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001  拿不到就走 DirectShow 兜底
        return None


def _is_black_frame(frame) -> bool:
    try:
        small = frame[::8, ::8]
        return float(small.mean()) < 10.0 and float(small.std()) < 6.0
    except Exception:  # noqa: BLE001
        return False


def _capture_once(source: str) -> bytes:
    """按来源截一张，返回 PNG 字节。拿不到就抛异常（异常文字会显示给用户）。"""
    if source == obs_settings.SOURCE_CAMERA:
        # 优先问 OBS WebSocket 要当前画面（不占虚拟摄像头设备，记录中也能截）
        png = _obs_ws_screenshot()
        if png:
            return png
        # 兜底：直接打开虚拟摄像头设备（会受“同一时间只能被一个程序打开”的限制）
        from cloud.capture import frame as vcam  # noqa: PLC0415
        cam, _notes = vcam.open_camera(log=lambda m: None, wanted=obs_settings.camera_device())
        try:
            frame = cam.grab()
        finally:
            cam.release()
        if frame is None:
            raise RuntimeError("没拿到画面：虚拟摄像头没开启，或正被别的程序/记录占用")
        return _encode_png(frame)

    if source == obs_settings.SOURCE_CARD:
        from cloud.capture import frame as vcam  # noqa: PLC0415
        # 记录进程正在用采集卡时：只用它存的最近帧，绝不再开卡（开卡会把正在记录的画面打断）
        try:
            from cloud.obs import cardsignal as _cs  # noqa: PLC0415
            alive = _cs.alive_age()
        except Exception:  # noqa: BLE001
            _cs = None
            alive = None
        if _cs is not None and alive is not None and alive < 6:
            snap = _cs.read_snapshot()
            if snap:
                try:
                    import cv2  # noqa: PLC0415
                    import numpy as np  # noqa: PLC0415
                    img = cv2.imdecode(np.frombuffer(snap, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None and _is_black_frame(img):
                        raise RuntimeError("当前画面是全黑的（采集卡没信号）")
                except RuntimeError:
                    raise
                except Exception:  # noqa: BLE001
                    pass
                return snap
            raise RuntimeError("正在记录，还没存到画面，请等一秒再试")
        frame = None
        try:
            cam, _notes = vcam.open_card(log=lambda m: None, wanted=obs_settings.card_device())
            # 采集卡刚打开的头几帧常是黑的（格式/信号还在协商）——多抓几秒直到拿到非黑帧
            deadline = time.time() + 2.5
            try:
                while time.time() < deadline:
                    f = cam.grab()
                    if f is not None:
                        frame = f
                        if not _is_black_frame(f):
                            break
                    time.sleep(0.05)
            finally:
                cam.release()
        except Exception:  # noqa: BLE001
            frame = None
        if frame is not None and not _is_black_frame(frame):
            return _encode_png(frame)
        # 正在记录时采集卡被记录子进程占着，用它的「最近一帧」（记录进程每 ~2 秒存一张）
        try:
            from cloud.obs import cardsignal  # noqa: PLC0415
            snap = cardsignal.read_snapshot()
            if snap:
                try:
                    import cv2  # noqa: PLC0415
                    import numpy as np  # noqa: PLC0415
                    img = cv2.imdecode(np.frombuffer(snap, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None and not _is_black_frame(img):
                        return snap
                except Exception:  # noqa: BLE001
                    return snap
        except Exception:  # noqa: BLE001
            pass
        if frame is not None:
            raise RuntimeError("只拿到全黑画面：采集卡没信号，或刚打开还在初始化，请再点一次")
        raise RuntimeError("没拿到画面：采集卡没信号，或正被别的程序/记录占用")

    if source == obs_settings.SOURCE_PROJECTOR:
        found = projector.find_projector()
        if not found:
            raise RuntimeError("没找到 OBS 投影窗口（请在 OBS 里做全屏投影）")
        import dxcam  # noqa: PLC0415
        cam = dxcam.create(output_idx=found["output_index"], output_color="BGR",
                           processor_backend="numpy")
        try:
            frame = cam.grab()
        finally:
            cam.release()
        if frame is None:
            raise RuntimeError("没拿到画面")
        return _encode_png(frame)

    # 整屏（上游 code/capture.py：dxcam，建不起来会退回 mss）
    import capture as evo_capture  # noqa: PLC0415
    # restart=False：一次性「测试截图」不该走双显卡 dxcam 不可用时的「改显卡节能设置 + 抛 NeedRestart」那条路，
    # 直接让上游退回 mss 截一张即可（2026-11-08 修复）。
    cam, _how = evo_capture.open_camera(log=lambda m: None, restart=False)
    try:
        frame = cam.grab()
    finally:
        cam.release()
    if frame is None:
        raise RuntimeError("没拿到画面")
    return _encode_png(frame)


# ---------- 模板片段 ----------

SECTION = """
    {# OBS 画面来源（cloud/obs/）：整屏 / OBS 投影 / OBS 虚拟摄像头。
       插在「提示音」前面（cloud/obs/page.py 的 ANCHOR）。这里只写条件，数据由 app_context_processor 给。 #}
    <section class="card settings-card" aria-labelledby="h-obs" id="obs">
      <h2 id="h-obs">画面来源</h2>
      <form method="post" action="{{ url_for('obs.set_source') }}" class="setting">
        <input type="hidden" name="token" value="{{ token }}">
        <div class="setting-text">
          <div class="setting-name" id="obs-source-name">从哪里读游戏画面</div>
          <div class="setting-desc">【PC】直接选「整屏进程」；【PC/主机】都能用「OBS 虚拟摄像头」（不占屏、推荐）。
            主机若用「OBS 源投影」，需要一块屏放全屏投影（窗口要前台可见）。</div>
        </div>
        <div class="setting-actions inline">
          <select name="source" aria-labelledby="obs-source-name">
            <option value="{{ obs.source_screen }}" {{ 'selected' if obs.source == obs.source_screen }}>【PC】整屏进程</option>
            <option value="{{ obs.source_camera }}" {{ 'selected' if obs.source == obs.source_camera }}>【PC/主机】OBS虚拟摄像头(推荐)</option>
            <option value="{{ obs.source_projector }}" {{ 'selected' if obs.source == obs.source_projector }}>【主机】OBS源投影(需要窗口前台化)</option>
            <option value="{{ obs.source_card }}" {{ 'selected' if obs.source == obs.source_card }}>【主机】采集卡(直读)</option>
          </select>
          <button type="submit" class="btn">保存</button>
        </div>
      </form>

      <form method="get" action="{{ url_for('obs.test_shot') }}" target="_blank" class="setting">
        <div class="setting-text">
          <div class="setting-name" id="obs-shot-name">测试截图</div>
          <div class="setting-desc">按当前「画面来源」截一张，新标签打开给你看（只截这一张，不记录、不上传）。</div>
        </div>
        <div class="setting-actions inline">
          <button type="submit" class="btn" aria-labelledby="obs-shot-name">测试截图</button>
        </div>
      </form>

      <div class="setting-desc update-error" id="obs-hint" role="alert"
           {% if not obs.hint %}style="display:none"{% endif %}>{{ obs.hint }}</div>

      {% if obs.is_projector %}
      <div class="setting">
        <div class="setting-text">
          <div class="setting-name" id="obs-state-name">当前状态</div>
          <div class="setting-desc" id="obs-state-text"
               data-ok="{{ '1' if obs._status.ok else '0' }}">{{ obs._status.text }}</div>
          <div class="setting-desc">投影窗口<strong>建议置顶</strong>（在窗口上右键 → 勾选「始终置顶」）：
            不置顶也能截，但别的窗口盖住它会截到错的画面。
            OBS 关掉、或投影被切走时，{% if obs.pause_when_closed %}Evo 会暂停截屏等它回来{% else %}Evo 会退回整屏截取{% endif %}。</div>
        </div>
      </div>
      <form method="post" action="{{ url_for('obs.set_pause') }}" class="setting">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="on" value="{{ '0' if obs.pause_when_closed else '1' }}">
        <div class="setting-text">
          <div class="setting-name" id="obs-pause-name">投影消失时</div>
          <div class="setting-desc">开：暂停截屏，等投影回来继续（不会误记别的窗口）。关：退回整屏截取。</div>
        </div>
        <button type="submit" class="switch" role="switch"
                aria-checked="{{ 'true' if obs.pause_when_closed else 'false' }}"
                aria-labelledby="obs-pause-name">
          <span class="switch-track" aria-hidden="true"><span class="switch-knob"></span></span>
          <span class="switch-state" aria-hidden="true">{{ '暂停截屏' if obs.pause_when_closed else '退回整屏' }}</span>
        </button>
      </form>
      {% endif %}

      {% if obs.is_camera %}
      <div class="setting">
        <div class="setting-text">
          <div class="setting-name" id="obs-state-name">当前状态</div>
          <div class="setting-desc" id="obs-state-text"
               data-ok="{{ '1' if obs._status.ok else '0' }}">{{ obs._status.text }}</div>
          <div class="setting-desc" id="obs-ws-text" data-ok="{{ '1' if obs.ws.ok else '0' }}">{{ obs.ws_text }}</div>
        </div>
      </div>
      <form method="post" action="{{ url_for('obs.set_source') }}" class="setting">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="source" value="{{ obs.source_camera }}">
        <div class="setting-text">
          <div class="setting-name" id="obs-device-name">用哪个摄像头设备</div>
          <div class="setting-desc">一般自动用 OBS Virtual Camera。列表不对（有多台摄像头）时可以手动选。</div>
        </div>
        <div class="setting-actions inline">
          <select name="device" aria-labelledby="obs-device-name">
            <option value="" {{ 'selected' if not obs.camera_device }}>自动（OBS Virtual Camera）</option>
            {% for d in obs.camera_devices %}
            <option value="{{ d.name }}" {{ 'selected' if d.selected }}>{{ d.name }}</option>
            {% endfor %}
          </select>
          <button type="submit" class="btn">保存</button>
        </div>
      </form>

      <form method="post" action="{{ url_for('obs.set_ws') }}" class="setting">
        <input type="hidden" name="token" value="{{ token }}">
        <div class="setting-text">
          <div class="setting-name" id="obs-ws-name">OBS WebSocket（可选）</div>
          <div class="setting-desc">Evo 默认自动读取 OBS 的 WebSocket 配置（端口/密码）。万一读不到，可在下面手填；
            两格都留空就是「自动读取」。</div>
        </div>
        <div class="setting-actions inline">
          <input type="text" name="port" inputmode="numeric" placeholder="端口 4455"
                 value="{{ obs.ws_port }}" aria-labelledby="obs-ws-name" style="width:7em">
          <input type="password" name="password" autocomplete="new-password"
                 placeholder="{{ '已保存（留空=自动）' if obs.ws_has_password else '密码（留空=自动）' }}"
                 value="" aria-labelledby="obs-ws-name" style="width:12em">
          <button type="submit" class="btn">保存</button>
        </div>
      </form>
      {% endif %}

      {% if obs.is_card %}
      <div class="setting">
        <div class="setting-text">
          <div class="setting-name" id="obs-state-name">当前状态</div>
          <div class="setting-desc" id="obs-state-text"
               data-ok="{{ '1' if obs._status.ok else '0' }}">{{ obs._status.text }}</div>
        </div>
      </div>
      <form method="post" action="{{ url_for('obs.set_source') }}" class="setting">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="source" value="{{ obs.source_card }}">
        <div class="setting-text">
          <div class="setting-name" id="obs-card-name">用哪个采集卡设备</div>
          <div class="setting-desc">默认自动选第一个「不是 OBS 虚拟摄像头」的采集设备；有多台时可以手动选。</div>
        </div>
        <div class="setting-actions inline">
          <select name="device" aria-labelledby="obs-card-name">
            <option value="" {{ 'selected' if not obs.card_device }}>自动（第一个采集设备）</option>
            {% for d in obs.card_devices %}
            <option value="{{ d.name }}" {{ 'selected' if d.selected }}>{{ d.name }}</option>
            {% endfor %}
          </select>
          <button type="submit" class="btn">保存</button>
        </div>
      </form>
      <div class="setting-desc">直读采集卡时，OBS 要松开这块卡（很多卡同时只能一个程序用）。Evo 开着就一直记录，画面掉了会自动重连恢复，不需要开 OBS。</div>
      {% endif %}

      <details class="move-data">
        <summary>【PC/主机】OBS 虚拟摄像头怎么配（点开看步骤）</summary>
            <div class="setting-desc">
          <ol>
            <li>主机画面接采集卡进 OBS；PC 用「游戏采集」/「窗口采集」。确认 OBS 预览里有画面。</li>
            <li>OBS 右下「控制按钮」→「启动虚拟摄像机」。</li>
            <li>点击虚拟摄像机旁的「设置」图标 →「输出类型」选择「源/程序」。</li>
            <li>（推荐）OBS「工具」→「WebSocket服务器设置」→ 勾选「开启WebSocket服务器」并点「应用」——
              这样 Evo 按「虚拟摄像头开/关」精确记录；端口/密码 Evo 会自动读取，读不到可在下面手填。</li>
            <li>回 Evo 这里选「OBS 虚拟摄像头」并保存；设备一般自动用 OBS Virtual Camera，下面可手动换。</li>
            <li>虚拟摄像头同一时间通常只能一个程序用：预览、会议软件会和 Evo 抢，用 Evo 时先关掉别的。</li>
            <li>PC 玩家可以把游戏放后台或窗口化，只要 OBS 预览里画面还在动就行（不用一直把游戏放前台）。</li>
            <li>OBS 关掉或「停止虚拟摄像机」后，Evo 会停止记录。分辨率、音频都不用改。</li>
          </ol>
        </div>
      </details>

      <details class="move-data">
        <summary>【主机】OBS 源投影怎么配（点开看步骤）</summary>
        <div class="setting-desc">
          <ol>
            <li>主机（Switch / PS5 / Xbox）的画面先接进采集卡，再进 OBS；PC 用 OBS 的「游戏采集」或「窗口采集」。</li>
            <li>OBS 里把采集卡 / 游戏加为来源：来源面板「+」→「视频采集设备」（主机）或「游戏采集」（PC），确认 OBS 预览里有画面。</li>
            <li>在来源（或预览）上右键 →「全屏投影（预览）」→ 选一块屏幕，把画面铺满那块屏。</li>
            <li>在投影窗口上右键 → 勾选「始终置顶」，免得被别的窗口盖住（不置顶也能截，但可能截到盖在上面的窗口）。</li>
            <li>回 Evo 这里选「OBS 投影」并保存，几秒内生效。</li>
            <li>OBS 关掉、投影被切走 / 最小化时，按上面「投影消失时」的设置暂停或退回整屏。分辨率、音频都不用改。</li>
          </ol>
        </div>
      </details>

      {{ obs.status_script }}
    </section>"""


# 「当前状态」自动刷新用的脚本。只在选了 OBS 投影 / 虚拟摄像头时插进页面。
STATUS_SCRIPT = """
      <script>
      (function () {
        var box = document.getElementById("obs-state-text");
        var wsbox = document.getElementById("obs-ws-text");
        var blackbox = document.getElementById("obs-hint");
        var url = {{ __URL__ }};
        var every = {{ __EVERY__ }};

        var timer = null;
        function stop() {
          if (timer) { clearInterval(timer); timer = null; }
        }

        function paint(el, text, good) {
          if (!el) return;
          if (text && el.textContent !== text) el.textContent = text;
          var flag = good ? "1" : "0";
          if (el.dataset.ok !== flag) {
            el.dataset.ok = flag;
            el.classList.toggle("ok", !!good);
            el.classList.toggle("err", !good);
          }
        }

        function render(data) {
          if (!data || !data.enabled) return;
          if (data.text) paint(box, data.text, data.ok);
          if (typeof data.ws_text === "string" && data.ws_text) paint(wsbox, data.ws_text, data.ws_ok);
          if (blackbox) {
            var bh = data.hint || "";
            if (bh) { if (blackbox.textContent !== bh) blackbox.textContent = bh; blackbox.style.display = ""; }
            else { blackbox.textContent = ""; blackbox.style.display = "none"; }
          }
        }

        function tick() {
          if (document.hidden) return;
          fetch(url, { headers: { "Accept": "application/json" }, cache: "no-store" })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) { if (data) render(data); })
            .catch(function () {});
        }

        function start() {
          if (timer) return;
          tick();
          timer = setInterval(tick, every);
        }

        document.addEventListener("visibilitychange", function () {
          if (document.hidden) stop(); else start();
        });
        window.addEventListener("pagehide", stop);

        if (!document.hidden) start();
      })();
      </script>"""


def _status_script() -> Markup:
    """把 STATUS_SCRIPT 里的两个占位换成真值，并标成安全 HTML（否则 <script> 会被转义）。"""
    url = "/obs/status"
    try:
        url = url_for("obs.status")
    except Exception:  # noqa: BLE001 没有请求上下文时退回相对路径
        pass
    return Markup(
        STATUS_SCRIPT
        .replace("{{ __URL__ }}", json.dumps(str(url)))
        .replace("{{ __EVERY__ }}", json.dumps(int(STATUS_EVERY)))
    )
