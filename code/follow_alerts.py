"""关注的主播开播通知：谁"刚开播"（Tracker），和托盘里的 Evo 每隔几分钟查一次的那一步（check）。

规则照 PureLive 的"开播提醒"（https://github.com/wzgrx/PureLive ，live_alerts.dart；2026-11-20 用户定的：这类功能优先参照它）：
- 每个直播间记着上次看到的状态。这次没查到（error）不算数，什么都不改
- 第一次看到一个直播间只记下、不提醒（Evo 刚启动、刚关注、刚打开通知：那时已经在播的不是新闻）
- 上次没在播、这次在播 = 刚开播，提醒一次；但同一场不提醒两遍：开播时刻和上次那一场差不到 SAME_START 秒，
  或者下播不到 RECONNECT 秒又回来了（推流断了重连）
- 上次在播、这次也在播，但开播时刻往后挪了 RECONNECT 秒以上 = 两次检查之间下播又开了新的一场，也提醒
和 PureLive 不一样的一处（用户定的："Apex 开着不查也不弹，关了再补"）：打游戏时没有查，游戏关了以后的第一次检查里，
第一次看到的直播间如果是游戏开着的那段时间里开播的（since_after），也提醒。

只在玩家打开了开播通知（follows.notify() 是 True）、关注了人的时候才联网；只查关注的、开直播的那几位（live_status.query）。
这个文件不加载面板和识别用的库。测试 tests/test_follow_alerts.py（tests/test_follow.py 是总览页的"关注我"，不是这个）。
"""

import threading
import time

import esports_data
import follows
import live_status

RECONNECT = 15 * 60   # 秒：下播这么久以内又在播算同一场；开播时刻挪了这么多算新的一场
SAME_START = 2 * 60   # 秒：两次看到的开播时刻差这么少算同一场（平台会取整，有的是用时长倒推的）
DATA_FRESH = 30 * 60  # 秒：名单（esports.json）这么久以内下载过就不重下
wake = threading.Event()  # 关注的名单、通知的开关改了：托盘里等着的那个线程不用等满 5 分钟，马上重新看一遍


class Tracker:
    """记着每个直播间上次看到的状态，告诉你这次谁刚开播。"""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._seen = {}  # id → {"live": 上次在不在播, "since": 那一场的开播时刻或 None, "off_at": 看到它下播的时刻或 None}

    def forget(self):
        self._seen.clear()

    def observe(self, found, since_after=None):
        """found：{id: live_status 的结果}。返回这次刚开播的 id（照 found 的顺序）。
        since_after：第一次看到的直播间，开播时刻在它以后的也算刚开播（打游戏那段时间没查，关了游戏补）。"""
        now, started = self._clock(), []
        for key, got in found.items():
            if got["state"] == live_status.ERROR:
                continue
            live, since, seen = got["state"] == live_status.LIVE, got.get("since"), self._seen.get(key)
            if seen is None:
                if live and since_after is not None and since is not None and since >= since_after:
                    started.append(key)
                self._seen[key] = {"live": live, "since": since if live else None, "off_at": None}
                continue
            if live and not seen["live"]:
                same = seen["since"] is not None and since is not None and abs(since - seen["since"]) < SAME_START
                back = seen["off_at"] is not None and now - seen["off_at"] < RECONNECT and (
                    since is None or seen["since"] is None or since - seen["since"] < RECONNECT)
                if not same and not back:
                    started.append(key)
                self._seen[key] = {"live": True, "since": since if since is not None else seen["since"], "off_at": None}
            elif live:
                if since is not None and seen["since"] is not None and since - seen["since"] > RECONNECT:
                    started.append(key)
                if since is not None:
                    seen["since"] = since
            elif seen["live"]:
                seen["live"], seen["off_at"] = False, now  # 上一场的开播时刻留着：重连回来时认得出是同一场
        for key in [key for key in self._seen if key not in found]:  # 取消关注了的不用再记
            del self._seen[key]
        return started


def wanted():
    """现在要不要在后台查：玩家打开了开播通知，而且关注了人。"""
    return follows.notify() is True and bool(follows.ids())


def check(tracker, since_after=None, query=None, get_data=None):
    """查一次关注的主播，返回刚开播的 [(选手, 开播状态), ...]。名单读不到、没有关注开直播的人返回 []。
    query、get_data：测试时换掉联网的两步。"""
    data = (get_data or esports_data.get)(fresh=DATA_FRESH)["data"]
    players = follows.players(data, streaming_only=True)
    if not players:
        tracker.forget()
        return []
    found = (query or live_status.query)(players)
    by_id = {player["id"]: player for player in players}
    return [(by_id[key], found[key]) for key in tracker.observe(found, since_after)]


def text(started):
    """通知上写什么：(正文, 标题)。一位写名字、平台和直播标题；几位写在一起。"""
    names = [player["name"] for player, _ in started]
    if len(started) == 1:
        player, got = started[0]
        platform = live_status.NAMES.get(player["platform"], "")
        title = " ".join(str(got.get("title") or "").split())[:60]
        body = f"{platform}：{title}" if title else f"正在{platform}直播"
        return f"{body}\n打开 Evo 的电竞页可以看", f"{names[0][:30]} 开播了"
    shown = "、".join(name[:20] for name in names[:4]) + (f" 等 {len(names)} 位" if len(names) > 4 else "")
    return f"{shown}\n打开 Evo 的电竞页可以看", "关注的主播开播了"
