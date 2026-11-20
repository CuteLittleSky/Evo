"""Evo 窗口的"录像"页（2026-11-20 用户要求；录像怎么录见 recorder.py、match_recording.py、docs/录像.md）：整局录像的列表和播放。

列表 /recordings（recordings.html）
- 每局一张卡片：缩略图（录完时截的；没有的话第一次打开时用 ffmpeg 截，存在录像旁边）、时长、传奇、模式和地图、时间，
  对上的那一局战绩（名次徽章、击杀、伤害）和事件数。从新到旧。上面一行：共几个、占多大、上限、"打开文件夹"
- 上次 Evo 崩了拼回来的（名字带"_未完成"，没有 .json）也列出来，标"未完成"
播放 /recordings/watch?v=<年-月/文件名>（recordings_watch.html）
- 播放器（不自动播放）、下面自己画的时间轴：事件标在对应的位置（击倒对手、倒地、被扶起、成盒、被复活、冠军、击杀），断掉的那几段
  画成斜线；点事件从它前 EVENT_LEAD 秒开始放。事件也按时间列在下面（按钮，键盘、读屏软件都能用）
- 网址带 t=秒：从那一刻开始（战绩页以后可以直接跳到某个事件）
- "在资源管理器中显示"、"删除"（先确认；删 MP4 和同名的 .json、.jpg）
录像文件 /recordings/video?v=…（send_file 支持 Range：播放器能拖进度条）、缩略图 /recordings/thumb?v=…
和战绩对上：按时间（.json 里的开始时刻 + 时长），战绩里那一局的结束时刻落在录像里（往后留 MATCH_SLACK）就算同一局。
不改数据库。战绩页有录像的局显示一个小按钮（match_links）
导航上的"录像"：设置里打开了录像、或者已经有录像时才显示（visible）：没打开的玩家看不到这一页（内测中）
这个文件不加载识别用的库。规则见 docs/面板.md"录像页"。
"""

import json
import os
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for

import app_settings
import recorder
import storage
from paths import DATA_DIR

bp = Blueprint("recordings", __name__)
EVENTS = {  # 事件 → (显示名, 样式)：颜色以外还写着名字（时间轴上悬停、下面的列表），不只靠颜色分
    "knocked": ("倒地", "ev-knocked"),
    "revived": ("被扶起", "ev-revived"),
    "dead": ("成盒", "ev-dead"),
    "respawned": ("被复活", "ev-respawned"),
    "knock": ("击倒", "ev-knock"),
    "champion": ("冠军", "ev-champion"),
    "kill": ("击杀", "ev-kill"),
}
EVENT_LEAD = 3  # 点事件时从它前几秒开始放：看得到是怎么发生的
MATCH_SLACK = timedelta(minutes=2)  # 战绩里那一局的结束时刻比录像的结尾晚这么多以内，也算同一局（结算画面上才记下）
MODES = {"ranked": "排位", "pubs": "匹配", "other": "其他"}
DEFAULT_LIMIT_GB = 30


def folder():
    """录像文件夹：设置里选的，没选是数据文件夹里的 recordings。"""
    return Path(app_settings.get("recording_dir") or DATA_DIR / "recordings")


def visible():
    """导航上显示"录像"吗：设置里打开了录像，或者文件夹里已经有录像。"""
    return bool(app_settings.get("recording_enabled")) or any(_videos(folder()))


def _videos(base):
    if not base.is_dir():
        return []
    return [v for v in base.glob("*/*.mp4") if v.parent.name != recorder.WORK_DIR_NAME]


def clock(seconds):
    """4:02、1:02:03。"""
    seconds = max(0, int(seconds or 0))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def size_text(n):
    return f"{n / 2 ** 30:.1f} GB" if n >= 2 ** 30 else f"{n / 2 ** 20:.0f} MB"


def when_text(dt):
    if dt.date() == date.today():
        return f"今天 {dt:%H:%M}"
    if dt.date() == date.today() - timedelta(days=1):
        return f"昨天 {dt:%H:%M}"
    return f"{dt:%m-%d %H:%M}"


def _started(video, meta):
    if meta.get("started"):
        try:
            return datetime.fromisoformat(meta["started"])
        except ValueError:
            pass
    try:
        return datetime.strptime(video.stem[:17], "%Y-%m-%d_%H%M%S")
    except ValueError:
        return datetime.fromtimestamp(video.stat().st_mtime)


def load(video):
    """一个录像的信息（字典）。"""
    meta = {}
    sidecar = video.with_suffix(".json")
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    started = _started(video, meta)
    duration = meta.get("duration") or recorder.mp4_duration(video) or 0
    events = []
    for e in meta.get("events", []):
        if e.get("kind") in EVENTS and isinstance(e.get("t"), (int, float)):
            label, css = EVENTS[e["kind"]]
            events.append({"t": e["t"], "clock": clock(e["t"]), "label": label, "css": css,
                           "left": min(100.0, max(0.0, e["t"] / duration * 100)) if duration else 0})
    outages = [{"left": a / duration * 100, "width": max(0.4, (b - a) / duration * 100), "seconds": b - a}
               for a, b in meta.get("outages", []) if duration and b > a]
    legend = (meta.get("match") or {}).get("legend")
    return {
        "id": f"{video.parent.name}/{video.stem}", "path": video, "started": started,
        "end": started + timedelta(seconds=(meta.get("first_frame") or 0) + duration),
        "when": when_text(started), "duration": duration, "clock": clock(duration),
        "size": video.stat().st_size, "size_text": size_text(video.stat().st_size),
        "legend": legend, "title": legend or "录像", "unfinished": "_未完成" in video.stem,
        "events": events, "outages": outages, "match": None,
    }


def entries():
    """全部录像，从新到旧。"""
    return sorted((load(v) for v in _videos(folder())), key=lambda r: r["started"], reverse=True)


def _db():
    if "db" not in g:
        g.db = storage.connect(current_app.config["DB"])  # panel_app 在请求结束时关掉
    return g.db


def attach_matches(items):
    """给每个录像找战绩里的那一局（结束时刻落在录像里，往后留 MATCH_SLACK），找到的放进 item["match"]。"""
    if not items:
        return items
    lo = min(r["started"] for r in items)
    hi = max(r["end"] for r in items) + MATCH_SLACK
    rows = _db().execute(
        "SELECT match_id, placement, kills, damage, mode, map, legend, ended_at FROM matches "
        "WHERE ended_at BETWEEN ? AND ? ORDER BY ended_at",
        (f"{lo:%Y-%m-%d %H:%M:%S}", f"{hi:%Y-%m-%d %H:%M:%S}")).fetchall()
    for r in items:
        best = None
        for m in rows:
            ended = datetime.strptime(m["ended_at"], "%Y-%m-%d %H:%M:%S")
            if r["started"] <= ended <= r["end"] + MATCH_SLACK:
                gap = abs((ended - r["end"]).total_seconds())
                if best is None or gap < best[0]:
                    best = (gap, m)
        if best:
            m = best[1]
            r["match"] = {"id": m["match_id"], "placement": m["placement"], "kills": m["kills"],
                          "damage": m["damage"], "mode": MODES.get(m["mode"]), "map": m["map"]}
            r["legend"] = r["legend"] or m["legend"]
            r["title"] = r["legend"] or "录像"
    return items


def match_links(rows):
    """战绩页：{战绩编号: 录像的 id}（有录像的局）。没打开录像、也没有录像时不扫文件夹，返回 {}。"""
    if not visible():
        return {}
    links = {}
    for r in attach_matches(entries()):
        if r["match"]:
            links[r["match"]["id"]] = r["id"]
    return {row["match_id"]: links[row["match_id"]] for row in rows if row["match_id"] in links}


def resolve(rid):
    """网址里的录像 id（年-月/文件名）→ MP4 的路径；不在录像文件夹里、不存在都返回 None（不让网址指到别的文件）。"""
    if not rid or "\\" in rid or rid.count("/") != 1:
        return None
    base = folder().resolve()
    video = (base / f"{rid}.mp4").resolve()
    if video.parent.parent != base or video.parent.name == recorder.WORK_DIR_NAME or not video.is_file():
        return None
    return video


def _badge(placement):
    if placement is None:
        return "p-none"
    return "p1" if placement == 1 else "p2" if placement <= 3 else "p3" if placement <= 10 else "p4"


def _decorate(r):
    """模板要用的几项：传奇小头像、名次徽章、副标题。"""
    from panel_app import LEGEND_ICONS  # panel_app 导入本文件，放在这里才不会互相导入
    m = r["match"]
    r["legend_icon"] = LEGEND_ICONS.get(r["legend"])
    r["badge"] = _badge(m["placement"]) if m else None
    r["subtitle"] = " · ".join(x for x in ((m or {}).get("mode"), (m or {}).get("map"), r["when"]) if x)
    return r


@bp.route("/recordings")
def page():
    items = [_decorate(r) for r in attach_matches(entries())]
    total = sum(r["size"] for r in items)
    limit = float(app_settings.get("recording_limit_gb", DEFAULT_LIMIT_GB))
    return render_template("recordings.html", items=items, total=size_text(total), limit=f"{limit:g} GB",
                           enabled=bool(app_settings.get("recording_enabled")))


@bp.route("/recordings/watch")
def watch():
    video = resolve(request.args.get("v"))
    if video is None:
        flash("找不到这个录像（可能已经删掉了）。", "error")
        return redirect(url_for("recordings.page"))
    item = _decorate(attach_matches([load(video)])[0])
    start = request.args.get("t", type=float)
    return render_template("recordings_watch.html", item=item, lead=EVENT_LEAD,
                           start=start if start is not None and 0 <= start < item["duration"] else None)


@bp.route("/recordings/video")
def video():
    path = resolve(request.args.get("v"))
    if path is None:
        abort(404)
    return send_file(path, mimetype="video/mp4", conditional=True, max_age=0)  # conditional：支持 Range，能拖进度条


@bp.route("/recordings/thumb")
def thumb():
    path = resolve(request.args.get("v"))
    if path is None:
        abort(404)
    jpg = path.with_suffix(".jpg")
    if not jpg.exists():  # 录完时没截成（或者是以前的录像）：现在截一张存着
        ffmpeg = recorder.find_ffmpeg(app_settings.get("recording_ffmpeg"), DATA_DIR)
        item = load(path)
        if not ffmpeg or not recorder.make_thumbnail(
                ffmpeg, path, recorder.thumbnail_time([{"t": e["t"]} for e in item["events"]], item["duration"])):
            abort(404)
    return send_file(jpg, mimetype="image/jpeg", max_age=0)


@bp.post("/recordings/delete")
def delete():
    path = resolve(request.form.get("v"))
    if path is None:
        flash("找不到这个录像（可能已经删掉了）。", "error")
        return redirect(url_for("recordings.page"))
    try:
        path.unlink()
    except OSError:
        flash("删不掉：这个录像可能正在播放，关掉播放器再删。", "error")
        return redirect(url_for("recordings.watch", v=request.form.get("v")))
    for extra in (path.with_suffix(".json"), path.with_suffix(".jpg")):
        extra.unlink(missing_ok=True)
    flash(f"删掉了 {path.name}", "ok")
    return redirect(url_for("recordings.page"))


@bp.post("/recordings/show")
def show():
    """在资源管理器里打开录像文件夹；带 v 时选中那个录像。"""
    path = resolve(request.form.get("v")) if request.form.get("v") else None
    base = folder()
    base.mkdir(parents=True, exist_ok=True)
    if path is not None:
        subprocess.Popen(["explorer", f"/select,{path}"])
    else:
        os.startfile(base)
    back = request.form.get("next") or url_for("recordings.page")
    return redirect(back if back.startswith("/recordings") else url_for("recordings.page"))
