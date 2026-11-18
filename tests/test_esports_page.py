"""Evo 窗口的"赛事"页面（panel_esports.py、esports.html）。不联网：赛事文件是临时文件夹里的一个文件，查开播状态换成假的。

- 导航里有"赛事"，在"赛季"和"设置"之间；这一页打开时"赛事"是选中的
- 三块：正在进行（卡片：进行中、类型、开始时间、名称、阶段和队伍数、"看直播"按钮）、即将开始（先开始的在前，
  每行有"看名单和直播"按钮：没开始也能点进去）、最近结束（"已结束"，不能点，超过 7 天的不显示）；最下面一行小字
- 没有正在进行的：写下一场什么时候开始；即将开始、最近结束没有就不显示那一块
- 没读到：一次都没读到过写"没有读到"；读到过、这次没连上，显示上次的并说明
- 几位选手在播（GET /esports/live）：只查正在进行的赛事名单里的选手；60 秒内再问不重查；没查到的单独数
- 赛程里的字只当纯文字显示；"看直播"能点进去（看直播页还没做，是占位的页面）、点回来；没有这场赛事时回到赛事页

运行：python tests/test_esports_page.py
"""

import json
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import esports_data
import live_status
import panel_app
import panel_esports
import storage

NOW = datetime.now().astimezone().replace(microsecond=0)


def event(eid, start, hours=4, name="示例联赛", kind="联赛", stage="A 组对 B 组", teams=("甲队", "乙队")):
    return {"id": eid, "name": name, "type": kind, "stage": stage, "start": start.isoformat(),
            "end": (start + timedelta(hours=hours)).isoformat(), "teams": list(teams)}


def roster():
    out = []
    for team, platform in (("甲队", "douyu"), ("乙队", "bilibili"), ("丙队", "huya")):
        out += [{"id": f"{platform}-{n}", "name": f"{team}{n}号", "team": team, "platform": platform, "room": str(n)}
                for n in (1, 2, 3)]
    return out


def serve(folder, events, players=None):
    """把赛事文件写到临时文件夹，让 Evo 读它；清掉上次存下的和查过的开播状态。"""
    path = Path(folder) / "esports.json"
    path.write_text(json.dumps({"version": 1, "updated": NOW.isoformat(), "players": roster() if players is None else players,
                                "events": events}), encoding="utf-8")
    esports_data.URL = str(path)
    esports_data.CACHE_PATH.unlink(missing_ok=True)
    panel_esports._status.update(at=0.0, found={})
    return path


def text(html):
    return " ".join(re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", html, flags=re.S)).split())


def section(html, heading):
    found = re.findall(rf'<section class="es-section"[^>]*>\s*<h2[^>]*>{heading}</h2>.*?</section>', html, re.S)
    return found[0] if found else ""


def main():
    print("赛事页（导航、三块、没读到、几位选手在播）")
    out = {}
    folder = tempfile.mkdtemp(prefix="evo-esports-")
    db = Path(folder) / "evo.db"
    storage.connect(db).close()
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()

    events = [event("live1", NOW - timedelta(hours=1), name="职业联赛 第 3 周"),
              event("live2", NOW - timedelta(minutes=5), name="晚间训练赛", kind="训练赛", stage="", teams=("乙队",)),
              event("soon2", NOW + timedelta(days=3), name="三天后的杯赛", kind="杯赛", stage="决赛"),
              event("soon1", NOW + timedelta(hours=2), name="<b>两小时后</b>的训练赛", kind="训练赛"),
              event("done", NOW - timedelta(days=1, hours=5), name="昨天的联赛"),
              event("old", NOW - timedelta(days=9), name="上上周的联赛")]
    serve(folder, events)
    raw = client.get("/esports").get_data(as_text=True)
    nav = re.findall(r'<nav class="nav".*?</nav>', raw, re.S)[0]
    out["导航里有\"赛事\"，在\"赛季\"和\"设置\"之间，这一页打开时是选中的"] = (
        text(nav).split() == ["总览", "战绩", "数据", "赛季", "赛事", "设置"]
        and 'class="active"' in re.findall(r'<a href="/esports"[^>]*>', nav)[0])
    live, upcoming, recent = (section(raw, h) for h in ("正在进行", "即将开始", "最近结束"))
    cards = re.findall(r"<article.*?</article>", live, re.S)
    first = text(cards[0]) if cards else ""
    start = NOW - timedelta(hours=1)
    out["正在进行：每场一张卡片，写着进行中、类型、开始时间、名称、阶段和队伍数，有\"看直播\"按钮"] = (
        len(cards) == 2 and all(word in first for word in ("进行中", "联赛", "职业联赛 第 3 周", "A 组对 B 组，2 支队伍", "看直播"))
        and f"{panel_esports.day_text(start, NOW)} {start:%H:%M} 开始" in first
        and 'href="/esports/live1/watch"' in cards[0] and "1 支队伍" in text(cards[1]) and "，1 支队伍" not in text(cards[1]))
    rows = [text(r) for r in re.findall(r"<li.*?</li>", upcoming, re.S)]
    out["即将开始：一行一场，先开始的在前，写日期、钟点、名称、阶段、队伍数"] = (
        len(rows) == 2 and "两小时后" in rows[0] and "三天后的杯赛" in rows[1] and "杯赛 · 决赛" in rows[1]
        and all("2 支队伍" in r for r in rows) and f"{NOW + timedelta(days=3):%m-%d} 周" in rows[1])
    links = re.findall(r'<a class="btn es-row-btn" href="([^"]+)"\s+aria-label="([^"]+)">([^<]+)</a>', upcoming)
    soon3 = NOW + timedelta(days=3)
    out["即将开始的也能点进去看名单和直播（没开始也能看选手赛前的直播）；按钮念出来带赛事名和时间；最近结束的不能点"] = (
        [(href, word) for href, _, word in links] == [("/esports/soon1/watch", "看名单和直播"), ("/esports/soon2/watch", "看名单和直播")]
        and links[1][1] == f"看名单和直播：三天后的杯赛，{soon3:%m-%d} {soon3:%H:%M} 开始"
        and "开始后可以看直播" not in raw and "<a " not in recent
        and client.get("/esports/soon1/watch").status_code == 200)
    ended = [text(r) for r in re.findall(r"<li.*?</li>", recent, re.S)]
    out["最近结束：灰色，写\"已结束\"；结束超过 7 天的不显示"] = (
        len(ended) == 1 and "昨天的联赛" in ended[0] and "已结束" in ended[0] and "es-ended" in recent
        and "上上周" not in raw)
    out["最下面一行小字；赛程里的字只当纯文字显示"] = (
        "赛事、队伍和每位选手的直播间由 Evo 统一维护。" in text(raw) and "<b>两小时后</b>" not in raw
        and "&lt;b&gt;两小时后&lt;/b&gt;" in raw)

    asked = []

    def fake_query(players):
        asked.append([p["id"] for p in players])
        states = {"douyu-1": "live", "douyu-2": "live", "douyu-3": "offline", "bilibili-1": "live", "bilibili-2": "error"}
        return {p["id"]: live_status.result(states.get(p["id"], "offline")) for p in players}

    real_query, live_status.query = live_status.query, fake_query
    try:
        counts = client.get("/esports/live").get_json()
        again = client.get("/esports/live").get_json()
        out["几位选手在播：只查正在进行的赛事名单里的选手（同一个人只查一次），没查到的单独数"] = (
            counts == {"events": {"live1": {"live": 3, "total": 6, "unknown": 1}, "live2": {"live": 1, "total": 3, "unknown": 1}}}
            and len(asked) == 1 and sorted(asked[0]) == sorted(f"{p}-{n}" for p in ("douyu", "bilibili") for n in (1, 2, 3)))
        out["60 秒内再问不重查"] = again == counts and len(asked) == 1
        panel_esports._status["at"] -= 61
        client.get("/esports/live")
        out["过了 60 秒再问会重查"] = len(asked) == 2
    finally:
        live_status.query = real_query
    out["页面上给\"几位选手在播\"留了位置（先写\"正在看谁在播…\"），由脚本问 /esports/live 填上"] = (
        raw.count('data-es-count="live') == 2 and "正在看谁在播…" in first and 'fetch("/esports/live"' in raw)

    watch = client.get("/esports/live1/watch")
    gone = client.get("/esports/no-such/watch")
    out["\"看直播\"能点进去、点回来；没有这场赛事时回到赛事页并说明"] = (
        watch.status_code == 200 and "职业联赛 第 3 周" in watch.get_data(as_text=True)
        and 'href="/esports"' in watch.get_data(as_text=True)
        and gone.status_code == 302 and gone.headers["Location"].endswith("/esports")
        and "没有找到这场赛事" in client.get("/esports").get_data(as_text=True))

    serve(folder, [event("soon", NOW + timedelta(hours=3))])
    quiet = client.get("/esports").get_data(as_text=True)
    soon = NOW + timedelta(hours=3)
    out["没有正在进行的：写下一场什么时候开始；没有最近结束的就不显示那一块，也不问谁在播"] = (
        f"现在没有正在进行的赛事，下一场 {panel_esports.day_text(soon, NOW)} {soon:%H:%M} 开始。" in text(quiet)
        and section(quiet, "即将开始") and not section(quiet, "最近结束") and "/esports/live" not in quiet)

    esports_data.URL = str(Path(folder) / "没有这个文件.json")
    stale = text(client.get("/esports").get_data(as_text=True))  # 60 秒内：还用刚才存下的，不算没连上
    cache = json.loads(esports_data.CACHE_PATH.read_text(encoding="utf-8"))
    cache["fetched_at"] -= 3600
    esports_data.CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    offline = text(client.get("/esports").get_data(as_text=True))
    out["读到过、这次没连上：显示上次的赛程，并说明可能不是最新的"] = (
        "这次没连上" not in stale and "这次没连上" in offline and "可能不是最新的" in offline and "示例联赛" in offline)
    esports_data.CACHE_PATH.unlink()
    nothing = text(client.get("/esports").get_data(as_text=True))
    out["一次都没读到过：写\"赛事信息没有读到\"和\"再试一次\"，不显示三块"] = (
        "赛事信息没有读到" in nothing and "再试一次" in nothing and "正在进行" not in nothing
        and client.get("/esports/live").get_json() == {"events": {}})
    return report(out)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
