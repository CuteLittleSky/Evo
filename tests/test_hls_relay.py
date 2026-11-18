"""HLS 中转（hls_relay.py：看直播页播 B站时，把 HLS 的播放列表和一段一段的 TS 接成一条流）。
不联网：播放列表和视频段都是这里拼出来的。

- 读播放列表：段的地址（相对的、带参数的）、多档的目录、写了结束；别的站的地址不要；fMP4 的、不是播放列表的报错
- 认编码：TS 开头的节目表里是 H.264 还是 H.265
- 中转：从最新的几段开始、之后新出现的段按顺序接上，不重复、不缺；不是 TS、不是 H.264、没有视频：一开始就报错
- 几段同时下载：同时最多 parallel 段，后面的先下好也按顺序发
- 出错：第一个服务器不行换下一个；播放列表取不到就重新要地址，连着几次不行才结束；有一段没取到就跳过；
  很久没有新的段（下播了）、播放列表写了结束、外面叫停：结束

运行：python tests/test_hls_relay.py
"""

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import hls_relay
from flv_relay import RelayError

BASE = "https://d1.bilivideo.com/live-bvc/1/live_x.m3u8?expires=1&sign=aaa"
OTHER = "https://d1b.bilivideo.com/live-bvc/1/live_x.m3u8?expires=1&sign=bbb"


def packet(pid, payload, start=True):
    head = bytes((0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, 0x10))
    return (head + payload + b"\xff" * 188)[:188]


def segment(number, video=0x1B, size=6):
    """假的一段 TS：节目总表 + 节目表（画面的编码是 video、声音是 AAC）+ 几个带着段号的包。"""
    pat = b"\x00\x00\xb0\x0d\x00\x01\xc1\x00\x00" + b"\x00\x01\xe1\x00" + b"\x00\x00\x00\x00"
    streams = bytes((video, 0xE1, 0x01, 0xF0, 0x00, 0x0F, 0xE1, 0x02, 0xF0, 0x00))
    pmt = b"\x00\x02\xb0" + bytes((9 + len(streams) + 4,)) + b"\x00\x01\xc1\x00\x00\xe1\x01\xf0\x00" + streams + b"\x00\x00\x00\x00"
    return packet(0, pat) + packet(0x100, pmt) + b"".join(packet(0x101, bytes((0, number & 0xFF, n)), False) for n in range(size))


def playlist(numbers, ended=False, prefix="live_x/"):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:3", "#EXT-X-MEDIA-SEQUENCE:1"]
    for n in numbers:
        lines += ["#EXTINF:2.987,", f"{prefix}{n}.ts?expires=1&sign=s{n}"]
    return "\n".join(lines + (["#EXT-X-ENDLIST"] if ended else [])) + "\n"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Net:
    """假的平台：lists 是每次取播放列表时依次给的内容（最后一份一直给下去）；记下取过什么。"""

    def __init__(self, lists, broken=(), video=0x1B, bad_segments=()):
        self.lists, self.broken, self.video, self.bad_segments = list(lists), set(broken), video, set(bad_segments)
        self.asked, self.resolved = [], 0

    def open_playlist(self):
        self.resolved += 1
        return [BASE, OTHER]

    def fetch(self, url):
        self.asked.append(url)
        host = url.split("/")[2]
        if host in self.broken:
            raise OSError(f"连不上 {host}")
        if ".m3u8" in url:
            text = self.lists.pop(0) if len(self.lists) > 1 else self.lists[0]
            if isinstance(text, Exception):
                raise text
            return text.encode("utf-8")
        number = int(url.split("/")[-1].split(".")[0])
        if number in self.bad_segments:
            raise OSError("HTTP 404")
        return segment(number, self.video)


def run(net, **more):
    clock = Clock()
    events = []
    relay = hls_relay.HlsRelay(net.open_playlist, net.fetch, clock=clock, sleep=clock.sleep, spawn=lambda fn: fn(),
                               on_event=events.append, **more)
    relay.start()
    return b"".join(relay.stream()), relay, events


def numbers(data):
    """接出来的流里每个视频包是第几段的（每段 6 个包；每段开头那两个表不算）。"""
    out = []
    for off in range(0, len(data), 188):
        pid = ((data[off + 1] & 0x1F) << 8) | data[off + 2]
        if pid == 0x101:
            out.append(data[off + 5])
    return out


def error(net, **more):
    try:
        run(net, **more)
    except RelayError as e:
        return e.code
    return None


def parse_checks(out):
    found = hls_relay.parse_playlist(playlist([7, 8]) + "https://evil.example/x.ts\nhttp://d1.bilivideo.com/plain.ts\n", BASE)
    out["读播放列表：段的地址接在播放列表的地址后面、带着自己的参数；别的站的、不是 https 的不要"] = (
        found["segments"] == [("/live-bvc/1/live_x/7.ts", "https://d1.bilivideo.com/live-bvc/1/live_x/7.ts?expires=1&sign=s7"),
                              ("/live-bvc/1/live_x/8.ts", "https://d1.bilivideo.com/live-bvc/1/live_x/8.ts?expires=1&sign=s8")]
        and found["variants"] == [] and found["ended"] is False)
    master = hls_relay.parse_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://cn-a.bilivideo.com/hi.m3u8?x=1\n"
                                      "#EXT-X-STREAM-INF:BANDWIDTH=2\nhttps://other.example/lo.m3u8\n", BASE)
    out["多档的目录：给出各档的地址（同一个站的）；写了结束的认得出"] = (
        master["variants"] == ["https://cn-a.bilivideo.com/hi.m3u8?x=1"] and master["segments"] == []
        and hls_relay.parse_playlist(playlist([1], ended=True), BASE)["ended"] is True)
    codes = []
    for text in ("<html>", "", "#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:1,\n1.m4s\n"):
        try:
            hls_relay.parse_playlist(text, BASE)
            codes.append(None)
        except RelayError as e:
            codes.append(e.code)
    out["不是播放列表、是 fMP4 的：报错"] = codes == ["upstream", "upstream", "not_ts"]
    out["认编码：节目表里画面是 H.264 还是 H.265；不是 TS 的读不出来"] = (
        hls_relay.video_types(segment(1)) == [0x1B, 0x0F] and hls_relay.video_types(segment(1, 0x24)) == [0x24, 0x0F]
        and hls_relay.video_types(b"FLV" + b"\x00" * 400) is None and hls_relay.video_types(b"") is None)


def relay_checks(out):
    net = Net([playlist([1, 2, 3, 4]), playlist([3, 4, 5]), playlist([3, 4, 5]), playlist([5, 6], ended=True)])
    data, relay, events = run(net)
    out["从最新的 3 段开始，之后新出现的段按顺序接上，不重复、不缺；播放列表写了结束就结束"] = (
        numbers(data) == [n for n in (2, 3, 4, 5, 6) for _ in range(6)] and relay.segments == 5 and relay.closed
        and len(data) == 5 * len(segment(1)) and net.resolved == 1 and relay.mimetype == "video/mp2t")
    out["一开始就确认：不是 H.264、不是 TS、播放列表里没有视频、取不到地址，都报错，不发视频"] = (
        error(Net([playlist([1, 2])], video=0x24)) == "not_h264"
        and error(Net(["#EXTM3U\n#EXT-X-MAP:URI=\"a\"\n"])) == "not_ts" and error(Net([playlist([])])) == "upstream"
        and error(Net([playlist([1])], broken=("d1.bilivideo.com", "d1b.bilivideo.com"))) == "upstream"
        and error(Net([playlist([1, 2])], bad_segments=(1,)), back=2) == "upstream")
    held, order = [], []
    net = Net([playlist([1, 2, 3, 4, 5]), playlist([1, 2, 3, 4, 5], ended=True)])
    clock = Clock()
    relay = hls_relay.HlsRelay(net.open_playlist, net.fetch, back=5, parallel=2, clock=clock, sleep=clock.sleep, spawn=held.append)

    def tick(seconds):  # 等的时候把后台的下载做掉：后开始的先做完
        clock.now += seconds
        order.append(len(held))
        while held:
            held.pop()()

    relay.sleep = tick
    relay.start()
    data = b"".join(relay.stream())
    out["几段同时下载：同时最多 parallel 段；后面的先下好，也按顺序发"] = (
        numbers(data) == [n for n in (1, 2, 3, 4, 5) for _ in range(6)] and max(order) == 2)
    net = Net([playlist([1, 2, 3]), playlist([2, 3, 4], ended=True)], broken=("d1.bilivideo.com",))
    data, relay, events = run(net)
    out["第一个服务器连不上：换下一个，照样接得上"] = (
        numbers(data) == [n for n in (1, 2, 3, 4) for _ in range(6)] and all("d1b." in url for url in net.asked[1:]))
    net = Net([playlist([1, 2]), OSError("HTTP 403"), OSError("HTTP 403"), playlist([2, 3]), playlist([3, 4], ended=True)])
    data, relay, events = run(net)
    out["播放列表取不到（地址过期）：重新要地址，接着播，不重复"] = (
        numbers(data) == [n for n in (1, 2, 3, 4) for _ in range(6)] and net.resolved >= 2
        and any("重新要地址" in text for text in events))
    net = Net([playlist([1, 2]), OSError("HTTP 404")])
    data, relay, events = run(net, retries=3)
    out["一直取不到：试几次就结束，不会没完没了"] = (
        numbers(data) == [n for n in (1, 2) for _ in range(6)] and net.resolved == 4 and "接不上了" in events[-1])
    net = Net([playlist([1, 2, 3]), playlist([3, 4, 5], ended=True)], bad_segments=(4,))
    data, relay, events = run(net)
    out["有一段没取到：跳过它，后面的照常发"] = (
        numbers(data) == [n for n in (1, 2, 3, 5) for _ in range(6)] and any("有一段没取到" in text for text in events))
    net = Net([playlist([1, 2])])
    data, relay, events = run(net, stale=10)
    out["很久没有新的段（主播下播了）：结束"] = (
        numbers(data) == [n for n in (1, 2) for _ in range(6)] and "没有新的视频" in events[-1] and relay.closed)
    net = Net([playlist([1, 2, 3]), playlist([2, 3, 4]), playlist([3, 4, 5]), playlist([4, 5, 6])])
    clock = Clock()
    relay = hls_relay.HlsRelay(net.open_playlist, net.fetch, clock=clock, sleep=clock.sleep, spawn=lambda fn: fn())
    relay.start()
    got = []
    for chunk in relay.stream():
        got.append(chunk)
        if len(got) == 4:
            relay.close()
    out["外面叫停（换人、关页面）：马上停"] = len(got) == 4 and relay.closed
    net = Net(["#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhttps://cn-a.bilivideo.com/hi/index.m3u8?x=1\n", playlist([1, 2], ended=True, prefix="")])
    data, relay, events = run(net)
    out["给的是多档的目录：跟到第一档再接"] = (
        numbers(data) == [n for n in (1, 2) for _ in range(6)] and net.asked[1] == "https://cn-a.bilivideo.com/hi/index.m3u8?x=1"
        and net.asked[2].startswith("https://cn-a.bilivideo.com/hi/1.ts"))


def main():
    print("HLS 中转（读播放列表、认编码、接成一条流、同时下载、出错和结束）")
    out = {}
    parse_checks(out)
    relay_checks(out)
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
