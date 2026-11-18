"""协作网页（server/team/esports.js）和 tools/esports.py 生成的 esports.json 一模一样：同一份选手库、赛程，两边各生成一次逐项比。
- 赛事的排序和编号（同一时刻的同类赛事加 -2）、结束超过 7 天的不放（正好 7 天的还放）、只放用到的队的选手、
  平台开关照给的原样、更新时间的写法
- 40 个字的名字（里面有表情这种占两个位置的字）两边都按 40 个字算，Evo 读的时候（esports_data.clean）一条都不去掉
- 抖音的房间号可以写抖音号（字母、数字、下划线），两边都转成小写；XXFPS 和 xxfps 算同一个房间；带点的、斗鱼写字母的两边都不收
- 队徽：同一份 {队名: {"logo"}} 给两边，只放这次用到、文件名写法对的队（多出来的键丢掉），键按队名排
- 平台名单（英文名、中文名、直播间地址、房间号的写法、顺序）和 config/watch.json 一样
两边走的都是真实的流程：网页是"保存时的检查 → 生成"，Python 是 tools/esports.py 的 check_players → build
要 Node.js（协作网页是 JavaScript）：电脑上没有 node 就跳过。
运行：python tests/test_team_publish.py
"""

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）
import esports_data
import live_status

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import esports as tool  # noqa: E402  tools/esports.py

RUNNER = ROOT / "server" / "team" / "test.mjs"
BEIJING = live_status.BEIJING
NOW = datetime(2026, 11, 17, 20, 0, 5, tzinfo=BEIJING)
LONG = "表情😀" * 13 + "x"  # 40 个字（Python 按字数算；JavaScript 里表情占两个位置）


def node(*args):
    result = subprocess.run(["node", str(RUNNER), *args], capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace"))
    return json.loads(result.stdout.decode("utf-8"))


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=BEIJING)


def sample():
    """网页存的样子（选手库、赛程按添加的顺序）"""
    players = []
    for team, people in [("星火电竞", [("Kite", "douyu", "520013", "Kite_(player)"), ("Ace", "bilibili", "100", ""),
                                        (LONG, "huya", "200", "")]),
                         ("北辰", [("北一", "douyin", "XXFPS", ""), ("北二", "douyin", "NOVA_Tt1seven", ""), ("北三", "douyu", "303", "")]),
                         ("没比赛的队", [("闲一", "douyu", "901", ""), ("闲二", "douyu", "902", ""), ("闲三", "douyu", "903", "")])]:
        for name, platform, room, liquipedia in people:
            players.append({"name": name, "team": team, "platform": platform, "room": room, "liquipedia": liquipedia})
    events = [
        ("联赛 第 3 周", "联赛", "B 组对 C 组", at(17, 19), at(17, 23), ["北辰", "星火电竞"]),
        ("同时的杯赛 一", "杯赛", "", at(18, 19), at(18, 21), ["北辰"]),
        ("训练赛", "训练赛", "", at(17, 10), at(17, 13), ["星火电竞"]),
        ("同时的杯赛 二", "杯赛", "决赛", at(18, 19), at(18, 22), ["星火电竞"]),
        ("正好 7 天前结束", "杯赛", "", at(10, 17), NOW.replace(day=10, second=5), ["北辰"]),
        ("8 天前结束", "杯赛", "", at(9, 10), at(9, 12), ["北辰"]),
    ]
    events = [{"name": n, "type": t, "stage": s, "start": a.isoformat(), "end": b.isoformat(), "teams": teams}
              for n, t, s, a, b, teams in events]
    return players, events


def main():
    if not shutil.which("node"):
        print("  跳过：电脑上没有 Node.js（协作网页的测试要它）")
        return None
    checks = []
    players, events = sample()
    switches = {"douyu": False, "huya": True}
    teams = {"北辰": {"logo": "3fa9c1d2e4b5a6f7.webp", "by": "黄指导"}, "星火电竞": {"logo": "BAD.png"},
             "没比赛的队": {"logo": "0123456789abcdef.png"}, "没有的队": {"logo": "fedcba9876543210.png"}}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "input.json"
        path.write_text(json.dumps({"players": players, "events": events, "switches": switches, "teams": teams,
                                    "now": int(NOW.timestamp() * 1000)}, ensure_ascii=False), encoding="utf-8")
        web = node("--build", str(path))

    # Python 那边：照选手库.csv 的样子（平台写中文名）过一遍 check_players，再 build
    table = [(i + 2, {"选手": p["name"], "战队": p["team"], "平台": live_status.NAMES[p["platform"]], "房间号": p["room"],
                      "Liquipedia页面名": p["liquipedia"]}) for i, p in enumerate(players)]
    errors = []
    rows, _ = tool.check_players(table, errors)
    plans = [{"line": i + 2, "name": e["name"], "type": e["type"], "stage": e["stage"],
              "start": datetime.fromisoformat(e["start"]), "end": datetime.fromisoformat(e["end"]), "teams": e["teams"]}
             for i, e in enumerate(events)]
    python = tool.build(rows, plans, switches, [], now=NOW, teams=teams)

    checks.append(("两边检查都没问题", web["errors"] == [] and errors == [], (web["errors"], errors)))
    checks.append(("两边生成的 esports.json 一模一样", web["data"] == python,
                   json.dumps({"网页": web["data"], "Python": python}, ensure_ascii=False)[:2000]))
    ids = [e["id"] for e in python["events"]]
    checks.append(("赛事按开始时间排、同一时刻的同类赛事加 -2、8 天前的不放、正好 7 天的还放",
                   ids == ["20261110-1700-cup", "20261117-1000-scrim", "20261117-1900-league", "20261118-1900-cup",
                           "20261118-1900-cup-2"], ids))
    checks.append(("没比赛的队不放、40 个字的名字留着", {p["team"] for p in python["players"]} == {"星火电竞", "北辰"}
                   and any(p["name"] == LONG for p in python["players"]), [p["name"] for p in python["players"]]))
    cleaned = esports_data.clean(web["data"])
    checks.append(("Evo 读网页生成的文件一条都不去掉", len(cleaned["players"]) == 6 and len(cleaned["events"]) == 5,
                   (len(cleaned["players"]), len(cleaned["events"]))))
    checks.append(("平台开关照给的原样（没写的当作开）",
                   python["platforms"] == {"douyu": False, "huya": True, "bilibili": True, "douyin": True}, python["platforms"]))
    checks.append(("更新时间的写法一样", web["data"]["updated"] == "2026-11-17T20:00:05+08:00", web["data"]["updated"]))
    checks.append(("队徽：只放用到、写法对的队，只留 logo", python.get("teams") == {"北辰": {"logo": "3fa9c1d2e4b5a6f7.webp"}}
                   and list(web["data"]).index("teams") > list(web["data"]).index("events"), python.get("teams")))
    checks.append(("Evo 读的时候队徽一条都不去掉", cleaned["teams"] == {"北辰": {"logo": "3fa9c1d2e4b5a6f7.webp"}}, cleaned.get("teams")))
    douyin = {p["name"]: (p["room"], p["id"]) for p in web["data"]["players"] if p["platform"] == "douyin"}
    checks.append(("抖音号转成小写，选手 id 跟着用小写", douyin == {"北一": ("xxfps", "douyin-xxfps"),
                                                       "北二": ("nova_tt1seven", "douyin-nova_tt1seven")}, douyin))

    # 房间号的几种写法：网页（保存时的检查 + 整张表）和 tools/esports.py 的 check_players 要么都收、要么都不收
    cases = {
        "抖音号大小写混着写": [("douyin", "NOVA_Tt1seven")],
        "XXFPS 和 xxfps 算同一个房间": [("douyin", "XXFPS"), ("douyin", "xxfps")],
        "抖音号带点": [("douyin", "a.b")],
        "抖音号超过 24 个字": [("douyin", "a" * 25)],
        "斗鱼写字母": [("douyu", "xxfps")],
        "B站写数字": [("bilibili", "21452505")],
    }
    for text, rooms in cases.items():
        people = [{"name": f"人{i}", "team": "队", "platform": platform, "room": room} for i, (platform, room) in enumerate(rooms)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "players.json"
            path.write_text(json.dumps(people, ensure_ascii=False), encoding="utf-8")
            web_result = node("--check-players", str(path))
        python_errors = []
        kept, _ = tool.check_players([(i + 2, {"选手": x["name"], "战队": "队", "平台": live_status.NAMES[x["platform"]],
                                               "房间号": x["room"], "Liquipedia页面名": ""}) for i, x in enumerate(people)],
                                     python_errors)
        python_errors = [e for e in python_errors if "房间" in e]
        same = bool(web_result["errors"]) == bool(python_errors) and (python_errors or web_result["rooms"] == [x["room"] for x in kept])
        checks.append((f"房间号：{text}（{'不收' if python_errors else '收'}，两边一样）", same,
                       {"网页": web_result, "Python": python_errors or [x["room"] for x in kept]}))

    platforms = node("--platforms")
    expected = [[key, p["name"], p["room_url"], p.get("room_pattern")] for key, p in live_status.PLATFORMS.items()]
    checks.append(("平台名单（英文名、中文名、直播间地址、房间号的写法、顺序）和 config/watch.json 一样", platforms == expected,
                   {"网页": platforms, "watch.json": expected}))
    for text, ok, detail in checks:
        if not ok:
            print(f"  {text}：{detail}")
    return report({text: ok for text, ok, _ in checks})


if __name__ == "__main__":
    sys.exit(0 if main() is not False else 1)
