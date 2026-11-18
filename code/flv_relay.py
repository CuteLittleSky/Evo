"""FLV 直播流的本机中转：把平台的视频数据原样转给页面里的播放器，地址到期、连接断了就换一条新的接上，播放不断。

为什么要它：斗鱼的视频服务器不让网页直接连，有的地址还限 300 秒（到点服务器就断开）。看直播页的播放器（mpegts.js）
连的是 Evo 自己（面板的 /esports/stream/…，只听 127.0.0.1），Evo 再去连平台。数据只是经过，不存盘、不转推。

怎么接（照 DTV internal/services/flv_relay.go 的思路，2026-11-17 探路时实测过，见 docs/底座.md"看直播"）：
- FLV 是一个文件头后面跟着一个个"标签"（视频、音频、说明信息），每个标签带时间戳。第一段原样转（先读到第一个视频
  标签，确认是 H.264 才开始转；不是就报错，页面显示退路）
- 斗鱼的时间戳是主播推流的绝对时间，换一条连接以后接着走，不会归零；新的一段从更早的一个关键帧开始重放一小段。
  所以新的一段：文件头、说明信息、和原来一样的初始化包都丢掉，时间戳不比已经转过的大的也丢掉，剩下的直接接上
- **直接接下一帧，不等关键帧**（和 DTV 不一样）：实测同一个房间先后开的两条连接，重叠的部分每个标签的内容一模一样，
  接上去只差一帧（40 毫秒）；DTV 的做法是等下一个关键帧，会丢约 20 帧、空 0.8 秒
- 万一新的一段时间戳对不上（和已经转到的地方差了 1 分钟以上，比如归零了）：从它的第一个关键帧开始接，
  把这个关键帧放在已经转到的地方后面一帧（40 毫秒），它后面的时间戳都跟着平移
- 地址有期限（ttl）：到期前 lead 秒在后台先连好新的，连上就马上换，不等旧的被断开。
  没有期限、但是连接断了（网络抖了一下、服务器断开）：马上重连，最多试 retries 次，都不行就结束（页面显示"直播断了"）

这个文件只管 FLV 和接续，不知道是哪个平台：取地址、连接由调用的人给（open_segment）。
"""

import threading
import time
from collections import namedtuple

AUDIO, VIDEO, SCRIPT = 8, 9, 18
Tag = namedtuple("Tag", "kind ts key seq raw")  # raw：整个标签（11 字节的头 + 内容 + 4 字节的"上一个标签多长"）
CHUNK = 32 * 1024
MAX_TAG = 8 << 20          # 一个标签不会这么大：超过就说明不是 FLV
START_LIMIT = 2 << 20      # 开头读了这么多还没有视频标签：不播
JUMP_MS = 60_000           # 新的一段和已经转到的地方差这么多毫秒以上：当作时间戳对不上
FRAME_MS = 40              # 时间戳对不上时，新的一段接在已经转到的地方后面这么多毫秒


class RelayError(Exception):
    """中转不了。code："not_flv" 不是 FLV / "not_h264" 不是 H.264（窗口里放不了）/ "no_video" 没有画面 /
    "upstream" 平台那边连不上或没给数据。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def video_codec(tag):
    """视频标签的编码："h264" / "h265" / "other"。老式写法第一个字节的低 4 位是编号（7 = H.264，12 = H.265）；
    新式写法（最高位是 1）后面 4 个字符是名字（hvc1 = H.265）。"""
    first = tag.raw[11]
    if first & 0x80:
        return "h265" if tag.raw[12:16] == b"hvc1" else "other"
    return {7: "h264", 12: "h265"}.get(first & 0x0F, "other")


def retime(raw, ts):
    """把一个标签的时间戳改成 ts（毫秒）。"""
    ts = max(0, min(int(ts), 0xFFFFFFFF))
    return raw[:4] + bytes(((ts >> 16) & 255, (ts >> 8) & 255, ts & 255, (ts >> 24) & 255)) + raw[8:]


class Demuxer:
    """把收到的字节拆成标签。feed() 每次给一块，返回拆出来的完整标签；header 是文件头（读到之前是 None）。"""

    def __init__(self):
        self.buffer = bytearray()
        self.header = None

    def feed(self, chunk):
        self.buffer += chunk
        if self.header is None:
            if len(self.buffer) < 9:
                return []
            if self.buffer[:3] != b"FLV":
                raise RelayError("not_flv", "平台给的不是 FLV 视频")
            size = max(9, int.from_bytes(self.buffer[5:9], "big")) + 4
            if len(self.buffer) < size:
                return []
            self.header = bytes(self.buffer[:size])
            del self.buffer[:size]
        tags, start = [], 0
        while len(self.buffer) - start >= 11:
            kind = self.buffer[start] & 0x1F
            size = int.from_bytes(self.buffer[start + 1:start + 4], "big")
            if kind not in (AUDIO, VIDEO, SCRIPT) or size > MAX_TAG:
                raise RelayError("not_flv", "平台给的视频数据读不懂")
            total = 11 + size + 4
            if len(self.buffer) - start < total:
                break
            raw = bytes(self.buffer[start:start + total])
            ts = int.from_bytes(raw[4:7], "big") | (raw[7] << 24)
            key = seq = False
            if kind == VIDEO and size >= 2:
                key = (raw[11] >> 4) & 0x07 == 1
                seq = raw[12] == 0 if not raw[11] & 0x80 else raw[11] & 0x0F == 0
            elif kind == AUDIO and size >= 2:
                seq = raw[11] >> 4 == 10 and raw[12] == 0  # AAC 的初始化包
            tags.append(Tag(kind, ts, key, seq, raw))
            start += total
        del self.buffer[:start]
        return tags


class Relay:
    """一路中转。open_segment() → (能 read() 和 close() 的连接, 这个地址多少秒以后被断开或 None)，连不上抛异常。
    用法：relay.start()（连第一段、确认是 H.264，出错抛 RelayError），然后把 relay.stream() 产出的字节发给播放器。
    clock、sleep、spawn 是测试时替换用的（spawn(fn)：在后台运行 fn，默认开一个线程）。"""

    def __init__(self, open_segment, lead=5, retries=5, retry_delay=0.5, clock=time.monotonic, sleep=time.sleep,
                 spawn=None, on_event=None):
        self.open_segment, self.lead, self.retries, self.retry_delay = open_segment, lead, retries, retry_delay
        self.clock, self.sleep = clock, sleep
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, name="relay-next", daemon=True).start())
        self.on_event = on_event or (lambda _text: None)
        self.closed = False
        self.segments = 0           # 接过几段（第一段也算）
        self._body = self._ttl = self._opened = None
        self._demux = Demuxer()
        self._pending = b""
        self._next = None           # 后台正在连的下一段：{"done", "body", "ttl"}
        self._next_after = 0.0      # 后台连下一段失败以后，过了这个时刻再试
        self._last = {AUDIO: None, VIDEO: None}   # 已经转到的时间戳
        self._seq = {}              # 转过的初始化包（kind → 内容）
        self._continuation = False  # 现在是不是接续的一段
        self._aligning = False      # 接续的一段还没看到第一个画面或声音：要看时间戳接不接得上
        self._need_key = False      # 时间戳对不上：等它的第一个关键帧，并把它平移到接着已经转到的地方
        self._offset = 0
        self._forwarded = 0         # 这一段转了几个标签

    # ---------- 连接 ----------

    def _open(self):
        try:
            return self.open_segment()
        except RelayError:
            raise
        except Exception as e:  # noqa: BLE001  取地址、连接出的各种错都算"平台那边连不上"
            raise RelayError("upstream", str(e)) from e

    def _use(self, body, ttl):
        self._body, self._ttl, self._opened = body, ttl, self.clock()
        self._demux = Demuxer()
        self._forwarded = 0
        self.segments += 1

    def _switch(self, body, ttl, why):
        self._close_body(self._body)
        self._use(body, ttl)
        self._continuation, self._aligning, self._need_key = True, True, False
        self.on_event(f"换到第 {self.segments} 段（{why}）")

    @staticmethod
    def _close_body(body):
        try:
            if body is not None:
                body.close()
        except Exception:  # noqa: BLE001
            pass

    def close(self):
        """停下（页面换了选手、关了直播、关了窗口）。可以从别的线程调用。"""
        self.closed = True
        self._close_body(self._body)
        waiting = self._next
        if waiting and waiting.get("body") is not None:
            self._close_body(waiting["body"])

    # ---------- 标签 ----------

    def _process(self, tags):
        out = []
        for tag in tags:
            if tag.kind == SCRIPT:
                if not self._continuation:
                    out.append(tag.raw)  # 接续的一段的说明信息丢掉：播放器已经有了
                continue
            if tag.seq:
                payload = tag.raw[11:-4]
                if self._seq.get(tag.kind) != payload:  # 和原来一样的初始化包丢掉；不一样（主播换了设置）要转
                    self._seq[tag.kind] = payload
                    out.append(tag.raw)
                continue
            marks = [ts for ts in self._last.values() if ts is not None]
            if self._aligning:
                self._aligning = False
                if marks and abs(tag.ts + self._offset - max(marks)) > JUMP_MS:
                    self._need_key = True
                    self.on_event("新的一段时间戳对不上，从关键帧接、平移时间戳")
            if self._need_key:
                if not (tag.kind == VIDEO and tag.key):
                    continue
                self._need_key = False
                self._offset = max(marks) + FRAME_MS - tag.ts
            ts = tag.ts + self._offset
            last = self._last[tag.kind]
            if last is not None and ts <= last:
                continue  # 新的一段重放的、已经转过的部分
            self._last[tag.kind] = ts
            self._forwarded += 1
            out.append(tag.raw if not self._offset else retime(tag.raw, ts))
        return b"".join(out)

    # ---------- 开始 ----------

    def start(self):
        """连第一段，读到第一个视频标签：是 H.264 才行。出错抛 RelayError（这时还什么都没发给播放器）。"""
        self._use(*self._open())
        pending, header_sent = bytearray(), False
        try:
            while True:
                chunk = self._body.read(CHUNK)
                if not chunk:
                    raise RelayError("upstream", "视频服务器没给数据")
                tags = self._demux.feed(chunk)
                if not header_sent and self._demux.header is not None:
                    pending += self._demux.header
                    header_sent = True
                video = next((tag for tag in tags if tag.kind == VIDEO), None)
                if video is not None and video_codec(video) != "h264":
                    raise RelayError("not_h264", "这个直播间给的不是 H.264")
                pending += self._process(tags)
                if video is not None:
                    break
                if len(pending) + len(self._demux.buffer) > START_LIMIT:
                    raise RelayError("no_video", "视频数据里没有画面")
        except RelayError:
            self.close()
            raise
        except (OSError, ValueError) as e:
            self.close()
            raise RelayError("upstream", f"视频服务器断开了（{e}）") from e
        self._pending = bytes(pending)
        return self

    # ---------- 一直转 ----------

    def _prefetch(self):
        """地址快到期了：在后台先连好下一段。"""
        if self._next is not None or self._ttl is None or self.clock() < self._next_after:
            return
        if self.clock() - self._opened < self._ttl - self.lead:
            return
        waiting = self._next = {"done": False, "body": None, "ttl": None}

        def connect():
            try:
                waiting["body"], waiting["ttl"] = self._open()
            except RelayError:
                pass
            waiting["done"] = True
            if self.closed:
                self._close_body(waiting["body"])
        self.spawn(connect)

    def _take_next(self):
        """后台连的那一段好了没有：好了返回 (连接, ttl) 并清掉；没连上就过一会儿再试。"""
        waiting = self._next
        if not waiting or not waiting["done"]:
            return None
        self._next = None
        if waiting["body"] is None:
            self._next_after = self.clock() + max(1.0, self.retry_delay * 2)
            return None
        return waiting["body"], waiting["ttl"]

    def _recover(self):
        """这一段断了：用后台连好的那一段，或者现在重连。接不上返回 False。"""
        waited = 0.0
        while self._next is not None and not self._next["done"] and waited < 20 and not self.closed:
            self.sleep(0.05)
            waited += 0.05
        ready = self._take_next()
        if ready:
            self._switch(*ready, "这一段断了，接上预先连好的")
            return True
        for attempt in range(self.retries):
            if self.closed:
                return False
            try:
                self._switch(*self._open(), "这一段断了，重连")
                return True
            except RelayError as e:
                self.on_event(f"重连没成（第 {attempt + 1} 次）：{e}")
                self.sleep(self.retry_delay)
        return False

    def stream(self):
        """产出要发给播放器的字节，直到停下或者接不上。"""
        empty = 0  # 连着几段什么都没转出来就断了（平台那边一连上就断）：算接不上
        try:
            if self._pending:
                yield self._pending
                self._pending = b""
            while not self.closed:
                self._prefetch()
                ready = self._take_next()
                if ready:
                    self._switch(*ready, "地址快到期，换新的")
                    continue
                try:
                    chunk = self._body.read(CHUNK)
                    out = self._process(self._demux.feed(chunk)) if chunk else b""
                except (OSError, ValueError, RelayError) as e:
                    self.on_event(f"这一段出错了：{e}")
                    chunk, out = b"", b""
                if self.closed:
                    break
                if chunk:
                    if out:
                        yield out
                    continue
                empty = empty + 1 if self._forwarded == 0 else 0
                if empty >= self.retries or not self._recover():
                    self.on_event("接不上了，结束")
                    break
        finally:
            self.close()
