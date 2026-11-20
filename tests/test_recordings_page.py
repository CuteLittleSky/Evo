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
- 设置页"录像"一栏（假的 ffmpeg 和编码器检测，这台电脑"编不了 AV1"）：关着时只有开关；没有 ffmpeg 打不开；
  打开时检测编码器、记住；画质四项一起保存，选了编不了的 AV1、乱填的不保存；估一局多大；硬盘上限的范围；
  保存位置（不是完整路径、在程序文件夹里的不行，填的留在输入框里；换了以后说原来的录像还在哪；改回默认）；关掉

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
import recorder
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
    print("录像页（列表、播放、删除、战绩页的看录像按钮；设置页的\"录像\"一栏）")
    try:
        return checks()
    finally:  # 全部测试在同一个临时数据文件夹里跑：留下录像、开着录像，后面几组的导航会多出"录像"
        app_settings.set_value("recording_dir", None)
        shutil.rmtree(panel_recordings.folder(), ignore_errors=True)
        for key in ("recording_enabled", panel_recordings.CHECK_KEY, "recording_size", "recording_fps",
                    "recording_quality", "recording_codec", "recording_limit_gb"):
            app_settings.set_value(key, None)


def section(raw):
    """设置页里"录像"那一栏的 HTML。"""
    found = re.findall(r'<section class="card settings-card" aria-labelledby="h-video">.*?</section>', raw, re.S)
    return found[0] if found else ""


def note(client, path, data, token):
    """提交设置，跟着跳回设置页，返回（"录像"那一栏，那一项下面写的结果）。"""
    r = client.post(path, data={**data, "token": token})
    if r.status_code != 302 or not r.headers["Location"].endswith("/settings#h-video"):
        return "", f"没跳回设置页（{r.status_code}）"
    box = section(client.get("/settings").get_data(as_text=True))
    found = re.findall(r'<div class="setting-desc rec-note-line[^"]*"\s*role="(\w+)" id="rec-note-\w+">([^<]*)</div>', box)
    return box, (f"{found[0][0]}:{found[0][1]}" if found else "")


def settings_checks(client, token, base):
    """设置页的"录像"一栏。base：现在的录像文件夹（里面有一个录像）。"""
    out = {}
    real_find, real_pick = recorder.find_ffmpeg, recorder.pick_encoder
    ffmpeg = {"path": None}
    recorder.find_ffmpeg = lambda *_, **__: ffmpeg["path"]
    tried = []

    def fake_pick(path, options, rules, hwnd=None):  # 这台电脑：NVIDIA，编得了 H.264、编不了 AV1
        tried.append(options.codec)
        return {"h264": "h264_nvenc"}.get(options.codec)
    recorder.pick_encoder = fake_pick
    try:
        app_settings.set_value("recording_enabled", False)
        app_settings.set_value(panel_recordings.CHECK_KEY, None)
        box = section(client.get("/settings").get_data(as_text=True))
        out["关着：有\"录像\"一栏和开关（关着），没有画质、上限、位置；没有 ffmpeg 时开关点不了、写着原因"] = (
            'aria-checked="false"' in box and "rec-size" not in box and "硬盘上限" not in box
            and "找不到录像要用的组件" in box and "disabled" in box)
        box, said = note(client, "/settings/recording", {"on": "1"}, token)
        out[f"没有 ffmpeg 时打不开：还是关着，开关下面写原因（{said}）"] = (
            said.startswith("alert:打不开") and app_settings.get("recording_enabled") is not True)
        r = client.post("/settings/recording", data={"on": "1"})
        out["没有口令不让改"] = r.status_code != 302 and app_settings.get("recording_enabled") is not True

        ffmpeg["path"] = "ffmpeg.exe"
        box, said = note(client, "/settings/recording", {"on": "1"}, token)
        check = app_settings.get(panel_recordings.CHECK_KEY) or {}
        out[f"打开：检测了 H.264、AV1 的编码器、记住了；写着用 NVIDIA 显卡；显示画质、上限、位置（{said}）"] = (
            app_settings.get("recording_enabled") is True and said.startswith("status:打开了")
            and check == {"ffmpeg": "ffmpeg.exe", "h264": "h264_nvenc", "av1": None}
            and "NVIDIA 显卡" in box and 'aria-checked="true"' in box and 'id="rec-size"' in box
            and "硬盘上限" in box and "保存位置" in box)
        tried.clear()
        client.get("/settings")
        out["记住了就不再检测（打开设置页不跑 ffmpeg）"] = tried == []
        codec = re.findall(r'<option value="av1"[^>]*>[^<]*</option>', box)
        out[f"编不了 AV1：那一项不能选、写着原因（{codec}）"] = (
            len(codec) == 1 and "disabled" in codec[0] and "编不了" in codec[0])
        out["默认：原画面、60 帧、高、H.264"] = all(re.search(p, box) for p in (
            r'<option value="native" selected>', r'<option value="60" selected>', r'<option value="high" selected>',
            r'<option value="h264" selected>'))

        rules = recorder.load_rules()
        line = panel_recordings.estimate(rules, (2560, 1440), "1080p", 60, "high", "h264", 30)
        out[f"估一局多大：1080p 60 帧高画质约 12 Mbps、一局 1.7 GB、30 GB 存 17 局（{line}）"] = (
            "1920 × 1080" in line and "约 12 Mbps" in line and "约 1.7 GB" in line and "约 17 局" in line)
        table = json.loads(re.findall(r"data-estimates='([^']*)'", box)[0].replace("&#34;", '"')) if "data-estimates" in box else {}
        out[f"每种组合都先算好了（{len(table)} 种），页面上改选项时换那一行"] = (
            len(table) == len(rules["sizes"]) * len(rules["fps"]) * len(rules["bits_per_pixel"]) * len(rules["encoders"])
            and 'id="rec-estimate"' in box)

        good = {"size": "720p", "fps": "30", "quality": "low", "codec": "h264"}
        box, said = note(client, "/settings/recording/quality", good, token)
        out[f"画质四项一起保存（{said}），页面上选着刚保存的"] = (
            said.startswith("status:保存了") and app_settings.get("recording_size") == "720p"
            and app_settings.get("recording_fps") == 30 and app_settings.get("recording_quality") == "low"
            and '<option value="720p" selected>' in box and '<option value="30" selected>' in box)
        _, said = note(client, "/settings/recording/quality", {**good, "size": "720p", "codec": "av1"}, token)
        out[f"选了编不了的 AV1：不保存（{said}）"] = (
            said.startswith("alert:没保存") and app_settings.get("recording_codec") == "h264")
        bad = [{**good, "size": "4k"}, {**good, "fps": "144"}, {**good, "quality": "ultra"}, {**good, "fps": "abc"}]
        results = [note(client, "/settings/recording/quality", b, token)[1] for b in bad]
        out["乱填的（4k、144 帧、ultra、abc）都不保存"] = (
            all(r.startswith("alert:没保存") for r in results) and app_settings.get("recording_size") == "720p")

        results = {gb: note(client, "/settings/recording/limit", {"gb": gb}, token)[1] for gb in ("0.5", "abc", "20000")}
        out[f"硬盘上限：不到 1、不是数、太大都不保存（{results['0.5']}）"] = (
            all(r.startswith("alert:没保存") for r in results.values()) and app_settings.get("recording_limit_gb") is None)
        box, said = note(client, "/settings/recording/limit", {"gb": "50"}, token)
        out[f"硬盘上限填 50：存成 50（{said}），框里是 50，写着现在几个录像、多大"] = (
            app_settings.get("recording_limit_gb") == 50 and said == "status:保存了" and 'value="50"' in box
            and "2</span> 个录像" in box)

        box, said = note(client, "/settings/recording/folder", {"folder": "录像"}, token)
        out[f"保存位置不是完整路径：不保存，\"更改保存位置\"展开着，填的留在框里，原因写在框下面（{said}）"] = (
            said.startswith("alert:请写完整的路径") and app_settings.get("recording_dir") is None
            and re.search(r'<details class="move-data" id="rec-folder"\s+open>', box) is not None
            and 'name="folder" value="录像"' in box and 'aria-describedby="rec-note-folder"' in box)
        _, said = note(client, "/settings/recording/folder", {"folder": str(panel_recordings.EXE_DIR / "录像")}, token)
        out[f"保存位置在程序文件夹里：不保存（{said}）"] = (
            "程序文件夹" in said and app_settings.get("recording_dir") is None)
        elsewhere = Path(tempfile.mkdtemp(prefix="evo-rec-elsewhere-"))
        box, said = note(client, "/settings/recording/folder", {"folder": f'"{elsewhere}"'}, token)
        out[f"换到别的文件夹：存下（带引号也行），说原来的 2 个录像没搬、还在哪（{said[:60]}…）"] = (
            app_settings.get("recording_dir") == str(elsewhere) and said.startswith("status:保存了")
            and "原来的 2 个录像没有搬" in said and str(base) in said and str(elsewhere) in box
            and "改回默认位置" in box)
        box, said = note(client, "/settings/recording/folder", {"reset": "1"}, token)
        out[f"改回默认位置（{said}）"] = (
            app_settings.get("recording_dir") is None and said.startswith("status:改回默认位置了")
            and "改回默认位置（" not in box)
        shutil.rmtree(elsewhere, ignore_errors=True)

        box, said = note(client, "/settings/recording", {"on": "0"}, token)
        out[f"关掉：只剩开关，说录好的还在（{said}）"] = (
            app_settings.get("recording_enabled") is False and "录好的还在" in said and "rec-size" not in box)
    finally:
        recorder.find_ffmpeg, recorder.pick_encoder = real_find, real_pick
    return out


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
                         events=[{"t": 60, "kind": "knocked"}, {"t": 90, "kind": "dead"}, {"t": 100, "kind": "unknown"},
                                 {"t": 30, "kind": "kill", "kills": 2, "count": 2}],
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
    out[f"列表：从新到旧两张卡片；新的这张有时长 2:00、传奇、对上的战绩（匹配 · 残月、#3、击杀 4、伤害 1,234）、3 个事件（{newest}）"] = (
        len(cards) == 2 and "2:00" in newest and "班加罗尔" in newest and "匹配 · 残月" in newest and "#3" in newest
        and "击杀 4" in newest and "伤害 1,234" in newest and "3 个事件" in newest)
    oldest = text(cards[1]) if len(cards) > 1 else ""
    out["列表：没有 .json 的（上次崩了拼回来的）标\"未完成\"、写\"没对上战绩\""] = "未完成" in oldest and "没对上战绩" in oldest
    out["列表：缩略图按需加载、先占好位置；上面写共多大、上限 30 GB"] = (
        'loading="lazy"' in cards[0] and 'width="480" height="270"' in cards[0] and "上限" in raw and "30 GB" in raw)

    rid = f"{new.parent.name}/{new.stem}"
    raw = client.get("/recordings/watch", query_string={"v": rid, "t": "61.5"}).get_data(as_text=True)
    marks = re.findall(r'<button type="button" class="rec-mark [^"]*"[^>]*>', raw)
    out["播放页：播放器不自动播放、先只读时长；网址带 t= 时从那一刻开始"] = (
        "<video" in raw and "autoplay" not in raw and 'preload="metadata"' in raw and 'data-start="61.5"' in raw)
    out[f"时间轴：认得的事件才标（3 个，按时间），位置按时长算（60 秒在 50%），按钮念得出名字和时刻；一下子两个击杀写'×2'（{marks[:2]}）"] = (
        len(marks) == 3 and "left: 25.000%" in marks[0] and "击杀 ×2，0:30" in marks[0] and "ev-kill" in marks[0]
        and "left: 50.000%" in marks[1] and "倒地，1:00" in marks[1] and "ev-knocked" in marks[1]
        and "left: 75.000%" in marks[2] and "成盒，1:30" in marks[2])
    out["时间轴：断掉的那段（30–36 秒）画出来"] = 'class="rec-gap" style="left: 25.000%; width: 5.000%"' in raw
    events = text("".join(re.findall(r'<ol class="rec-events".*?</ol>', raw, re.S)))
    out[f"事件列表：按时间（{events}）"] = events == "0:30 击杀 ×2 1:00 倒地 1:30 成盒"
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

    out.update(settings_checks(client, token, base))

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
