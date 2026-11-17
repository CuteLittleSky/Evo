"""协作网页（server/team/esports.js）和 tools/esports.py 生成的 esports.json 一模一样：同一份选手库、赛程，两边各生成一次逐项比。
- 赛事的排序和编号（同一时刻的同类赛事加 -2）、结束超过 7 天的不放（正好 7 天的还放）、只放用到的队的选手、
  平台开关照给的原样、更新时间的写法
- 40 个字的名字（里面有表情这种占两个位置的字）两边都按 40 个字算，Evo 读的时候（esports_data.clean）一条都不去掉
- 平台名单（英文名、中文名、直播间地址、顺序）和 config/watch.json 一样
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
                         ("北辰", [("北一", "douyin", "301", ""), ("北二", "douyin", "302", ""), ("北三", "douyu", "303", "")]),
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
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "input.json"
        path.write_text(json.dumps({"players": players, "events": events, "switches": switches,
                                    "now": int(NOW.timestamp() * 1000)}, ensure_ascii=False), encoding="utf-8")
        web = node("--build", str(path))

    rows = [{"line": i + 2, **p} for i, p in enumerate(players)]
    plans = [{"line": i + 2, "name": e["name"], "type": e["type"], "stage": e["stage"],
              "start": datetime.fromisoformat(e["start"]), "end": datetime.fromisoformat(e["end"]), "teams": e["teams"]}
             for i, e in enumerate(events)]
    python = tool.build(rows, plans, switches, [], now=NOW)

    checks.append(("网页那边检查没问题", web["errors"] == [], web["errors"]))
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

    platforms = node("--platforms")
    expected = [[key, p["name"], p["room_url"]] for key, p in live_status.PLATFORMS.items()]
    checks.append(("平台名单（英文名、中文名、直播间地址、顺序）和 config/watch.json 一样", platforms == expected,
                   {"网页": platforms, "watch.json": expected}))
    for text, ok, detail in checks:
        if not ok:
            print(f"  {text}：{detail}")
    return report({text: ok for text, ok, _ in checks})


if __name__ == "__main__":
    sys.exit(0 if main() is not False else 1)
