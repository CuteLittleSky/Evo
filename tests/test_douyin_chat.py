"""抖音的弹幕（code/douyin_chat.py 和面板的 /esports/danmaku-feed/<选手>）：不联网。

- 签名：固定的输入算出来和 2026-11-20 记下的一样（那天用它连上了抖音，改坏了会变）
- protobuf：读得出弹幕内容，要回执的帧给出回执，读不懂的当作什么都没有
- 只够用的 WebSocket：发出去的帧带掩码，三种长度都对；收的时候一帧不够就等、分片的接起来、ping 回 pong、对面关了报错
- 一路弹幕：连上发心跳、之后每 10 秒一个；该回执的回执；45 秒没动静算断了；关掉时把连接关掉
- 面板：要带口令；只给抖音的选手；每格一次只转一路；转给页面的是一行一条

运行：python tests/test_douyin_chat.py
"""

import gzip
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import danmaku
import douyin_chat
import esports_data
import panel_app
import panel_esports
import storage

NOW = datetime.now().astimezone().replace(microsecond=0)


def chat(text, method=b"WebcastChatMessage"):
    """一条消息：Message{1 method, 2 payload{3 content}}。"""
    return douyin_chat.put(1, douyin_chat.put(1, method) + douyin_chat.put(2, douyin_chat.put(3, text.encode("utf-8"))))


def push(messages, need_ack=False, log_id=77, packed=True):
    response = b"".join(messages) + douyin_chat.put(5, b"ext-1") + (douyin_chat.put(9, 1) if need_ack else b"")
    return douyin_chat.put(2, log_id) + douyin_chat.put(8, gzip.compress(response) if packed else response)


def server_frame(payload, opcode=2, final=True):
    """服务器发的一帧（不带掩码）。"""
    head = bytes([(0x80 if final else 0) | opcode])
    size = len(payload)
    if size < 126:
        return head + bytes([size]) + payload
    if size < 65536:
        return head + bytes([126]) + size.to_bytes(2, "big") + payload
    return head + bytes([127]) + size.to_bytes(8, "big") + payload


class FakeConnection:
    """假的网络连接：recv() 照顺序给 chunks 里的东西（给完了算超时），sendall() 记下来。"""

    def __init__(self, chunks):
        self.chunks, self.sent, self.closed = list(chunks), [], False

    def sendall(self, data):
        self.sent.append(bytes(data))

    def recv(self, size):
        if not self.chunks:
            raise douyin_chat.socket.timeout()
        return self.chunks.pop(0)

    def settimeout(self, seconds):
        pass

    def close(self):
        self.closed = True


class FakeSocket:
    """假的 WebSocket：poll() 照顺序给 batches 里的一批批消息。"""

    def __init__(self, batches):
        self.batches, self.sent, self.closed = list(batches), [], False

    def send(self, payload, opcode=2):
        self.sent.append(payload)

    def poll(self, wait=1.0):
        return self.batches.pop(0) if self.batches else []

    def close(self):
        self.closed = True


def main():
    print("抖音的弹幕（签名、protobuf、WebSocket、一路弹幕、面板的接口）")
    out = {}
    out["签名：固定的输入算出来和 2026-11-20 记下的一样；16 个字符；房间、游客编号变了签名跟着变"] = (
        douyin_chat.signature("7575757575757575757", "7312345678901234567", r1=17, r2=200) == "6WNbnYeUF6Li0zfB"
        and douyin_chat.x_bogus("0123456789abcdef0123456789abcdef", r1=0, r2=1) == "fDgZdDSD1vCtUr78"
        and douyin_chat.signature("7575757575757575758", "7312345678901234567", r1=17, r2=200) != "6WNbnYeUF6Li0zfB"
        and len(douyin_chat.signature("1", "2")) == 16)
    visitor = douyin_chat.visitor_id()
    url = douyin_chat.address("wss://example.invalid/push/", "123456789", "7300000000000000000", "Mozilla/5.0 (X) Chrome/1", now=1700000000)
    query = dict(pair.split("=", 1) for pair in url.split("?", 1)[1].split("&"))
    out["连接地址：带房间号、游客编号、时间和签名（+ 和 / 转了码）；游客编号是 7 开头的 19 位数字"] = (
        url.startswith("wss://example.invalid/push/?") and query["room_id"] == "123456789" and query["user_unique_id"] == "7300000000000000000"
        and query["cursor"] == "h-1_t-1700000000000_r-1_d-1_u-1" and query["browser_version"].startswith("5.0%20")
        and "+" not in query["signature"] and "/" not in query["signature"] and len(visitor) == 19 and visitor[0] == "7" and visitor.isdigit())

    texts, ack = douyin_chat.read_push(push([chat("第一条"), chat("别的消息", b"WebcastLikeMessage"), chat("  第二条\n换行  ")], need_ack=True))
    plain, no_ack = douyin_chat.read_push(push([chat("没压缩的")], packed=False))
    long_text, _ = douyin_chat.read_push(push([chat("长" * 500)]))
    out["读一帧：只要弹幕（WebcastChatMessage）的内容，换行和多余的空格收掉，太长的截短；压没压缩都认"] = (
        texts == ["第一条", "第二条 换行"] and plain == ["没压缩的"] and no_ack is None and long_text == ["长" * douyin_chat.MAX_TEXT])
    ack_fields = douyin_chat.fields(ack or b"")
    out["要回执的帧：回执带着这一帧的编号、ack 和服务器给的那一段；读不懂的东西当作什么都没有"] = (
        ack_fields == [(2, 77), (7, b"ack"), (8, b"ext-1")]
        and douyin_chat.read_push(b"\xff\xff\xff") == ([], None) and douyin_chat.read_push(b"") == ([], None)
        and douyin_chat.read_push(douyin_chat.put(8, b"\x1f\x8b not gzip")) == ([], None)
        and douyin_chat.heartbeat() == bytes.fromhex("3a026862"))

    out["发出去的帧带掩码，短的、中等的、长的长度都写对"] = (
        douyin_chat.encode_frame(b"hb", 2, b"\x01\x02\x03\x04").hex() == "8282010203046960"
        and douyin_chat.encode_frame(b"x" * 200, 2, b"\0\0\0\0")[:4] == bytes([0x82, 0x80 | 126, 0, 200])
        and douyin_chat.encode_frame(b"x" * 70000, 2, b"\0\0\0\0")[:10] == bytes([0x82, 0x80 | 127]) + (70000).to_bytes(8, "big"))
    whole = server_frame(b"a" * 300)
    masked = douyin_chat.encode_frame(b"masked", 2, b"\x09\x08\x07\x06")
    out["收的时候：不够一帧就等；够了取出来、剩下的留着；带掩码的解开；太大的不收"] = (
        douyin_chat.take_frame(whole[:3]) is None and douyin_chat.take_frame(whole[:100]) is None
        and douyin_chat.take_frame(whole + b"rest") == (True, 2, b"a" * 300, b"rest")
        and douyin_chat.take_frame(masked)[2] == b"masked"
        and _raises(lambda: douyin_chat.take_frame(bytes([0x82, 127]) + (douyin_chat.MAX_FRAME + 1).to_bytes(8, "big"))))

    hello = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"
    line = FakeConnection([hello + server_frame(b"one")[:2], server_frame(b"one")[2:] + server_frame(b"part-", final=False),
                           server_frame(b"two", opcode=0), server_frame(b"ping!", opcode=9) + server_frame(b"text", opcode=1),
                           server_frame(b"", opcode=8)])
    sock = douyin_chat.Socket("wss://example.invalid:8443/push/?a=1", {"User-Agent": "UA", "Cookie": "ttwid=T"}, connect=lambda host, port: line)
    request = line.sent[0].decode("utf-8")
    first, second, third = sock.poll(), sock.poll(), sock.poll()
    out["握手：发的是 WebSocket 升级请求，带上给的请求头；回 101 才算连上"] = (
        request.startswith("GET /push/?a=1 HTTP/1.1\r\nHost: example.invalid\r\n") and "Upgrade: websocket\r\n" in request
        and "User-Agent: UA\r\n" in request and "Cookie: ttwid=T\r\n" in request and request.endswith("\r\n\r\n")
        and _raises(lambda: douyin_chat.Socket("wss://example.invalid/x", {}, connect=lambda host, port: FakeConnection([b"HTTP/1.1 403 Forbidden\r\n\r\n"])))
        and _raises(lambda: douyin_chat.Socket("http://example.invalid/x", {})))
    out["收消息：跨两次收到的一帧接得上；分片的接成一条；ping 回 pong；文字消息不要；对面关了报错；一时没有就是空的"] = (
        first == [b"one"] and second == [b"part-two"] and third == []
        and douyin_chat.take_frame(line.sent[1])[1:3] == (10, b"ping!") and _raises(sock.poll) and sock.poll.__self__.buffer is not None)
    sock.close()

    clock = [0.0]
    fake = FakeSocket([[push([chat("甲")], need_ack=True)], [], [push([chat("乙"), chat("丙")])], []])
    feed = douyin_chat.Feed("888", open_socket=lambda url, headers: fake, inner="7300000000000000001", clock=lambda: clock[0])
    stream = feed.texts()
    got = [next(stream)]
    clock[0] = 4.0
    got.append(next(stream))
    clock[0] = 10.5
    got.append(next(stream))
    beats = [item for item in fake.sent if item == douyin_chat.heartbeat()]
    out["一路弹幕：连上先发心跳，过了 10 秒再发一个；要回执的回执；收到的弹幕一批批给出来，没有就是空的一批"] = (
        got == [["甲"], [], ["乙", "丙"]] and len(beats) == 2 and any(b"ack" in item for item in fake.sent) and len(fake.sent) == 3)
    clock[0] = 10.5 + douyin_chat.SILENCE + 1
    out["45 秒什么都没收到：算断了、把连接关掉；自己关掉也关连接"] = _raises(lambda: next(stream)) and fake.closed and feed.closed
    real_url = douyin_chat.live_status.PLATFORMS["douyin"]["danmaku_url"]
    douyin_chat.live_status.PLATFORMS["douyin"]["danmaku_url"] = ""
    try:
        off = _raises(lambda: douyin_chat.Feed("888", open_socket=lambda url, headers: fake, inner="1")) and not danmaku.supported("douyin")
    finally:
        douyin_chat.live_status.PLATFORMS["douyin"]["danmaku_url"] = real_url
    out["配置里把地址去掉：抖音就没有弹幕；没找到这一场（没开播）也不连"] = (
        off and danmaku.supported("douyin") and _raises(lambda: douyin_chat.Feed("888", open_socket=lambda url, headers: fake, inner="")))

    # ---- 面板 ----
    folder = tempfile.mkdtemp(prefix="evo-douyin-chat-")
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    token = panel_app.app.config["TOKEN"]
    players = [{"id": "douyin-88", "name": "示例选手1", "team": "示例战队", "platform": "douyin", "room": "88"},
               {"id": "douyu-89", "name": "示例选手2", "team": "示例战队", "platform": "douyu", "room": "89"}]
    events = [{"id": "live", "name": "示例赛", "type": "联赛", "stage": "", "start": (NOW - timedelta(hours=1)).isoformat(),
               "end": (NOW + timedelta(hours=3)).isoformat(), "teams": ["示例战队"]}]
    path = Path(folder) / "esports.json"
    path.write_text(json.dumps({"version": 1, "updated": NOW.isoformat(), "players": players, "events": events}), encoding="utf-8")
    esports_data.URL = str(path)
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    client.get("/esports")
    made = []

    class FakeFeed:
        def __init__(self, room):
            self.room, self.closed = room, False
            made.append(self)

        def texts(self, wait=1.0):
            yield ["你好", '带"引号"的<b>字</b>']
            yield []
            raise douyin_chat.ChatError("对面关了")

        def close(self):
            self.closed = True

    real_feed, douyin_chat.Feed = douyin_chat.Feed, FakeFeed
    try:
        info = client.get("/esports/danmaku/douyin-88?s=2").get_json()
        refused = [client.get("/esports/danmaku-feed/douyin-88").status_code, client.get("/esports/danmaku-feed/douyin-88?t=wrong").status_code]
        missing = [client.get(f"/esports/danmaku-feed/{pid}?t={token}").status_code for pid in ("douyu-89", "nobody-1")]
        count = len(made)
        one = client.get(f"/esports/danmaku-feed/douyin-88?t={token}", buffered=False)
        other = client.get(f"/esports/danmaku-feed/douyin-88?s=1&t={token}", buffered=False)
        two = client.get(f"/esports/danmaku-feed/douyin-88?t={token}", buffered=False)
        body = b"".join(two.response).decode("utf-8")
        lines = [json.loads(line[6:]) for line in body.split("\n") if line.startswith("data: ")]

        def broken(room):
            raise douyin_chat.ChatError("抖音的弹幕服务器没让连")

        douyin_chat.Feed = broken
        failed = client.get(f"/esports/danmaku-feed/douyin-88?t={token}")
    finally:
        douyin_chat.Feed = real_feed
        panel_esports._chats.clear()
    out["问弹幕时：抖音回的是面板自己的转弹幕地址（带口令、带第几格），没有抖音的任何东西"] = (
        info == {"ok": True, "platform": "douyin", "feed": f"/esports/danmaku-feed/douyin-88?s=2&t={token}"})
    out["转弹幕的接口要带面板的口令（没带、带错都拒绝，也不去连抖音）；只给抖音的选手"] = (
        refused == [403, 403] and missing == [404, 404] and count == 0)
    out["转给页面的是一行一条（JSON，内容只是文字）；每一格一次只转一路：这一格开新的一路，上一路就关，别的格子不动"] = (
        one.status_code == 200 and one.mimetype == "text/event-stream" and lines == [{"text": "你好"}, {"text": '带"引号"的<b>字</b>'}]
        and len(made) == 3 and made[0].closed and not made[1].closed and made[0].room == "88")
    out["连不上抖音：回 502 和原因（页面就不显示弹幕）"] = (
        failed.status_code == 502 and failed.get_json() == {"ok": False, "reason": "upstream", "detail": "抖音的弹幕服务器没让连"})
    one.close()
    other.close()
    return report(out)


def _raises(action):
    try:
        action()
    except douyin_chat.ChatError:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
