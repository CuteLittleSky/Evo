"""最小 OBS WebSocket 客户端（零新依赖，只连本机）。

用途：选「OBS 虚拟摄像头」时，用 obs-websocket 的 `GetVirtualCamStatus` 精确判断
「虚拟摄像头开没开」，代替原来「OBS 进程在就常开记录」的粗判断；连不上就退回进程判断。

只用标准库（socket / base64 / hashlib / json / struct）。只连 127.0.0.1，不做 TLS/代理。

OBS 端：OBS 28+ 自带 obs-websocket。工具 → WebSocket Server Settings → 勾选启用
（默认端口 4455，可设密码）。配置在
    %APPDATA%\\obs-studio\\plugin_config\\obs-websocket\\config.json
读到 server_enabled=False 就认为「没启用」，不连，交给上层退回进程判断。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import uuid

HOST = "127.0.0.1"
DEFAULT_PORT = 4455
CONNECT_TIMEOUT = 4.0
POLL_SECONDS = 3.0        # 连着时多久问一次虚拟摄像头状态
RETRY_SECONDS = 10.0      # 连不上/断了多久重试
STALE_SECONDS = 12.0      # 状态超过这么久没更新就当「不知道」，退回进程判断
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------- 配置 ----------

def config_path() -> str:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "obs-studio", "plugin_config", "obs-websocket", "config.json")


def _read_obs_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_config() -> dict | None:
    """读连接参数：默认读 OBS 自己的 config.json；设置页手填了端口/密码就用手的（防止读不到配置）。

    返回 dict 里 `source` = "obs"（自动读到的）或 "manual"（设置页手填的）。
    读不到配置、也没手填 → 返回 None（上层退回「OBS 进程判断」）。
    """
    data = _read_obs_config()
    base = {
        "enabled": bool(data and data.get("server_enabled")),
        "port": int((data or {}).get("server_port") or DEFAULT_PORT),
        "auth_required": bool((data or {}).get("auth_required")),
        "password": str((data or {}).get("server_password") or ""),
        "source": "obs",
    }
    manual_port = manual_password = ""
    try:
        from cloud.obs import settings as obs_settings  # noqa: PLC0415
        manual_port = obs_settings.ws_port()
        manual_password = obs_settings.ws_password()
    except Exception:
        pass
    if manual_port or manual_password:
        port = base["port"]
        if str(manual_port).strip().isdigit():
            port = int(str(manual_port).strip())
        base.update({
            "enabled": True,        # 手填了就当作要连；服务没开时会连不上→自动退回进程判断
            "port": port,
            "password": manual_password or base["password"],
            "source": "manual",
        })
    if not data and not (manual_port or manual_password):
        return None
    return base


# ---------- WebSocket 帧 ----------

def _mask_frame(opcode: int, payload: bytes) -> bytes:
    """客户端发给服务端的帧必须掩码。"""
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


class _WS:
    """够用的 WebSocket 客户端：文本帧、ping/pong、分片。"""

    def __init__(self, sock: socket.socket, pre: bytes = b""):
        self._sock = sock
        self._buf = b""   # 分片拼接用
        self._pre = pre   # 握手时和 101 响应同一个包里可能已经带来第一条消息

    def _recv_exact(self, n: int) -> bytes:
        out = b""
        if self._pre:
            take = min(n, len(self._pre))
            out, self._pre = self._pre[:take], self._pre[take:]
        while len(out) < n:
            chunk = self._sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("连接被关闭")
            out += chunk
        return out

    def send_text(self, text: str) -> None:
        self._sock.sendall(_mask_frame(0x1, text.encode("utf-8")))

    def recv_text(self) -> str:
        """读一条完整文本消息（自动处理分片、ping）。"""
        while True:
            b0, b1 = self._recv_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 0x8:          # close
                raise ConnectionError("服务端关闭了连接")
            if opcode == 0x9:          # ping → pong
                self._sock.sendall(_mask_frame(0xA, payload))
                continue
            if opcode == 0xA:          # pong
                continue
            if opcode in (0x1, 0x2):   # text / binary（可能还有后续分片）
                self._buf = payload
                if fin:
                    text, self._buf = self._buf, b""
                    return text.decode("utf-8", "replace")
                continue
            if opcode == 0x0:          # continuation
                self._buf += payload
                if fin:
                    text, self._buf = self._buf, b""
                    return text.decode("utf-8", "replace")
                continue
            # 其它帧忽略


# ---------- obs-websocket 连接 ----------

def _auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode()


class ObsWS:
    """一条到 obs-websocket 的连接：握手 + 认证 + 请求。"""

    def __init__(self, port: int = DEFAULT_PORT, password: str = ""):
        self.port = port
        self.password = password
        self._sock = None
        self._ws = None

    def connect(self) -> None:
        sock = socket.create_connection((HOST, self.port), timeout=CONNECT_TIMEOUT)
        sock.settimeout(CONNECT_TIMEOUT)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET / HTTP/1.1\r\nHost: {HOST}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手时连接被关闭")
            header += chunk
            if len(header) > 65536:
                raise ConnectionError("握手响应过长")
        head, _, leftover = header.partition(b"\r\n\r\n")
        text = head.decode("latin-1")
        if " 101 " not in text.split("\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket 握手失败：{text.splitlines()[0] if text else ''}")
        expect = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if f"sec-websocket-accept: {expect}".lower() not in text.lower():
            raise ConnectionError("Sec-WebSocket-Accept 校验失败")
        self._sock = sock
        self._ws = _WS(sock, pre=leftover)
        self._handshake_obs()

    def _handshake_obs(self) -> None:
        hello = self._recv_json()
        if hello.get("op") != 0:
            raise ConnectionError(f"没收到 Hello（op={hello.get('op')}）")
        d = hello.get("d") or {}
        rpc_version = int(d.get("rpcVersion") or 1)
        identify = {"rpcVersion": rpc_version, "eventSubscriptions": 0}
        auth = d.get("authentication")
        if auth:
            identify["authentication"] = _auth_string(
                self.password, str(auth.get("salt") or ""), str(auth.get("challenge") or ""))
        self._send_json({"op": 1, "d": identify})
        reply = self._recv_json()
        if reply.get("op") != 2:      # 2 = Identified
            raise ConnectionError(f"认证失败（op={reply.get('op')}：{reply.get('d')}）")

    def request(self, request_type: str, request_data: dict | None = None) -> dict:
        rid = uuid.uuid4().hex
        payload = {"requestType": request_type, "requestId": rid}
        if request_data:
            payload["requestData"] = request_data
        self._send_json({"op": 6, "d": payload})
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            msg = self._recv_json()
            if msg.get("op") != 7:     # 7 = RequestResponse
                continue               # 跳过事件
            d = msg.get("d") or {}
            if d.get("requestId") != rid:
                continue
            status = d.get("requestStatus") or {}
            if not status.get("result"):
                raise ConnectionError(f"{request_type} 失败：{status.get('comment') or status}")
            return d.get("responseData") or {}
        raise TimeoutError(f"{request_type} 超时")

    def close(self) -> None:
        try:
            if self._ws is not None:
                self._sock.sendall(_mask_frame(0x8, b""))
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = self._ws = None

    # ---- 低层 JSON ----
    def _send_json(self, obj: dict) -> None:
        self._ws.send_text(json.dumps(obj))

    def _recv_json(self) -> dict:
        return json.loads(self._ws.recv_text())


# ---------- 后台轮询 + 缓存 ----------

_lock = threading.Lock()
_state = {"available": False, "active": False, "enabled": False, "reason": "还没连",
          "port": DEFAULT_PORT, "source": "", "at": 0.0}
_started = False


def _update(**kw) -> None:
    with _lock:
        _state.update(kw)
        _state["at"] = time.monotonic()


def _snapshot() -> dict:
    with _lock:
        return dict(_state)


def ensure_started() -> None:
    """第一次用到时启动后台轮询线程（daemon）。幂等。"""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="obs-websocket", daemon=True).start()


def _loop() -> None:
    while True:
        cfg = load_config()
        if not cfg or not cfg["enabled"]:
            reason = ("没找到 obs-websocket 配置" if not cfg
                      else "OBS WebSocket 服务器没启用（OBS：工具 → WebSocket Server Settings）")
            _update(available=False, enabled=bool(cfg and cfg["enabled"]),
                    reason=reason, port=(cfg or {}).get("port", DEFAULT_PORT),
                    source=(cfg or {}).get("source", ""))
            time.sleep(RETRY_SECONDS)
            continue
        conn = None
        try:
            conn = ObsWS(port=cfg["port"], password=cfg["password"])
            conn.connect()
            _update(available=True, enabled=True, reason="", port=cfg["port"],
                    source=cfg.get("source", ""))
            while True:
                data = conn.request("GetVirtualCamStatus")
                _update(available=True, enabled=True, reason="",
                        active=bool(data.get("outputActive")), port=cfg["port"],
                        source=cfg.get("source", ""))
                time.sleep(POLL_SECONDS)
        except Exception as exc:  # noqa: BLE001  连不上就退回进程判断，不刷屏
            _update(available=False, enabled=True,
                    reason=f"{type(exc).__name__}: {exc}", port=(cfg or {}).get("port", DEFAULT_PORT),
                    source=(cfg or {}).get("source", ""))
            time.sleep(RETRY_SECONDS)
        finally:
            if conn is not None:
                conn.close()


def virtual_cam_active():
    """虚拟摄像头开没开。True/False；**连不上或状态过期返回 None**（上层退回进程判断）。"""
    ensure_started()
    snap = _snapshot()
    if not snap.get("available"):
        return None
    if snap.get("at") and time.monotonic() - snap["at"] > STALE_SECONDS:
        return None
    return bool(snap.get("active"))


def status() -> dict:
    """给设置页用：当前连接情况。会按需启动轮询。"""
    ensure_started()
    snap = _snapshot()
    if not snap.get("at"):            # 轮询线程还没来得及写状态：先按配置给个初始值
        cfg = load_config()
        if not cfg:
            snap.update(enabled=False, reason="没找到 obs-websocket 配置")
        elif not cfg.get("enabled"):
            snap.update(enabled=False,
                        reason="OBS WebSocket 服务器没启用（OBS：工具 → WebSocket Server Settings）")
        else:
            snap.update(enabled=True, reason="连接中…", port=cfg.get("port", DEFAULT_PORT),
                        source=cfg.get("source", ""))
    snap["ok"] = bool(snap.get("available"))
    return snap
