"""录像页（panel_recordings.py、recordings.html、recordings_watch.html）和战绩页的"看录像"按钮。

不用 ffmpeg：录像文件用几 KB 的假数据，时长写在 .json 里，缩略图用现成的 .jpg。
- 导航：没打开录像、也没有录像时没有"录像"；打开了、或者已经有录像时有，在"战绩"后面
- 列表：从新到旧，每张卡片有缩略图、时长、传奇、模式和地图（对上的战绩里的）、名次、击杀、伤害、事件数；
  没 .json 的（上次崩了拼回来的）标"未完成"；上面写共多大、上限；没有录像时的说明
- 播放页：播放器不自动播放；时间轴上事件标在对应位置（时长 120 秒、60 秒处的倒地在 50%），每个是按钮、念得出名字和时刻；
  断掉的那段画出来；下面的事件列表；对上的战绩能点过去；网址带 t= 从那一刻开始；删除要先确认
- 录像文件支持拖动（Range 请求回 206）；缩略图；网址不能指到录像文件夹以外、分段文件夹里的文件
- 删除：MP4、.json、.jpg 一起删；没有口令不让删
- 战绩页：有录像的局多一个"看录像"按钮，指到那个录像；没录像的局没有

运行：python tests/test_recordings_page.py
"""

import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from fake_game import report  # 要在项目代码之前导入（设置临时数据文件夹）

import app_settings
import panel_app
import panel_recordings
import storage

NOW = datetime.now().replace(microsecond=0)


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def nav_labels(raw):
    nav = re.findall(r'<nav class="nav".*?</nav>', raw, re.S)[0]
    return text(nav).split()


def make_recording(base, started, name, duration, events=(), outages=(), legend="班加罗尔", sidecar=True):
    month = base / started.strftime("%Y-%m")
    month.mkdir(parents=True, exist_ok=True)
    video = month / f"{started:%Y-%m-%d_%H%M%S}_{name}.mp4"
    video.write_bytes(bytes(range(256)) * 16)  # 4 KB 的假录像
    video.with_suffix(".jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    if sidecar:
        video.with_suffix(".json").write_text(json.dumps({
            "version": 1, "video": video.name, "started": started.isoformat(timespec="seconds"),
            "first_frame": 0.4, "duration": duration, "match": {"legend": legend} if legend else {},
            "events": list(events), "outages": list(outages), "hud_changes": []}, ensure_ascii=False), encoding="utf-8")
    return video


def main():
    print("录像页（列表、播放、删除、战绩页的看录像按钮）")
    try:
        return checks()
    finally:  # 全部测试在同一个临时数据文件夹里跑：留下录像、开着录像，后面几组的导航会多出"录像"
        shutil.rmtree(panel_recordings.folder(), ignore_errors=True)
        app_settings.set_value("recording_enabled", False)


def checks():
    out = {}
    folder = Path(tempfile.mkdtemp(prefix="evo-recordings-"))
    db = folder / "evo.db"
    conn = storage.connect(db)
    panel_app.app.config["DB"] = str(db)
    client = panel_app.app.test_client()
    token = panel_app.app.config["TOKEN"]
    base = panel_recordings.folder()

    # 没打开录像、也没有录像：导航里没有
    app_settings.set_value("recording_enabled", False)
    raw = client.get("/matches").get_data(as_text=True)
    out["没打开录像、也没有录像：导航里没有\"录像\"，战绩页不扫录像文件夹"] = (
        "录像" not in nav_labels(raw) and panel_recordings.match_links([]) == {})
    app_settings.set_value("recording_enabled", True)
    raw = client.get("/recordings").get_data(as_text=True)
    out["打开了录像：导航里\"录像\"在\"战绩\"后面，选中；还没有录像时写说明"] = (
        nav_labels(raw)[:3] == ["总览", "战绩", "录像"] and 'aria-current="page"' in re.findall(
            r'<a href="/recordings"[^>]*>', raw)[0] and "还没有录像" in raw)

    # 两局录像：新的那局对得上战绩，旧的那局是上次崩了拼回来的（没有 .json）
    first = NOW - timedelta(hours=3)
    second = NOW - timedelta(minutes=30)
    old = make_recording(base, first, "未完成", 0, sidecar=False)
    new = make_recording(base, second, "班加罗尔", 120,
                         events=[{"t": 60, "kind": "knocked"}, {"t": 90, "kind": "dead"}, {"t": 100, "kind": "unknown"}],
                         outages=[[30, 36]])
    linked = storage.insert_match(conn, {"player_name": "Me", "ended_at": f"{second + timedelta(seconds=118):%Y-%m-%d %H:%M:%S}",
                                         "placement": 3, "kills": 4, "damage": 1234, "mode": "pubs", "map": "残月",
                                         "legend": "班加罗尔"})
    other = storage.insert_match(conn, {"player_name": "Me", "ended_at": f"{NOW - timedelta(days=2):%Y-%m-%d %H:%M:%S}",
                                        "placement": 9, "legend": "恶灵"})
    app_settings.set_value("recording_enabled", False)
    raw = client.get("/recordings").get_data(as_text=True)
    cards = re.findall(r'<article class="rec-card".*?</article>', raw, re.S)
    out["没打开录像但已经有录像：导航里照样有\"录像\""] = "录像" in nav_labels(raw)
    newest = text(cards[0]) if cards else ""
    out[f"列表：从新到旧两张卡片；新的这张有时长 2:00、传奇、对上的战绩（匹配 · 残月、#3、击杀 4、伤害 1,234）、2 个事件（{newest}）"] = (
        len(cards) == 2 and "2:00" in newest and "班加罗尔" in newest and "匹配 · 残月" in newest and "#3" in newest
        and "击杀 4" in newest and "伤害 1,234" in newest and "2 个事件" in newest)
    oldest = text(cards[1]) if len(cards) > 1 else ""
    out["列表：没有 .json 的（上次崩了拼回来的）标\"未完成\"、写\"没对上战绩\""] = "未完成" in oldest and "没对上战绩" in oldest
    out["列表：缩略图按需加载、先占好位置；上面写共多大、上限 30 GB"] = (
        'loading="lazy"' in cards[0] and 'width="480" height="270"' in cards[0] and "上限" in raw and "30 GB" in raw)

    rid = f"{new.parent.name}/{new.stem}"
    raw = client.get("/recordings/watch", query_string={"v": rid, "t": "61.5"}).get_data(as_text=True)
    marks = re.findall(r'<button type="button" class="rec-mark [^"]*"[^>]*>', raw)
    out["播放页：播放器不自动播放、先只读时长；网址带 t= 时从那一刻开始"] = (
        "<video" in raw and "autoplay" not in raw and 'preload="metadata"' in raw and 'data-start="61.5"' in raw)
    out[f"时间轴：认得的事件才标（2 个），位置按时长算（60 秒在 50%），按钮念得出名字和时刻（{marks[:1]}）"] = (
        len(marks) == 2 and "left: 50.000%" in marks[0] and "倒地，1:00" in marks[0] and "ev-knocked" in marks[0]
        and "left: 75.000%" in marks[1] and "成盒，1:30" in marks[1])
    out["时间轴：断掉的那段（30–36 秒）画出来"] = 'class="rec-gap" style="left: 25.000%; width: 5.000%"' in raw
    events = text("".join(re.findall(r'<ol class="rec-events".*?</ol>', raw, re.S)))
    out[f"事件列表：按时间（{events}）"] = events == "1:00 倒地 1:30 成盒"
    out["对上的战绩：名次、击杀、伤害，点了去战绩页展开那一局"] = (
        f'href="/matches?open={linked}"' in raw and "在战绩里看" in raw)
    out["删除要先确认（写着文件名、多大、不能恢复）"] = "data-confirm" in raw and new.name in raw and "不能恢复" in raw

    r = client.get("/recordings/video", query_string={"v": rid}, headers={"Range": "bytes=0-9"})
    out[f"录像文件支持拖动：Range 请求回 206 和那一段（{r.status_code}，{len(r.data)} 字节）"] = (
        r.status_code == 206 and r.data == bytes(range(10)) and r.headers.get("Content-Type", "").startswith("video/mp4"))
    r = client.get("/recordings/thumb", query_string={"v": rid})
    out["缩略图：录像旁边的 .jpg"] = r.status_code == 200 and r.data.startswith(b"\xff\xd8")
    work = base / "_recording" / "x"
    work.mkdir(parents=True, exist_ok=True)
    (work / "part.mp4").write_bytes(b"0")
    outside = folder / "secret.mp4"
    outside.write_bytes(b"0")
    bad = ["../secret", "..\\..\\secret", "_recording/part", "2026-11/../../secret", rid + "/x", ""]
    codes = [client.get("/recordings/video", query_string={"v": b}).status_code for b in bad]
    watch_bad = client.get("/recordings/watch", query_string={"v": "../secret"})
    out[f"网址不能指到录像文件夹以外、分段文件夹里的文件（{codes}）"] = (
        all(c == 404 for c in codes) and watch_bad.status_code == 302)

    raw = client.get("/matches").get_data(as_text=True)
    rows = {int(mid): row for mid, row in re.findall(r'<tr id="m(\d+)"(.*?)</tr>', raw, re.S)}  # 每一局那一行
    link = re.findall(r'class="rec-link" href="([^"]+)"', rows.get(linked, ""))
    out["战绩页：有录像的局多一个\"看录像\"按钮，指到那个录像；没录像的局没有"] = (
        len(link) == 1 and link[0].startswith("/recordings/watch?v=") and "rec-link" not in rows.get(other, "-")
        and "看第" in rows.get(linked, ""))

    r = client.post("/recordings/delete", data={"v": rid})
    still = new.exists()
    r = client.post("/recordings/delete", data={"v": rid, "token": token})
    out["删除：没有口令不让删；有口令时 MP4、.json、.jpg 一起删，回到列表"] = (
        still and r.status_code == 302 and r.headers["Location"].endswith("/recordings") and not new.exists()
        and not new.with_suffix(".json").exists() and not new.with_suffix(".jpg").exists() and old.exists())
    conn.close()
    return report(out)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
