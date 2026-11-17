"""Evo 窗口的看直播页（panel_esports.py、esports_watch.html）。不联网：赛事文件是临时文件夹里的一个文件，
查开播状态、Apex 在不在运行都换成假的。页面上的脚本（点亮在播的、切换选手、放播放器）在浏览器里看，这里测服务器给的东西。

- 页面：占满窗口、没有顶部导航，有"← 赛事"；赛事名、阶段；正在进行的写"进行中"，没开始的写什么时候开始；
  三个去平台的按钮、同队视角、延迟的说明、Apex 提示条（先藏着）；没有"上一位 / 下一位"
- 右边：每支队一栏（按赛程里的顺序，队标的位置是队名第一个字），每位选手一行，写着平台，先都不能点；
  "全部 N 支 / 有人在播的"；最下面写每 60 秒刷新
- 给脚本的数据：每位选手的直播间地址只按配置拼；只有 B站有嵌入播放器的地址；网址里的 p 是名单里的人才选中；
  名字里的尖括号不会变成网页的一部分
- 开播状态接口：只查这场赛事名单里的人；50 秒内再问不重查；带上平台开关；头像只要 https 的；
  没有这场赛事回 404（页面据此回赛事页）
- Apex 在不在运行的接口
- 别的页面照旧有顶部导航和主题按钮

运行：python tests/test_esports_watch.py
"""

import html
import json
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import esports_data
import game_detect
import live_status
import panel_app
import panel_esports
import storage

NOW = datetime.now().astimezone().replace(microsecond=0)
TEAMS = ("甲队", "<b>乙队</b>", "丙队")


def serve(folder, platforms=None):
    players = []
    for team, platform in zip(TEAMS, ("douyu", "bilibili", "douyin")):
        players += [{"id": f"{platform}-{n}", "name": f"{team[:1]}{n}号" if n > 1 else "kite</script>", "team": team,
                     "platform": platform if n < 3 else "huya", "room": str(100 + n)} for n in (1, 2, 3)]
    events = [{"id": "live", "name": "职业联赛 第 3 周", "type": "联赛", "stage": "B 组对 C 组",
               "start": (NOW - timedelta(hours=1)).isoformat(), "end": (NOW + timedelta(hours=3)).isoformat(),
               "teams": ["丙队", "甲队", "<b>乙队</b>", "没有人的队"]},
              {"id": "soon", "name": "明天的训练赛", "type": "训练赛", "stage": "",
               "start": (NOW + timedelta(days=1)).isoformat(), "end": (NOW + timedelta(days=1, hours=3)).isoformat(),
               "teams": ["甲队"]}]
    path = Path(folder) / "esports.json"
    path.write_text(json.dumps({"version": 1, "updated": NOW.isoformat(), "players": players, "events": events,
                                "platforms": platforms or {}}), encoding="utf-8")
    esports_data.URL = str(path)
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    panel_esports._status.update(at=0.0, found={})


def text(page):
    """网页上看得到的字（去掉标签和脚本，&lt; 这类还原成原来的字）。"""
    return html.unescape(" ".join(re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", page, flags=re.S)).split()))


def page_data(page):
    found = re.search(r'<script type="application/json" id="watch-data">(.*?)</script>', page, re.S)
    return json.loads(found.group(1)) if found else {}


def main():
    print("看直播页（页面、名单、给脚本的数据、开播状态接口、Apex 接口）")
    out = {}
    folder = tempfile.mkdtemp(prefix="evo-watch-")
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    serve(folder, {"douyin": False})

    raw = client.get("/esports/live/watch").get_data(as_text=True)
    words = text(raw)
    head = re.findall(r'<div class="watch-head">.*?</div>', raw, re.S)[0]
    out["占满窗口、没有顶部导航和主题按钮，有\"← 赛事\"能回去；写着赛事名、阶段和\"进行中\""] = (
        '<nav class="nav"' not in raw and 'id="theme-toggle"' not in raw and 'class="wrap watch-wrap"' in raw
        and 'href="/esports"' in head and text(head).split() == ["赛事", "职业联赛", "第", "3", "周", "B", "组对", "C", "组", "进行中"]
        and "es-live-tag" in head)
    soon = client.get("/esports/soon/watch").get_data(as_text=True)
    start = NOW + timedelta(days=1)
    soon_head = re.findall(r'<div class="watch-head">.*?</div>', soon, re.S)[0]
    out["没开始的赛事也能进来：右上角写什么时候开始，不写\"进行中\""] = (
        f"{panel_esports.day_text(start, NOW)} {start:%H:%M} 开始" in text(soon_head) and "进行中" not in soon_head
        and "es-live-tag" not in soon_head and len(page_data(soon)["players"]) == 3)
    out["三个去平台的按钮（用浏览器打开）、同队视角、延迟的说明；没有\"上一位 / 下一位\""] = (
        all(word in words for word in ("发送弹幕", "给主播送礼物", "同队视角",
                                       "每位选手的直播延迟不一样，比赛通常至少延迟 10 分钟，所以切换后画面时间可能对不上。"))
        and raw.count('target="_blank" rel="noopener noreferrer"') == 3 and "上一位" not in raw and "下一位" not in raw)
    apex = re.findall(r'<div class="watch-apex"[^>]*>.*?</div>', raw, re.S)[0]
    out["Apex 提示条先藏着，写着会占用显卡，有\"继续看\"和\"关闭直播\""] = (
        " hidden" in apex.split(">")[0] and "Apex 正在运行。看直播会占用显卡，可能让游戏掉帧。" in text(apex)
        and text(apex).endswith("继续看 关闭直播"))

    boxes = re.findall(r'<details class="watch-team-box".*?</details>', raw, re.S)
    names = [text(re.findall(r'<span class="watch-team-name[^"]*">(.*?)</span>', box, re.S)[0]) for box in boxes]
    logos = [text(re.findall(r'<span class="watch-team-logo"[^>]*>(.*?)</span>', box, re.S)[0]) for box in boxes]
    buttons = re.findall(r'<button type="button" class="watch-p"[^>]*>', raw)
    side = re.findall(r'<aside class="watch-side".*?</aside>', raw, re.S)[0]
    out["右边每支队一栏，按赛程里的顺序，队标的位置是队名第一个字；没有选手的队也在"] = (
        names == ["丙队", "甲队", "<b>乙队</b>", "没有人的队"] and logos == ["丙", "甲", "<", "没"]
        and "名单里还没有这支队的选手" in text(boxes[3]))
    out["每位选手一行，写着平台，查到在播之前都不能点"] = (
        len(buttons) == 9 and all(" disabled" in b and 'aria-pressed="false"' in b for b in buttons)
        and text(boxes[1]).count("斗鱼") == 2 and text(boxes[1]).count("虎牙") == 1 and "kite</script>" in text(boxes[1]))
    out["\"全部 N 支 / 有人在播的\"、在播人数的位置、最下面写每 60 秒刷新"] = (
        "全部 4 支" in text(side) and "有人在播的" in text(side) and 'id="watch-count"' in side
        and "开着这一页时每 60 秒刷新一次谁在播。" in text(side))

    data = page_data(raw)
    by_id = {p["id"]: p for p in data.get("players", [])}
    out["给脚本的数据：直播间地址按配置拼；只有 B站有嵌入播放器的地址；带上平台开关和两个接口的地址"] = (
        list(by_id) == [f"{p}-{n}" for p in ("douyin", "douyu", "bilibili") for n in (1, 2, 3)]
        and by_id["douyu-1"]["url"] == "https://www.douyu.com/101" and by_id["douyu-3"]["url"] == "https://www.huya.com/103"
        and by_id["bilibili-2"]["embed"] == "https://www.bilibili.com/blackboard/live/live-activity-player.html?cid=102&quality=0"
        and [p["id"] for p in by_id.values() if p["embed"]] == ["bilibili-1", "bilibili-2"]
        and data["platforms"] == {"douyu": True, "huya": True, "bilibili": True, "douyin": False}
        and data["status_url"] == "/esports/live/status" and data["game_url"] == "/esports/game")
    picked = page_data(client.get("/esports/live/watch?p=bilibili-2").get_data(as_text=True))
    wrong = page_data(client.get("/esports/live/watch?p=nobody").get_data(as_text=True))
    out["网址里的 p 是名单里的选手才选中；不是的话不选"] = (
        data["selected"] is None and picked["selected"] == "bilibili-2" and wrong["selected"] is None)
    out["名字里的尖括号不会变成网页的一部分"] = (
        "kite</script>" not in raw and "<b>乙队</b>" not in raw and by_id["douyu-1"]["name"] == "kite</script>"
        and by_id["bilibili-1"]["team"] == "<b>乙队</b>")

    asked = []

    def fake_query(players):
        asked.append(sorted(p["id"] for p in players))
        return {p["id"]: live_status.result(
            "live" if p["id"].endswith("-1") else "error" if p["id"] == "douyu-2" else "offline", name="平台上的名字",
            title="比赛日", since=1794900000 if p["id"].endswith("-1") else None,
            avatar={"douyu-1": "http://apic.douyucdn.cn/a.jpg", "bilibili-1": "javascript:alert(1)",
                    "douyin-1": "https://p3.douyinpic.com/a.jpeg"}.get(p["id"], ""),
            error="连不上" if p["id"] == "douyu-2" else "") for p in players}

    real_query, live_status.query = live_status.query, fake_query
    real_game = game_detect.game_running
    try:
        got = client.get("/esports/live/status").get_json()
        again = client.get("/esports/live/status").get_json()
        players = got["players"]
        out["开播状态接口：只查这场赛事名单里的人，回每个人在不在播、标题、开播时刻；不把没查到的原因发给页面"] = (
            asked == [sorted(by_id)] and players["douyu-1"] == {"state": "live", "loop": False, "title": "比赛日",
                                                              "since": 1794900000, "avatar": "https://apic.douyucdn.cn/a.jpg"}
            and players["douyu-2"]["state"] == "error" and players["douyu-3"]["state"] == "offline"
            and "连不上" not in json.dumps(got, ensure_ascii=False))
        out["头像只要 https 的（http 的换成 https），别的不要"] = (
            players["douyu-1"]["avatar"] == "https://apic.douyucdn.cn/a.jpg" and players["bilibili-1"]["avatar"] == ""
            and players["douyin-1"]["avatar"] == "https://p3.douyinpic.com/a.jpeg")
        out["带上平台开关（esports.json 里关掉的平台，页面改成去平台看）"] = got["platforms"]["douyin"] is False
        out["50 秒内再问不重查"] = len(asked) == 1 and again["players"] == players
        panel_esports._status["at"] -= 51
        client.get("/esports/live/status")
        out["页面每 60 秒问一次，那时会重查（开播、下播后 2 分钟内页面跟着变）"] = len(asked) == 2
        missing = client.get("/esports/no-such/status")
        out["没有这场赛事：回 404（页面据此回赛事页），不去查直播间"] = missing.status_code == 404 and len(asked) == 2
        game_detect.game_running = lambda: True
        on = client.get("/esports/game").get_json()
        game_detect.game_running = lambda: False
        off = client.get("/esports/game").get_json()
        out["Apex 在不在运行的接口（只看进程名）"] = on == {"running": True} and off == {"running": False}
    finally:
        live_status.query, game_detect.game_running = real_query, real_game

    home = client.get("/esports").get_data(as_text=True)
    out["别的页面照旧有顶部导航和主题按钮"] = '<nav class="nav"' in home and 'id="theme-toggle"' in home
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
