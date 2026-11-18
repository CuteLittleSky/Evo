"""HLS 中转：把平台的 HLS 直播（一份不断更新的播放列表 + 一段一段的 TS 视频）接成一条连着的 MPEG-TS 流，
转给页面里的播放器（mpegts.js）。看直播页播 B站用（watch_bilibili.py；面板的 /esports/stream）。

为什么 B站不让页面直接连（2026-11-19）：0.6.0 是页面直接连 B站的 FLV 地址，在瑞士测是好的，国内的玩家登录以后却播不成、
悄悄退回了官方播放器（最高 720p）。参考项目 DTV 记着"国内 FLV 直连 404，HLS 直连 200"；有的服务器还要求请求带来源页。
改成 Evo 的后台去取（带上 B站自己网页那样的来源页）、再转给页面：和斗鱼一样只是经过，不存盘、不转推。

怎么接：
- 取播放列表（m3u8）。给的是"多档的目录"就跟到第一档；只收 TS 的（带 #EXT-X-MAP 的是 fMP4，不要）
- 从最新的 back 段开始（默认 3 段，约 9 秒），先确认第一段是 TS、里面的画面是 H.264（看节目表 PMT），不是就报错、什么都不发。
  垫 9 秒是实测定的（2026-11-19）：B站一段约 3 秒，新的一段隔 3–4 秒才出现、有时两段一起到
- 之后每 poll 秒看一次播放列表，新出现的段**马上开始下载、几段同时下**（parallel），按顺序原样发出去
  （TS 的时间戳本来就是连着的，不用改）。认"新"看的是段的路径，不看序号：B站的播放列表序号一直是 1。
  同时下是实测定的（2026-11-19，瑞士）：到 B站服务器的一条连接只有约 2.3 Mbps，原画约 3 Mbps，一段接一段地下会越落越远
  （90 秒只播了 70 秒）；三段同时下合计 5.5 Mbps。国内离得近，一条连接就够，同时下也没有坏处
- 播放列表取不到（地址过期、服务器出错）：重新要一次地址再试，连着 retries 次不行就结束；
  stale 秒没有新的段（主播下播了）、播放列表写了结束：把手上的发完就结束。外面叫停（换人、关页面）也结束
- 播放列表里的地址只跟到和播放列表同一个站的 https 地址：平台的服务器被人动了手脚，也不能让 Evo 去连别的地方
"""

import threading
import time
import urllib.parse
from collections import deque

from flv_relay import RelayError

MIMETYPE = "video/mp2t"
PACKET = 188          # TS 一个包的长度，每个包以 0x47 开头
H264, HEVC = 0x1B, 0x24  # 节目表里画面的编码编号
MAX_SEEN = 400        # 记着最近转过的这么多段（认"新"用）


def same_site(url, base):
    """url 是不是和 base 同一个站的 https 地址（主机名最后两段一样，如 xxx.bilivideo.com）。"""
    a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
    return (a.scheme == "https" and b.scheme == "https" and bool(a.hostname) and bool(b.hostname)
            and a.hostname.split(".")[-2:] == b.hostname.split(".")[-2:])


def parse_playlist(text, base):
    """播放列表 → {"variants": [各档的播放列表地址], "segments": [(路径, 地址), ...], "ended": 写没写结束}。
    不是播放列表、是 fMP4 的抛 RelayError。别的站的地址不要。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise RelayError("upstream", "平台给的不是 HLS 播放列表")
    variants, segments, ended, variant_next = [], [], False, False
    for line in lines[1:]:
        if line.startswith("#EXT-X-MAP"):
            raise RelayError("not_ts", "平台给的是 fMP4 的 HLS，Evo 只接 TS 的")
        if line.startswith("#EXT-X-STREAM-INF"):
            variant_next = True
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif not line.startswith("#"):
            url = urllib.parse.urljoin(base, line)
            if same_site(url, base):
                if variant_next:
                    variants.append(url)
                else:
                    segments.append((urllib.parse.urlsplit(url).path, url))
            variant_next = False
    return {"variants": variants, "segments": segments, "ended": ended}


def video_types(data):
    """TS 开头的节目表里各路的编码编号（0x1B = H.264，0x24 = H.265，0x0F = AAC 声音……）；读不出来返回 None。"""
    pmt_pid = None
    for off in range(0, len(data) - PACKET + 1, PACKET):
        if data[off] != 0x47:
            return None
        pid = ((data[off + 1] & 0x1F) << 8) | data[off + 2]
        if pid != 0 and pid != pmt_pid:
            continue
        if not data[off + 1] & 0x40 or not (data[off + 3] >> 4) & 1:  # 不是一张表的开头，或者这个包没有内容
            continue
        end = off + PACKET
        pos = off + 4
        if (data[off + 3] >> 4) & 2:  # 前面有一段附加信息：跳过
            pos += 1 + data[pos]
        if pos >= end:
            continue
        pos += 1 + data[pos]  # 表从这里开始
        if pos + 12 > end:
            continue
        stop = min(end, pos + 3 + (((data[pos + 1] & 0x0F) << 8) | data[pos + 2]) - 4)  # 表的最后 4 个字节是校验
        if pid == 0:  # 节目总表：找到节目表在哪一路
            at = pos + 8
            while at + 4 <= stop:
                if (data[at] << 8) | data[at + 1]:
                    pmt_pid = ((data[at + 2] & 0x1F) << 8) | data[at + 3]
                    break
                at += 4
        else:  # 节目表
            at = pos + 12 + (((data[pos + 10] & 0x0F) << 8) | data[pos + 11])
            types = []
            while at + 5 <= stop:
                types.append(data[at])
                at += 5 + (((data[at + 3] & 0x0F) << 8) | data[at + 4])
            return types
    return None


class HlsRelay:
    """一路 HLS 中转。open_playlist() → [播放列表地址, ...]（平台给的几个服务器，按顺序试；地址不行了会再叫一次），
    fetch(地址) → 字节（连不上、不是 200 抛 OSError）。
    用法和 flv_relay.Relay 一样：relay.start()（出错抛 RelayError），然后把 relay.stream() 产出的字节发给播放器。
    clock、sleep、spawn 是测试时替换用的（spawn(fn)：在后台运行 fn，默认开一个线程）。"""
    mimetype = MIMETYPE

    def __init__(self, open_playlist, fetch, back=3, poll=0.5, parallel=3, retries=5, stale=30.0, tick=0.1, first_wait=30.0,
                 clock=time.monotonic, sleep=time.sleep, spawn=None, on_event=None):
        self.open_playlist, self.fetch = open_playlist, fetch
        self.back, self.poll, self.parallel, self.retries = max(1, back), poll, max(1, parallel), retries
        self.stale, self.tick, self.first_wait = stale, tick, first_wait
        self.clock, self.sleep = clock, sleep
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, name="hls-segment", daemon=True).start())
        self.on_event = on_event or (lambda _text: None)
        self.closed = False
        self.segments = 0            # 转了几段
        self._urls = []              # 现在用的播放列表地址（能用的那个排在最前）
        self._seen = deque(maxlen=MAX_SEEN)
        self._queue = []             # 还没开始下载的段
        self._jobs = deque()         # 下载中、下载好还没发的段，按顺序：{"url", "done", "data", "error"}
        self._first = b""
        self._ended = False

    def close(self):
        """停下（页面换了选手、关了直播、关了窗口）。可以从别的线程调用。"""
        self.closed = True

    # ---------- 取 ----------

    def _resolve(self):
        try:
            urls = [url for url in self.open_playlist() if isinstance(url, str) and url.startswith("https://")]
        except RelayError:
            raise
        except Exception as e:  # noqa: BLE001  取地址出的各种错都算"平台那边连不上"
            raise RelayError("upstream", str(e)) from e
        if not urls:
            raise RelayError("upstream", "平台没给 HLS 的地址")
        self._urls = urls

    def _load(self):
        """取现在的播放列表（几个服务器按顺序试，给的是多档的目录就跟到第一档）；都不行抛 RelayError。"""
        problem = None
        for index, url in enumerate(list(self._urls)):
            try:
                found = parse_playlist(self.fetch(url).decode("utf-8", "replace"), url)
                if found["variants"]:
                    url = found["variants"][0]
                    found = parse_playlist(self.fetch(url).decode("utf-8", "replace"), url)
                self._urls = [url, *self._urls[:index], *self._urls[index + 1:]]  # 能用的这个下次先试
                return found
            except RelayError as e:
                if e.code == "not_ts":
                    raise
                problem = e
            except OSError as e:
                problem = e
        raise RelayError("upstream", f"取不到播放列表（{problem}）")

    def _take(self, found):
        """播放列表里没转过的段排进队里；返回多了几段。"""
        new = [segment for segment in found["segments"] if segment[0] not in self._seen]
        for segment in new:
            self._seen.append(segment[0])
        self._queue.extend(new)
        self._ended = found["ended"]
        return len(new)

    def _download(self, job):
        try:
            job["data"] = self.fetch(job["url"])
        except Exception as e:  # noqa: BLE001  连不上、不是 200，或者别的错：都只算这一段没取到
            job["error"] = e
        job["done"] = True

    def _launch(self):
        """队里的段开始下载：同时最多 parallel 段。"""
        while self._queue and sum(not job["done"] for job in self._jobs) < self.parallel:
            job = {"url": self._queue.pop(0)[1], "done": False, "data": None, "error": None}
            self._jobs.append(job)
            self.spawn(lambda job=job: self._download(job))

    def _fail(self, code, message):
        self.closed = True
        return RelayError(code, message)

    def start(self):
        """取地址、取播放列表、下载第一段并确认是 H.264 的 TS。出错抛 RelayError，什么都不发。"""
        self._resolve()
        found = self._load()
        if not found["segments"]:
            raise self._fail("upstream", "播放列表里还没有视频")
        for segment in found["segments"][:-self.back]:  # 太早的不要：从离直播最近的几段开始
            self._seen.append(segment[0])
        self._take(found)
        self._launch()  # 开头的几段一起下，先等第一段
        first, began = self._jobs.popleft(), self.clock()
        while not first["done"]:
            if self.closed or self.clock() - began > self.first_wait:
                raise self._fail("upstream", "连不上平台的视频服务器（等第一段等太久）")
            self.sleep(self.tick)
        data = first["data"]
        if data is None:
            raise self._fail("upstream", f"连不上平台的视频服务器（{first['error']}）")
        if len(data) < PACKET or data[0] != 0x47:
            raise self._fail("not_ts", "平台给的不是 TS 视频")
        types = video_types(data[:PACKET * 400])
        if types is not None and H264 not in types:
            raise self._fail("not_h264", "不是 H.264 的画面" + ("（是 H.265）" if HEVC in types else ""))
        self._first = data
        self.segments = 1

    def stream(self):
        """产出要发给播放器的字节，直到直播结束、接不上了或被叫停。"""
        try:
            if self._first:
                yield self._first
                self._first = b""
            list_failures = segment_failures = 0
            next_poll, quiet_since = 0.0, self.clock()
            while not self.closed:
                now = self.clock()
                if not self._ended and now >= next_poll:  # 看一眼播放列表有没有新的段
                    next_poll = now + self.poll
                    try:
                        more = self._take(self._load())
                    except RelayError as e:
                        list_failures += 1
                        if list_failures > self.retries:
                            self.on_event(f"接不上了，结束（{e}）")
                            self._ended = True
                        else:
                            self.on_event(f"播放列表没取到，重新要地址（第 {list_failures} 次）：{e}")
                            try:
                                self._resolve()
                            except RelayError as again:
                                self.on_event(f"重新要地址没成：{again}")
                    else:
                        list_failures = 0
                        if more:
                            quiet_since = now
                        elif now - quiet_since > self.stale:
                            self.on_event(f"{int(self.stale)} 秒没有新的视频（多半是下播了），结束")
                            self._ended = True
                self._launch()
                if self._jobs and self._jobs[0]["done"]:  # 最前面的那一段下好了：发出去
                    job = self._jobs.popleft()
                    if job["data"] is None:  # 这一段没取到：跳过它（画面会卡一下），连着几段都不行就结束
                        segment_failures += 1
                        self.on_event(f"有一段没取到（{job['error']}）")
                        if segment_failures > self.retries:
                            self.on_event("接不上了，结束")
                            return
                        continue
                    segment_failures = 0
                    self.segments += 1
                    yield job["data"]
                    continue
                if self._ended and not self._jobs and not self._queue:
                    return
                self.sleep(self.tick)
        finally:
            self.closed = True
