"""抖音的弹幕（只看不发）：托盘里的 Evo 连抖音的弹幕服务器，把收到的弹幕一条条转给看直播页（面板的 /esports/danmaku-feed/<选手>）。

斗鱼、B站的弹幕是页面里的脚本直接连平台（danmaku.py），抖音不行：连接时要带抖音给的游客 Cookie 和浏览器那样的来源页，
网页里的脚本带不了，所以由 Evo 的后台去连（和斗鱼的视频经本机中转是一个道理），只在这台电脑里转给窗口。
2026-11-20 做的（用户看了开源的 PureLive https://github.com/wzgrx/PureLive 以后要的弹幕；协议和签名是照它读来的，代码是自己写的）。
没有加依赖：Python 没有自带的 WebSocket，这里写了一个只够用的客户端（TLS、握手、收发二进制帧、回 ping）。

- 地址：config/watch.json 里 douyin 的 danmaku_url（去掉它抖音就没有弹幕）；参数里要这一场的内部房间号（live_status 查开播状态时
  记着，没记着就开一次直播间网页）、一个随机的游客编号和一个签名（signature()：固定的参数串 → MD5 → X-Bogus）
- 请求带浏览器的 User-Agent、来源页和游客 Cookie（ttwid，live_status 开直播间网页时抖音给的，只在内存里）；不登录
- 收到的每一帧是 protobuf：PushFrame{2 logId, 8 payload（gzip）}，里面 Response{1 messages, 5 internalExt, 9 needAck}，
  Message{1 method, 2 payload}；method 是 WebcastChatMessage 的，payload 里 3 是弹幕内容。要回执的帧先回 ack
- 连上发一个心跳（PushFrame{7: "hb"}），之后每 10 秒一个；45 秒什么都没收到算断了

用法：python code/douyin_chat.py 抖音房间号   → 连 20 秒，打印收到几条（不打印内容）
"""

import base64
import gzip
import hashlib
import os
import random
import socket
import ssl
import struct
import sys
import time
import urllib.parse

import live_status

ALPHABET = "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe"  # X-Bogus 自己的 base64 字母表
HEARTBEAT = 10      # 秒：多久发一个心跳
SILENCE = 45        # 秒：这么久什么都没收到算断了
MAX_FRAME = 4 * 1024 * 1024
MAX_TEXT = 200      # 一条弹幕最多转这么多字（页面只显示前 60 个）
QUERY = {"app_name": "douyin_web", "version_code": "180800", "webcast_sdk_version": "1.0.15", "update_version_code": "1.0.15",
         "compress": "gzip", "host": "https://live.douyin.com", "aid": "6383", "live_id": "1", "did_rule": "3", "debug": "false",
         "maxCacheMessageNumber": "20", "endpoint": "live_pc", "support_wrds": "1", "im_path": "/webcast/im/fetch/",
         "device_platform": "web", "cookie_enabled": "true", "screen_width": "1920", "screen_height": "1080",
         "browser_language": "zh-CN", "browser_platform": "Win32", "browser_name": "Mozilla", "browser_online": "true",
         "tz_name": "Asia/Shanghai", "identity": "audience", "need_persist_msg_count": "15", "heartbeatDuration": "0"}


class ChatError(Exception):
    """连不上、被拒绝、断了。"""


# ---------- 签名 ----------

def _rc4(key, data):
    box, j = list(range(256)), 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) % 256
        box[i], box[j] = box[j], box[i]
    out, i, j = [], 0, 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + box[i]) % 256
        box[i], box[j] = box[j], box[i]
        out.append(byte ^ box[(box[i] + box[j]) % 256])
    return out


def x_bogus(stub_hex, r1=None, r2=None):
    """32 个十六进制字符 → 16 个字符的 X-Bogus。r1、r2：两个随机数（测试时固定）。"""
    r1 = random.randrange(256) if r1 is None else r1
    r2 = random.randrange(255) if r2 is None else r2
    digest = hashlib.md5(bytes.fromhex(stub_hex)).digest()  # noqa: S324  抖音规定用 MD5，不是拿来保密的
    payload = [1, 0, 1, 0x0E, 0x45, 0x3F, digest[14], digest[15], r2, 0]
    for i in range(9):
        payload[9] ^= payload[i]
    data = [0x40 | (r1 & 0x1F), r2, *_rc4([r2], payload)]
    out = ""
    for i in range(0, len(data), 3):
        n = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
        out += "".join(ALPHABET[(n >> (18 - 6 * j)) & 0x3F] for j in range(4))
    return out


def signature(room_id, visitor, r1=None, r2=None):
    stub = ",".join(["live_id=1", "aid=6383", "version_code=180800", "webcast_sdk_version=1.0.15", f"room_id={room_id}", "sub_room_id=",
                     "sub_channel_id=", "did_rule=3", f"user_unique_id={visitor}", "device_platform=web", "device_type=", "ac=",
                     "identity=audience"])
    return x_bogus(hashlib.md5(stub.encode("utf-8")).hexdigest(), r1, r2)  # noqa: S324


def visitor_id():
    """随机的游客编号：7、一位 3–9、再 17 位数字（和网页版的写法一样）。"""
    return "7" + str(random.randrange(3, 10)) + "".join(str(random.randrange(10)) for _ in range(17))


def address(base, room_id, visitor, user_agent, now=None):
    query = {**QUERY, "cursor": f"h-1_t-{int((time.time() if now is None else now) * 1000)}_r-1_d-1_u-1", "user_unique_id": visitor,
             "browser_version": user_agent[len("Mozilla/"):] if user_agent.startswith("Mozilla/") else user_agent,
             "room_id": str(room_id), "signature": signature(room_id, visitor)}
    return base + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)


# ---------- protobuf（只读、只写用到的那几种）----------

def _varint(data, i):
    value, shift = 0, 0
    while True:
        if i >= len(data) or shift > 63:
            raise ValueError("protobuf 不完整")
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def fields(data):
    """一段 protobuf → [(编号, 值)]；值是整数（varint）或者字节。写得不对抛 ValueError。"""
    out, i = [], 0
    while i < len(data):
        key, i = _varint(data, i)
        number, kind = key >> 3, key & 7
        if kind == 0:
            value, i = _varint(data, i)
        elif kind == 2:
            size, i = _varint(data, i)
            if i + size > len(data):
                raise ValueError("protobuf 不完整")
            value, i = bytes(data[i:i + size]), i + size
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
            value, i = bytes(data[i:i + size]), i + size
        else:
            raise ValueError("protobuf 里有不认识的类型")
        out.append((number, value))
    return out


def first(items, number, default=None):
    return next((value for n, value in items if n == number), default)


def _put_varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def put(number, value):
    if isinstance(value, int):
        return _put_varint(number << 3) + _put_varint(value)
    return _put_varint(number << 3 | 2) + _put_varint(len(value)) + value


def heartbeat():
    return put(7, b"hb")


def read_push(body):
    """服务器发来的一帧 → (弹幕内容的列表, 要回的回执或 None)。读不懂的当作什么都没有。"""
    try:
        frame = fields(body)
        payload = first(frame, 8, b"")
        if not isinstance(payload, bytes):
            return [], None
        if payload[:2] == b"\x1f\x8b":
            payload = gzip.decompress(payload)
        response = fields(payload)
    except (ValueError, OSError, EOFError):
        return [], None
    ack = None
    if first(response, 9):
        log_id, ext = first(frame, 2, 0), first(response, 5, b"")
        ack = put(2, log_id if isinstance(log_id, int) else 0) + put(7, b"ack") + put(8, ext if isinstance(ext, bytes) else b"")
    texts = []
    for number, message in response:
        if number != 1 or not isinstance(message, bytes):
            continue
        try:
            item = fields(message)
            if first(item, 1) != b"WebcastChatMessage":
                continue
            content = first(fields(first(item, 2, b"")), 3, b"")
        except ValueError:
            continue
        if isinstance(content, bytes) and content:
            text = " ".join(content.decode("utf-8", "replace").split())[:MAX_TEXT]
            if text:
                texts.append(text)
    return texts, ack


# ---------- 只够用的 WebSocket 客户端 ----------

def encode_frame(payload, opcode=2, mask=None):
    """客户端发的一帧（必须带掩码）。"""
    mask = os.urandom(4) if mask is None else mask
    size = len(payload)
    if size < 126:
        head = bytes([0x80 | opcode, 0x80 | size])
    elif size < 65536:
        head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", size)
    else:
        head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", size)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def take_frame(buffer):
    """缓冲区开头够一帧就取出来：返回 (是不是最后一片, 操作码, 内容, 剩下的)；不够返回 None。太大抛 ChatError。"""
    if len(buffer) < 2:
        return None
    size, at = buffer[1] & 0x7F, 2
    if size == 126:
        if len(buffer) < 4:
            return None
        size, at = struct.unpack(">H", buffer[2:4])[0], 4
    elif size == 127:
        if len(buffer) < 10:
            return None
        size, at = struct.unpack(">Q", buffer[2:10])[0], 10
    if size > MAX_FRAME:
        raise ChatError("弹幕服务器发来的一帧太大")
    masked = bool(buffer[1] & 0x80)
    end = at + (4 if masked else 0) + size
    if len(buffer) < end:
        return None
    data = buffer[at + (4 if masked else 0):end]
    if masked:
        mask = buffer[at:at + 4]
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return bool(buffer[0] & 0x80), buffer[0] & 0x0F, bytes(data), buffer[end:]


class Socket:
    """连上、握手；poll() 等一小会儿，返回这段时间里收齐的二进制消息。"""

    def __init__(self, url, headers, timeout=10, connect=None):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "wss" or not parts.hostname:
            raise ChatError("弹幕服务器的地址不对")
        try:
            if connect is not None:
                self.sock = connect(parts.hostname, parts.port or 443)
            else:
                raw = socket.create_connection((parts.hostname, parts.port or 443), timeout=timeout)
                self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=parts.hostname)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            lines = [f"GET {parts.path}?{parts.query} HTTP/1.1", f"Host: {parts.hostname}", "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", *[f"{name}: {value}" for name, value in headers.items()], "", ""]
            self.sock.sendall("\r\n".join(lines).encode("utf-8"))
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.sock.recv(4096)
                if not chunk or len(head) > 65536:
                    break
                head += chunk
        except OSError as e:
            raise ChatError(f"连不上抖音的弹幕服务器（{e}）") from e
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status + " ":
            self.close()
            raise ChatError(f"抖音的弹幕服务器没让连（{status[:60] or '没有回话'}）")
        self.buffer = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
        self._pieces, self._opcode = b"", None

    def send(self, payload, opcode=2):
        try:
            self.sock.sendall(encode_frame(payload, opcode))
        except OSError as e:
            raise ChatError(f"和抖音的弹幕服务器断开了（{e}）") from e

    def poll(self, wait=1.0):
        """最多等 wait 秒，返回收齐的消息 [字节, ...]。对面关了、断了抛 ChatError。"""
        try:
            self.sock.settimeout(wait)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ChatError("抖音的弹幕服务器把连接关了")
            self.buffer += chunk
        except (socket.timeout, ssl.SSLWantReadError):
            pass
        except OSError as e:
            raise ChatError(f"和抖音的弹幕服务器断开了（{e}）") from e
        out = []
        while True:
            found = take_frame(self.buffer)
            if found is None:
                return out
            final, opcode, data, self.buffer = found
            if opcode == 9:  # ping → 回 pong
                self.send(data, 10)
            elif opcode == 8:
                raise ChatError("抖音的弹幕服务器把连接关了")
            elif opcode in (0, 1, 2):
                self._opcode = opcode or self._opcode
                self._pieces += data
                if len(self._pieces) > MAX_FRAME:
                    raise ChatError("弹幕服务器发来的一条消息太大")
                if final:
                    if self._opcode == 2:
                        out.append(self._pieces)
                    self._pieces, self._opcode = b"", None

    def close(self):
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001, S110
            pass


# ---------- 一路弹幕 ----------

class Feed:
    """一个抖音直播间的弹幕：texts() 一直给收到的弹幕（每次一批，可能是空的，好让上面知道还连着）；close() 断开。"""

    def __init__(self, room, open_socket=None, inner=None, clock=time.monotonic, on_event=None):
        self.room, self.clock = str(room), clock
        self.on_event = on_event or (lambda _text: None)
        self.closed, self.socket = False, None
        cfg = live_status.PLATFORMS["douyin"]
        base = cfg.get("danmaku_url")
        if not isinstance(base, str) or not base.startswith("wss://"):
            raise ChatError("抖音的弹幕没有开")
        room_id = inner if inner is not None else (live_status.douyin_known(self.room) or live_status.douyin_inner(self.room))
        if not room_id:
            raise ChatError("没找到这个直播间现在这一场（可能没开播）")
        ttwid = next((cookie.value for cookie in live_status._cookies if cookie.name == "ttwid"), "")
        headers = {"User-Agent": cfg["user_agent"], "Origin": "https://live.douyin.com"}
        if ttwid:
            headers["Cookie"] = f"ttwid={ttwid}"
        url = address(base, room_id, visitor_id(), cfg["user_agent"])
        self.socket = (open_socket or Socket)(url, headers)

    def texts(self, wait=1.0):
        try:
            self.socket.send(heartbeat())
            beat = heard = self.clock()
            while not self.closed:
                frames = self.socket.poll(wait)
                now = self.clock()
                out = []
                for frame in frames:
                    heard = now
                    found, ack = read_push(frame)
                    if ack is not None:
                        self.socket.send(ack)
                    out += found
                if now - beat >= HEARTBEAT:
                    self.socket.send(heartbeat())
                    beat = now
                if now - heard > SILENCE:
                    raise ChatError("抖音的弹幕服务器很久没有动静")
                yield out
        finally:
            self.close()

    def close(self):
        self.closed = True
        if self.socket is not None:
            self.socket.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    try:
        feed = Feed(sys.argv[1])
    except (ChatError, live_status.StatusError) as error:
        raise SystemExit(f"没连上：{error}") from None
    start, count = time.time(), 0
    for batch in feed.texts():
        count += len(batch)
        if time.time() - start > 20:
            feed.close()
    print(f"20 秒里收到 {count} 条弹幕")
