"""发赛事数据：把本机的两张表（选手库.csv、赛程.csv）检查一遍，转成 esports.json 上传，Evo 的赛事页读它
（格式和 Evo 怎么读见 code/esports_data.py）。

    python tools/esports.py                 检查两张表 → 打印预览 → 输入 y 才上传 → 下载回来核对
    python tools/esports.py --check         检查两张表，再把每个房间问一遍（存不存在、现在在不在播），不上传
    python tools/esports.py --test          同第一条，但传到 esports_test.json（试的时候用，玩家的 Evo 不读它）
    python tools/esports.py --out 文件      只生成 json 写到这个文件，不上传（在本机看效果用）
    python tools/esports.py --show          看线上现在是什么（加 --test 看测试文件）
    python tools/esports.py --off 斗鱼      关掉一个平台：Evo 不在窗口里播它，改成"去斗鱼看"（--on 斗鱼 再打开）
    python tools/esports.py --init          esports/ 里还没有表时，建两张示例表和一份填写说明

两张表在 E:\\Evo\\esports\\（不进 git：里面是别人的房间号），用 Excel 或 WPS 改，存成 CSV；GBK、UTF-8 都认。
- 选手库.csv：选手,战队,平台,房间号,Liquipedia页面名。平台只能写 斗鱼 / 虎牙 / B站 / 抖音；房间号只写数字（抖音的也可以写主播的抖音号：字母、数字、下划线，存的时候转成小写）；最后一列可以空
- 赛程.csv：名称,类型,阶段,开始,结束,队伍。类型是 联赛 / 训练赛 / 杯赛；时间写北京时间 2026-11-17 19:00；
  队伍用顿号「、」隔开，每个队名都要在选手库里、正好 3 个人
有错就指出是哪个文件第几行，不上传。结束超过 7 天的赛事不放进去（赛事页只显示最近 7 天的）；选手只放这些赛事用到的。
平台开关记在线上的文件里：平常上传时照线上现在的开关，--off / --on 只改开关、不动别的。
只传 R2（https://dl.evotracker.cn/esports.json ，和 notice.json 一起）：COS 停用中，Evo 也只读 R2。
"""

import argparse
import csv
import difflib
import io
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import code_path  # noqa: F401  先把 code/ 加进搜索路径，下面才能 import 程序代码（见 code_path.py）
import esports_data
import live_status
from release import DIST, Stop

ROOT = code_path.ROOT
TABLES = ROOT / "esports"
PLAYERS_NAME, EVENTS_NAME, GUIDE_NAME = "选手库.csv", "赛程.csv", "填写说明.txt"
PLAYER_COLUMNS = ["选手", "战队", "平台", "房间号", "Liquipedia页面名"]
EVENT_COLUMNS = ["名称", "类型", "阶段", "开始", "结束", "队伍"]
NAME, TEST_NAME = "esports.json", "esports_test.json"
CONTENT_TYPE = "application/json; charset=utf-8"
PLATFORM_KEYS = {name: key for key, name in live_status.NAMES.items()}  # {"斗鱼": "douyu", ...}
TYPE_KEYS = {"联赛": "league", "训练赛": "scrim", "杯赛": "cup"}
TEAM_SIZE = 3
USUAL_TEAMS = 20
EXAMPLE = "示例"
TIME_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s+(\d{1,2})[:：](\d{2})(?::\d{2})?")
GUIDE = """赛事数据怎么填（python tools/esports.py --init 生成的说明）

选手库.csv：选手,战队,平台,房间号,Liquipedia页面名
  - 一个选手一行；一支队正好 3 个人；一个选手只能在一支队
  - 平台只能写：斗鱼 / 虎牙 / B站 / 抖音
  - 房间号只写数字，就是直播间地址最后那串数字（虎牙要数字房间号，不要写英文别名）。
    抖音的地址 live.douyin.com/ 后面有的是字母（主播的抖音号，如 xxfps），照着写就行，大小写都可以
  - Liquipedia页面名可以空着
  - 只有转会、换平台时才改

赛程.csv：名称,类型,阶段,开始,结束,队伍
  - 类型只能写：联赛 / 训练赛 / 杯赛
  - 开始、结束写北京时间，如 2026-11-17 19:00（Excel 改成 2026/11/17 19:00 也认）
  - 队伍：队名用顿号「、」隔开，每个队名要和选手库里的一模一样
  - 阶段可以空着

改完以后（在 E:\\Evo 里运行）：
  python tools/esports.py --check     检查两张表，再把每个房间问一遍
  python tools/esports.py             检查 → 预览 → 输入 y 上传
表里带"示例"两个字的数据不能上传，先删掉。
"""


def now_beijing():
    return datetime.now(live_status.BEIJING)


# ---------- 读表 ----------

def read_table(path, columns):
    """读一张 CSV，返回 [(行号, {列名: 内容}), ...]（空行跳过）。GBK（中文 Windows 上 Excel 默认存的）、UTF-8
    （带不带 BOM）、UTF-16（Excel 的"Unicode 文本"）都认。"""
    try:
        raw = path.read_bytes()
    except OSError:
        raise Stop(f"找不到 {path}（先运行 python tools/esports.py --init 建示例表）") from None
    encodings = ("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise Stop(f"{path.name} 读不出来：用 Excel 另存为「CSV UTF-8（逗号分隔）」再试")
    first = text.split("\n", 1)[0]
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter="\t" if "\t" in first and "," not in first else ","))
    header = [cell.strip() for cell in rows[0]] if rows else []
    while header and not header[-1]:
        header.pop()
    if header != columns:
        raise Stop(f"{path.name} 第 1 行（表头）要是：{','.join(columns)}\n    现在是：{','.join(header)}")
    out = []
    for number, row in enumerate(rows[1:], start=2):
        cells = [" ".join(cell.split()) for cell in row] + [""] * len(columns)
        if any(cells):
            out.append((number, dict(zip(columns, cells))))
    return out


def parse_time(text):
    found = TIME_RE.fullmatch(text)
    if not found:
        return None
    try:
        return datetime(*(int(part) for part in found.groups()), tzinfo=live_status.BEIJING)
    except ValueError:
        return None


def check_players(rows, errors):
    """选手库 → [{"line", "name", "team", "platform", "room", "liquipedia"}, ...]；问题写进 errors。"""
    players, names, rooms, teams = [], {}, {}, {}
    for line, row in rows:
        where = f"{PLAYERS_NAME} 第 {line} 行"
        name, team, platform, room = row["选手"], row["战队"], row["平台"], row["房间号"]
        bad = False
        for column in ("选手", "战队", "平台", "房间号"):
            if not row[column]:
                errors.append(f"{where}：「{column}」是空的")
                bad = True
        if platform and platform not in PLATFORM_KEYS:
            errors.append(f"{where}：平台「{platform}」不认识，只能写 {' / '.join(PLATFORM_KEYS)}")
            bad = True
        if room and platform == "抖音" and live_status.room_ok("douyin", room):
            room = room.lower()  # 抖音不分大小写：转成小写，XXFPS 和 xxfps 才查得出是同一个房间，选手的 id 也不会变
        elif room and platform == "抖音":
            errors.append(f"{where}：房间号「{room}」只能是字母、数字、下划线（直播间地址 live.douyin.com/ 后面那一段）")
            bad = True
        elif room and not live_status.ROOM_RE.fullmatch(room):
            errors.append(f"{where}：房间号「{room}」只能是数字（直播间地址最后那串数字）")
            bad = True
        if max(len(name), len(team)) > 40:
            errors.append(f"{where}：选手名、队名最多 40 个字")
            bad = True
        if name and name in names:
            errors.append(f"{where}：选手「{name}」在第 {names[name]} 行已经写过（一个选手只写一行、只能在一支队）")
            bad = True
        if not bad and (platform, room) in rooms:
            errors.append(f"{where}：{platform} 房间 {room} 在第 {rooms[(platform, room)]} 行已经写过")
            bad = True
        if name:
            names.setdefault(name, line)
        if team:
            teams.setdefault(team, []).append(line)
        if not bad:
            rooms[(platform, room)] = line
            players.append({"line": line, "name": name, "team": team, "platform": PLATFORM_KEYS[platform],
                            "room": room, "liquipedia": row["Liquipedia页面名"]})
    for team, lines in teams.items():
        if len(lines) != TEAM_SIZE:
            errors.append(f"{PLAYERS_NAME}：战队「{team}」有 {len(lines)} 个人（第 {'、'.join(map(str, lines))} 行），"
                          f"要正好 {TEAM_SIZE} 个")
    return players, set(teams)


def check_events(rows, known_teams, errors, notes):
    """赛程 → [{"line", "name", "type", "stage", "start", "end", "teams"}, ...]；问题写进 errors，提醒写进 notes。"""
    events = []
    for line, row in rows:
        where = f"{EVENTS_NAME} 第 {line} 行"
        before = len(errors)
        if not row["名称"]:
            errors.append(f"{where}：「名称」是空的")
        if len(row["名称"]) > 80 or len(row["阶段"]) > 80:
            errors.append(f"{where}：名称、阶段最多 80 个字")
        if row["类型"] not in TYPE_KEYS:
            errors.append(f"{where}：类型「{row['类型']}」不认识，只能写 {' / '.join(TYPE_KEYS)}")
        start, end = parse_time(row["开始"]), parse_time(row["结束"])
        for column, moment in (("开始", start), ("结束", end)):
            if moment is None:
                errors.append(f"{where}：{column}「{row[column]}」要写成 2026-11-17 19:00（北京时间）")
        if start and end and end <= start:
            errors.append(f"{where}：结束（{row['结束']}）要晚于开始（{row['开始']}）")
        teams = [team.strip() for team in row["队伍"].split("、") if team.strip()]
        if not teams:
            errors.append(f"{where}：「队伍」是空的")
        elif len(teams) == 1 and re.search(r"[，,;；/]", teams[0]):
            errors.append(f"{where}：队伍要用顿号「、」隔开")
        else:
            for team in dict.fromkeys(teams):
                if teams.count(team) > 1:
                    errors.append(f"{where}：队伍「{team}」写了 {teams.count(team)} 次")
                if team not in known_teams:
                    close = difflib.get_close_matches(team, known_teams, n=1, cutoff=0.6)
                    errors.append(f"{where}：队伍「{team}」不在{PLAYERS_NAME}里" + (f"（是不是「{close[0]}」？）" if close else ""))
            if len(teams) != USUAL_TEAMS:
                notes.append(f"{where}：「{row['名称']}」有 {len(teams)} 支队（一般是 {USUAL_TEAMS} 支），没写错就不用管")
        if len(errors) == before:
            events.append({"line": line, "name": row["名称"], "type": row["类型"], "stage": row["阶段"],
                           "start": start, "end": end, "teams": teams})
    return events


def load_tables(folder):
    """读两张表并检查。返回 (选手, 赛事, 提醒)；有错把每一条都打印出来再停下。"""
    errors, notes = [], []
    players, teams = check_players(read_table(folder / PLAYERS_NAME, PLAYER_COLUMNS), errors)
    events = check_events(read_table(folder / EVENTS_NAME, EVENT_COLUMNS), teams, errors, notes)
    if errors:
        raise Stop(f"表里有 {len(errors)} 处要改：\n" + "\n".join(f"  · {error}" for error in errors))
    return players, events, notes


# ---------- 转成 esports.json ----------

def build(players, events, switches, notes, now=None, teams=None):
    """teams：{队名: {"logo": 文件名}}（队徽；这个脚本不管队徽，上传时照线上现在的带上）。只留这次用到的队，一个都没有就不写这一项。"""
    now = now or now_beijing()
    kept, ids = [], set()
    old = [event for event in events if now - event["end"] > timedelta(days=esports_data.RECENT_DAYS)]
    if old:
        notes.append(f"{len(old)} 场赛事结束超过 {esports_data.RECENT_DAYS} 天，没放进去（可以从{EVENTS_NAME}里删掉）："
                     + "、".join(f"第 {event['line']} 行" for event in old))
    for event in sorted((event for event in events if event not in old), key=lambda e: (e["start"], e["line"])):
        eid = base = f"{event['start']:%Y%m%d-%H%M}-{TYPE_KEYS[event['type']]}"
        number = 1
        while eid in ids:  # 同一时刻开始的同类赛事：后面的加 -2、-3
            number += 1
            eid = f"{base}-{number}"
        ids.add(eid)
        kept.append({"id": eid, "name": event["name"], "type": event["type"], "stage": event["stage"],
                     "start": event["start"].isoformat(), "end": event["end"].isoformat(), "teams": event["teams"]})
    used = {team for event in kept for team in event["teams"]}
    data = {"version": esports_data.VERSION, "updated": now.isoformat(timespec="seconds"),
            "platforms": {key: switches.get(key) is not False for key in live_status.PLATFORMS},
            "players": [{"id": f"{p['platform']}-{p['room']}", "name": p["name"], "team": p["team"],
                         "platform": p["platform"], "room": p["room"], "liquipedia": p["liquipedia"]}
                        for p in players if p["team"] in used],
            "events": kept}
    logos = esports_data.clean_teams(teams, used)
    if logos:
        data["teams"] = {team: logos[team] for team in sorted(logos)}
    checked = esports_data.clean(data)  # Evo 读的时候会不会去掉哪一条
    if len(checked["players"]) != len(data["players"]) or len(checked["events"]) != len(data["events"]):
        raise Stop("生成的文件里有 Evo 会去掉的条目（名字里有特别的字符？），告诉 Claude 查一下")
    return data


def describe(data, now=None):
    """一份 esports.json 写成几行字（预览、--show 用）。"""
    now = now or now_beijing()
    data = esports_data.clean(data)
    count = {}
    for player in data["players"]:
        count[player["platform"]] = count.get(player["platform"], 0) + 1
    teams = {team for event in data["events"] for team in event["teams"]}
    words = {esports_data.LIVE: "进行中", esports_data.UPCOMING: "即将开始", esports_data.ENDED: "已结束"}
    lines = ["  平台开关：" + "、".join(f"{live_status.NAMES[key]} {'开' if on else '关（改成去平台看）'}"
                                    for key, on in data["platforms"].items()),
             f"  {len(data['events'])} 场赛事，{len(teams)} 支队，{len(data['players'])} 位选手"
             + (f"（{'、'.join(f'{live_status.NAMES[key]} {n}' for key, n in count.items())}）" if count else "")
             + (f"，更新于 {data['updated'][:16].replace('T', ' ')}" if data["updated"] else "")]
    for event in data["events"]:
        start, end = esports_data.parse_time(event["start"]), esports_data.parse_time(event["end"])
        lines.append(f"    [{words[esports_data.status(event, now)]}] {start:%m-%d %H:%M}–{end:%H:%M} {event['type']} "
                     f"{event['name']}" + (f" · {event['stage']}" if event["stage"] else "")
                     + f" · {len(event['teams'])} 支队 {len(esports_data.event_players(data, event))} 人")
    return "\n".join(lines)


# ---------- 线上（R2） ----------

def online(name):
    """线上现在的文件（没有、格式不对返回 None）。"""
    from publish import DL, remote_bytes  # 用到 R2 时才引入（只检查表、--out 不需要）
    body = remote_bytes(f"{DL}/{name}")
    try:
        data = json.loads(body.decode("utf-8")) if body is not None else None
        esports_data.clean(data)
    except (ValueError, esports_data.EsportsError):
        return None
    return data


def upload(data, name):
    from publish import BUCKET, DL, remote_bytes, wrangler
    local = DIST / name
    body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(body)
    print(f"\n上传到 R2：{DL}/{name}（{len(body) / 1024:.1f} KB）")
    wrangler("r2", "object", "put", f"{BUCKET}/{name}", "--file", str(local), "--remote",
             "--cache-control", "no-cache", "--content-type", CONTENT_TYPE)
    if remote_bytes(f"{DL}/{name}") != body:
        raise Stop(f"{DL}/{name} 和本机的 {local} 不一样")
    print("    ✅ 下载回来核对过，一样")


def confirm(args, data, name):
    from publish import DL
    current = online(name)
    print(f"线上现在（{DL}/{name}）：\n" + ("  没有这个文件" if current is None else describe(current)))
    print("要换成：\n" + describe(data))
    try:
        agreed = args.yes or input("\n确认上传吗？输入 y 上传：").strip().lower() == "y"
    except EOFError:  # 没有人在键盘前（比如 Claude 运行的）：不上传
        agreed = False
    if not agreed:
        print("没有上传")
        return False
    upload(data, name)
    return True


# ---------- 各个命令 ----------

def init(folder):
    folder.mkdir(parents=True, exist_ok=True)
    today = now_beijing().replace(hour=19, minute=0, second=0, microsecond=0)
    later = today + timedelta(days=3)
    files = {
        PLAYERS_NAME: [PLAYER_COLUMNS, ["示例选手1", "示例战队", "斗鱼", "000001", ""],
                       ["示例选手2", "示例战队", "B站", "000002", ""], ["示例选手3", "示例战队", "抖音", "000003", ""]],
        EVENTS_NAME: [EVENT_COLUMNS,
                      ["示例联赛 第 1 周", "联赛", "A 组对 B 组", f"{today:%Y-%m-%d %H:%M}",
                       f"{today + timedelta(hours=4):%Y-%m-%d %H:%M}", "示例战队"],
                      ["示例训练赛", "训练赛", "", f"{later:%Y-%m-%d %H:%M}",
                       f"{later + timedelta(hours=3):%Y-%m-%d %H:%M}", "示例战队"]]}
    for name, rows in files.items():
        path = folder / name
        if path.exists():
            print(f"已经有 {path}，没有动它")
            continue
        text = io.StringIO(newline="")
        csv.writer(text).writerows(rows)
        path.write_bytes(text.getvalue().encode("utf-8-sig"))  # 带 BOM：Excel 打开不乱码
        print(f"建好了 {path}（里面是示例，换成真的）")
    (folder / GUIDE_NAME).write_text(GUIDE, encoding="utf-8-sig")
    print(f"填写说明：{folder / GUIDE_NAME}")


def check_rooms(players):
    """把每个房间问一遍并打印。有不存在的房间返回 False。"""
    print(f"\n把 {len(players)} 个房间问一遍（同时最多 {live_status.CONFIG['parallel']} 个请求）……")
    asked = [{"id": str(p["line"]), "platform": p["platform"], "room": p["room"]} for p in players]
    found = live_status.query(asked)
    marks = {live_status.LIVE: "🔴", live_status.OFFLINE: "⚪", live_status.MISSING: "❌", live_status.ERROR: "⚠"}
    count = dict.fromkeys(marks, 0)
    for player in sorted(players, key=lambda p: (p["team"], p["line"])):
        state = found[str(player["line"])]
        count[state["state"]] += 1
        print(f"  {marks[state['state']]} 第 {player['line']} 行 {player['team']} / {player['name']}（"
              f"{live_status.NAMES[player['platform']]} {player['room']}）：{live_status.describe(state)}")
    print(f"\n在播 {count[live_status.LIVE]}，没开播 {count[live_status.OFFLINE]}，"
          f"房间不存在 {count[live_status.MISSING]}，没查到 {count[live_status.ERROR]}")
    print("平台上的主播名写在冒号后面：和选手对不上的，多半是房间号填错了。")
    if count[live_status.ERROR]:
        print("⚠ 没查到的：可能是网络，也可能是平台改了接口；过一会儿再试，还不行告诉 Claude")
    return not count[live_status.MISSING]


def switch(args, name):
    key = PLATFORM_KEYS.get(args.off or args.on, args.off or args.on)
    if key not in live_status.PLATFORMS:
        raise Stop(f"平台只能写 {' / '.join(PLATFORM_KEYS)}")
    data = online(name)
    if data is None:
        raise Stop(f"线上还没有 {name}：先上传一次赛事数据")
    data["platforms"] = {**esports_data.clean(data)["platforms"], key: bool(args.on)}
    data["updated"] = now_beijing().isoformat(timespec="seconds")
    return confirm(args, data, name)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="发赛事数据（说明见 esports.py 开头）")
    parser.add_argument("--check", action="store_true", help="检查两张表，再把每个房间问一遍，不上传")
    parser.add_argument("--test", action="store_true", help=f"传到 {TEST_NAME}（试的时候用）")
    parser.add_argument("--out", help="只生成 json 写到这个文件，不上传")
    parser.add_argument("--show", action="store_true", help="看线上现在是什么")
    parser.add_argument("--off", metavar="平台", help="关掉一个平台（Evo 改成去平台看）")
    parser.add_argument("--on", metavar="平台", help="重新打开一个平台")
    parser.add_argument("--init", action="store_true", help="建两张示例表和填写说明")
    parser.add_argument("--dir", default=str(TABLES), help="两张表所在的文件夹（默认 esports/）")
    parser.add_argument("--yes", action="store_true", help="不再问确认")
    args = parser.parse_args()
    folder = Path(args.dir)
    name = TEST_NAME if args.test else NAME
    try:
        if args.init:
            init(folder)
            return 0
        if args.show:
            data = online(name)
            print(f"线上的 {name}：\n" + ("  没有这个文件（或格式不对）" if data is None else describe(data)))
            return 0
        if args.off or args.on:
            if args.off and args.on:
                parser.error("--off 和 --on 一次只能用一个")
            return 0 if switch(args, name) else 1
        players, events, notes = load_tables(folder)
        print(f"两张表没有问题：{len(players)} 位选手、{len({p['team'] for p in players})} 支队、{len(events)} 场赛事")
        if args.check:
            for note in notes:
                print(f"提醒：{note}")
            return 0 if check_rooms(players) else 1
        if args.out:
            data = build(players, events, {}, notes)
            Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(describe(data))
            for note in notes:
                print(f"提醒：{note}")
            print(f"写到了 {args.out}（没有上传）")
            return 0
        current = online(name)
        online_now = esports_data.clean(current) if current else {}
        data = build(players, events, online_now.get("platforms", {}), notes, teams=online_now.get("teams"))
        for note in notes:
            print(f"提醒：{note}")
        examples = [text for text in [p["name"] for p in data["players"]] + [e["name"] for e in data["events"]]
                    if EXAMPLE in text]
        if examples:
            raise Stop(f"表里还有示例数据（{'、'.join(examples[:5])}），删掉再上传")
        if not data["events"]:
            raise Stop("没有可以上传的赛事（赛程是空的，或者都结束超过 7 天了）")
        if not confirm(args, data, name):
            return 1
        print("\n发好了：" + ("这是测试文件，玩家的 Evo 不读它" if args.test else "玩家下次打开赛事页就能看到"))
        return 0
    except Stop as e:
        print(f"\n❌ {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
