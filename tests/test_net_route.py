"""联网出错的原因、国内备用线路的入口（net_route.py，2026-11-19）。不联网：备用线路用假的冒充，"正式地址"换成本机没有服务器的端口。

- 原因：连接被重置、超时、查不到地址、证书不对、服务器回的 HTTP 代码都写得出来；几个地址各写一段（COS 写"COS"）
- 4xx 不算"连不上"（换条线路也一样）；5xx、重置、超时算
- 没有备用线路（GitHub 上的 Evo 没有 cloud_relay.py）：只连 evotracker.cn，和以前一样
- 地址换成了测试地址（不是正式地址）：绝不走备用线路
- 公告、赛程、地图轮换、全体玩家：正式地址连不上时用备用线路取回来的，照样过各自的检查；两边都不行时写明两边的原因
- 走备用线路时省调用次数：公告 3 小时内不再读、赛程 15 分钟内不重下；地图轮换按备用线路给的"旧了多久"校准时间

运行：python tests/test_net_route.py
"""

import json
import os
import socket
import ssl
import time
import urllib.error

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import app_settings
import esports_data
import global_stats
import map_rotation
import net_route
import notice_check
import telemetry

os.environ["no_proxy"] = "127.0.0.1,localhost"  # 电脑上设了代理时，本机的地址也直接连
DEAD = "http://127.0.0.1:9"  # 没有服务器的端口：连上去马上被拒绝


class FakeRelay:
    """冒充 cloud_relay：照它的规矩先 primary()，连不上再"走备用线路"（给 files 里的内容，或者说也不行）。"""

    def __init__(self, files=None, age=0, fail=None):
        self.files, self.age, self.fail, self.asked = files or {}, age, fail, []

    def get(self, name, primary):
        try:
            return primary(), net_route.DIRECT, 0
        except net_route.Unreachable as e:
            self.asked.append(name)
            if self.fail:
                raise net_route.Unreachable(f"{e}；备用线路：{self.fail}") from e
            return self.files[name], net_route.RELAY, self.age


def http_error(code):
    return urllib.error.HTTPError("https://dl.evotracker.cn/x", code, "x", {}, None)


def reason_checks(out):
    cert = ssl.SSLCertVerificationError(1, "certificate verify failed")
    cert.verify_message = "EE certificate key too weak"
    d = net_route.describe
    out["原因：重置、中止、拒绝、超时、DNS、证书、HTTP 代码各说各的"] = (
        d(ConnectionResetError(10054, "x")) == "连接被重置" and d(ConnectionAbortedError()) == "连接被中止"
        and d(ConnectionRefusedError()) == "连接被拒绝" and d(socket.timeout("timed out")) == "超时"
        and d(TimeoutError()) == "超时" and d(socket.gaierror(11001, "getaddrinfo failed")) == "查不到服务器的地址（DNS）"
        and d(cert) == "证书不对（EE certificate key too weak）" and d(http_error(451)) == "服务器回了 HTTP 451")
    out["urllib 包了一层的（URLError）也认得出里面的原因；认不出的照原话"] = (
        d(urllib.error.URLError(ConnectionResetError())) == "连接被重置"
        and d(urllib.error.URLError("_ssl.c:1015: The handshake operation timed out")) == "超时"
        and d(OSError("别的原因")) == "别的原因")
    out["地址的简称：COS 写\"COS\"，别的写主机名"] = (
        net_route.labeled("https://evo-dl-1.cos.ap-shanghai.myqcloud.com/notice.json", http_error(451))
        == "COS：服务器回了 HTTP 451"
        and net_route.labeled("https://dl.evotracker.cn/notice.json", ConnectionResetError()) == "dl.evotracker.cn：连接被重置")
    u = net_route.unreachable
    out["4xx 不算连不上（换条线路也一样）；5xx、重置算"] = (
        u("https://x.cn/a", http_error(404)) is None and isinstance(u("https://x.cn/a", http_error(503)), net_route.Unreachable)
        and str(u("https://x.cn/a", ConnectionResetError())) == "x.cn：连接被重置")


def entry_checks(out):
    real = net_route._relay
    try:
        net_route._relay = lambda: None
        out["没有备用线路（GitHub 上的 Evo）：只连 evotracker.cn"] = (
            net_route.get("notice", lambda: b"x") == (b"x", net_route.DIRECT, 0))

        def must_not():
            raise AssertionError("不是正式地址，不该找备用线路")

        net_route._relay = must_not
        try:
            net_route.get("notice", lambda: (_ for _ in ()).throw(net_route.Unreachable("127.0.0.1：连接被拒绝")),
                          official=False)
            unofficial = False
        except net_route.Unreachable:
            unofficial = True
        out["不是正式地址（测试、预览）：连不上就是连不上，绝不走备用线路"] = unofficial
    finally:
        net_route._relay = real


def notice_checks(out):
    urls, default = notice_check.URLS, notice_check.DEFAULT_URL
    notice_check.URLS = [f"{DEAD}/cos/notice.json", f"{DEAD}/notice.json"]
    notice_check.DEFAULT_URL = notice_check.URLS[-1]  # 把"正式地址"换成连不上的本机地址
    app_settings.set_value("update_auto", True)
    app_settings.set_value(notice_check.STATE_KEY, None)
    body = json.dumps({"notices": [{"id": "n1", "level": "info", "text": "经备用线路读到的公告"}]}).encode("utf-8")
    relay = FakeRelay({"notice": body})
    real = net_route._relay
    net_route._relay = lambda: relay
    try:
        first = notice_check.check()
        out["公告：两个地址都连不上 → 用备用线路读到的（照样检查格式）"] = (
            first["ok"] and first["via"] == net_route.RELAY and [n["id"] for n in first["notices"]] == ["n1"]
            and [n["id"] for n in notice_check.visible()] == ["n1"])
        again = notice_check.check()
        out["公告：走备用线路读到的，3 小时内不再读（不联网、不写日志）"] = again is None and relay.asked == ["notice"]
        state = app_settings.get(notice_check.STATE_KEY)
        state["checked"] -= notice_check.RELAY_EVERY + 1
        app_settings.set_value(notice_check.STATE_KEY, state)
        relay.fail = "连不上云端（超时）"
        failed = notice_check.check()
        out["公告：过了 3 小时再读；两边都不行时写明每个地址和备用线路各自的原因，留着上次的"] = (
            not failed["ok"] and "COS" not in failed["error"] and failed["error"].count("127.0.0.1：连接被拒绝") == 2
            and "备用线路：连不上云端（超时）" in failed["error"] and [n["id"] for n in notice_check.visible()] == ["n1"])
        relay.fail = None
        relay.files["notice"] = b"<html>blocked</html>"
        junk = notice_check.check()
        out["公告：备用线路给的格式不对也不要"] = not junk["ok"] and "格式不对" in junk["error"]
    finally:
        net_route._relay = real
        notice_check.URLS, notice_check.DEFAULT_URL = urls, default
        for key in ("update_auto", notice_check.STATE_KEY):
            app_settings.set_value(key, None)


def esports_checks(out):
    url, default = esports_data.URL, esports_data.DEFAULT_URL
    esports_data.URL = esports_data.DEFAULT_URL = f"{DEAD}/esports.json"
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    good = {"version": 1, "updated": "2026-11-19T20:00:00+08:00",
            "players": [{"id": "douyu-1", "name": "甲", "team": "示例战队", "platform": "douyu", "room": "1"},
                        {"id": "douyu-2", "name": "乙", "team": "示例战队", "platform": "douyu", "room": "不是数字"}],
            "events": []}
    relay = FakeRelay({"esports": json.dumps(good).encode("utf-8")})
    real = net_route._relay
    net_route._relay = lambda: relay
    try:
        got = esports_data.get()
        out["赛程：连不上 → 用备用线路取回来的，照样过 clean()（房间号不对的选手去掉）"] = (
            got["ok"] and [p["id"] for p in got["data"]["players"]] == ["douyu-1"]
            and esports_data.cached()["via"] == net_route.RELAY)
        saved = json.loads(esports_data.CACHE_PATH.read_text(encoding="utf-8"))
        saved["fetched_at"] -= 300  # 5 分钟前（看直播页开着时每 5 分钟重下一次）
        esports_data.CACHE_PATH.write_text(json.dumps(saved), encoding="utf-8")
        esports_data.get(fresh=300)
        out["赛程：走备用线路取的，15 分钟内不重下"] = relay.asked == ["esports"]
        esports_data.get(force=True)
        out["赛程：点刷新照样重下"] = relay.asked == ["esports", "esports"]
        relay.files["esports"] = json.dumps({**good, "version": 2}).encode("utf-8")
        newer = esports_data.get(force=True)
        out["赛程：备用线路给的格式不对：说没读到，用上次存下的"] = (
            not newer["ok"] and "格式不对" in newer["error"] and [p["id"] for p in newer["data"]["players"]] == ["douyu-1"])
        relay.fail = "连不上云端（超时）"
        both = esports_data.get(force=True)
        out["赛程：两边都不行：写明两边的原因"] = (
            not both["ok"] and "127.0.0.1：连接被拒绝" in both["error"] and "备用线路" in both["error"])
        esports_data.URL = f"{DEAD}/esports_test.json"  # 换成了测试地址
        relay.asked.clear()
        esports_data.get(force=True)
        out["赛程：地址换成了测试地址（EVO_ESPORTS_URL）：不走备用线路"] = relay.asked == []
    finally:
        net_route._relay = real
        esports_data.URL, esports_data.DEFAULT_URL = url, default
        esports_data.CACHE_PATH.unlink(missing_ok=True)


def stats_checks(out):
    url, default = telemetry.STATS_URL, telemetry.DEFAULT_STATS_URL
    telemetry.STATS_URL = telemetry.DEFAULT_STATS_URL = DEAD
    server_now = int(time.time()) + 120  # 服务器比电脑快 2 分钟
    modes = {"pubs": {"current": {"map": "Storm Point", "code": "storm_point_rotation", "event": None,
                                  "start": server_now - 600, "end": server_now + 3000}, "next": None}}
    stored = json.dumps({"v": 1, "now": server_now - 300, "modes": modes}).encode("utf-8")  # 备用线路存的是 5 分钟前的
    relay = FakeRelay({"maps": stored, "summary": b'{"not": "a summary"}'}, age=300)
    real = net_route._relay
    net_route._relay = lambda: relay
    app_settings.set_value(map_rotation.SETTING_KEY, True)
    app_settings.set_value(telemetry.CONSENT_KEY, True)
    try:
        maps = map_rotation.fetch()
        offset = json.loads(map_rotation.CACHE_PATH.read_text(encoding="utf-8"))["offset"]
        out["地图轮换：连不上 → 用备用线路的；按它旧了多久校准时间（不会差 5 分钟）"] = (
            maps["ok"] and relay.asked == ["maps"] and abs(offset - 120) <= 10)  # Windows 上连没开的端口要等约 2 秒
        summary = global_stats.fetch()
        out["全体玩家：备用线路给的照样检查格式"] = (
            not summary["ok"] and "格式不对" in summary["error"] and relay.asked == ["maps", "summary"])
        relay.fail = "连不上云端（连接被重置）"
        both = map_rotation.fetch()
        stats = global_stats.fetch()
        out["地图轮换、全体玩家：两边都不行时写明两边的原因"] = (
            "127.0.0.1：连接被拒绝" in both["error"] and "备用线路：连不上云端（连接被重置）" in both["error"]
            and stats["error"].startswith("连不上统计服务器（127.0.0.1：连接被拒绝；备用线路"))
    finally:
        net_route._relay = real
        telemetry.STATS_URL, telemetry.DEFAULT_STATS_URL = url, default
        map_rotation.CACHE_PATH.unlink(missing_ok=True)
        for key in (map_rotation.SETTING_KEY, telemetry.CONSENT_KEY):
            app_settings.set_value(key, None)


def main():
    print("联网出错的原因、国内备用线路的入口（net_route.py）")
    out = {}
    reason_checks(out)
    entry_checks(out)
    notice_checks(out)
    esports_checks(out)
    stats_checks(out)
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
