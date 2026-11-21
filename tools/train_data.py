"""训练小模型的素材（开发工具）：从截图、视频里裁出对局中右上角的数字小块，用现在的引擎先读一遍，按规则分成"规则核对过""要人看"两堆。

第一个小模型读数字（见 claude/todo.md"哪些识别换成自己训练的小模型"），先做右上角的六项：剩余小队、击杀、助攻、老板分、伤害、本局 RP。
- 考卷（exam）：我们自己的真实截图（captures/ 里的，和数据文件夹里留着的对局截图），训练时一张都不用；答案要用户全部核对
- 训练素材（video）：captures/videos/ 里的视频（用户从 B站、YouTube 下的），和 Evo 自己的录像（数据文件夹的 recordings/）
找位置照 Evo 的做法（hud.py）：先认左下角自己的传奇（认得出 = 自己的界面），按画面比例（game_aspect.AspectGuess 认）
裁右上角，伤害、击杀那一行先找图标。每一项只在和上一次存的不一样时才存一块，同一个读数最多留 KEEP_SAME 块。
分堆（assign_piles）：引擎读出来、把握够（AUTO_SCORE）、和前后几块对得上（伤害、击杀……只增不减，剩余小队只减不增）的
进"规则核对过"（auto），其余进"要人看"（check），多出来的同一个读数进 extra（不用看）。

都放在 captures/train/（不进 git）：train.db 记每一块（从哪来、第几秒、哪一项、引擎读的、答案），crops/ 是小块原图（原分辨率、彩色）。

  python tools/train_data.py exam        出考卷（裁过的截图文件夹跳过），最后量引擎读一块多快、占多少内存
  python tools/train_data.py videos      切视频（切过的跳过）；-j 几个同时切（默认 4）
  python tools/train_data.py status      各堆多少块、核对了多少
核对答案、看考卷成绩：python tools/train_web.py
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import code_path  # noqa: F401  （把 code/ 加进 import 的路径）
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
TRAIN = ROOT / "captures" / "train"
CROPS = TRAIN / "crops"
VIDEOS = ROOT / "captures" / "videos"
DB_PATH = TRAIN / "train.db"

FIELDS = ("squads_left", "kills", "assists", "participation", "damage", "rp_delta")
LABELS = {"squads_left": "剩余小队", "kills": "击杀", "assists": "助攻", "participation": "老板分",
          "damage": "伤害", "rp_delta": "RP"}
KP_ROW = ("kills", "assists", "participation")
FALLING = {"squads_left"}  # 一局里只减不增；别的几项只增不减（RP：入场费扣完以后只会加）

# 考卷从这些地方的截图里裁（每个放图片的文件夹算一个来源）。rec_probe 是录像实验、videos 是训练素材，都不进考卷
EXAM_DIRS = ("frames", "s31", "aspect", "testers", "live", "samples")
IMAGE_EXT = {".png", ".jpg", ".jpeg"}
VIDEO_EXT = {".mp4", ".mkv", ".flv", ".webm", ".mov", ".avi", ".ts", ".m4v"}

EVERY = 0.3        # 视频每隔几秒看一帧（和记录程序对局中一样）
CHANGE = 3.0       # 和上一次存的那块比，平均差多少才算变了（和 config/hud.json 的 change 一样）
KEEP_SAME = 3      # 同一个来源、同一项、引擎读出同一个数：最多留几块（挑时间上隔得开的）
KEEP_NONE = 30     # 引擎读不出的：最多留几块（难的样本，但一个视频别太多）
AUTO_SCORE = 0.9   # 进"规则核对过"要的把握
GAP = 90           # 前后两块隔了这么多秒，算不是同一局（规则核对不跨局）
VOTES = 30         # 认画面比例时看几帧
UPSCALE, PAD = 3, 12  # 引擎读之前放大、加边（和 config/hud.json 的 upscale、pad 一样）

SCHEMA = """
create table if not exists sources (
  id integer primary key,
  kind text not null,              -- exam 考卷 / video 训练素材
  path text not null unique,       -- 截图文件夹或视频文件
  name text,
  width integer, height integer, fps real, seconds real, frames integer,
  aspect text,                     -- 认出来的画面比例
  status text,                     -- running / done / skipped / error
  note text,
  made_at text
);
create table if not exists crops (
  id integer primary key,
  source integer not null,
  kind text not null,
  field text not null,
  t real,                          -- 视频：第几秒；截图：文件名里的时刻（一天里第几秒），没有就是第几张
  frame text,                      -- 截图的文件名（视频是空的）
  bx real, by real, bw real, bh real,  -- 这一块在 1920x1080 基准上的位置
  img text not null,               -- 相对 crops/ 的路径
  w integer, h integer,            -- 小块原图多大
  engine_text text,                -- 引擎读到的所有文字（| 隔开）
  engine_score real,
  engine_value text,               -- Evo 会采用的读数（格式、范围、把握都过了），读不出是空
  engine_ms real,
  pile text,                       -- auto 规则核对过 / check 要人看 / extra 多出来的同一个读数
  answer text,                     -- 用户核对过的答案；'' = 这一块里没有数字
  status text not null default 'todo',  -- todo 没核对 / done 核对过 / skip 丢掉（挡住了、裁歪了）
  checked_at text
);
create index if not exists crops_source on crops(source);
create index if not exists crops_queue on crops(kind, pile, status, field);
create table if not exists meta (key text primary key, value text);
create table if not exists train_runs (  -- 每一次训练（tools/train_digits.py）
  id integer primary key, started text, finished text,
  status text,                     -- running / done / error
  device text, params text, epochs integer, best_epoch integer,
  threshold real,                  -- 把握的门槛（在验证用的视频上挑的）
  val_right integer, val_wrong integer, val_missed integer, val_n integer,
  model_path text, size_kb real,   -- 导出的 ONNX（相对 captures/train/）
  model_ms real, model_mem_mb real,  -- 考卷上读一块几毫秒、加载以后多占多少内存（onnxruntime，1 个线程）
  note text
);
create table if not exists train_epochs (
  run integer, epoch integer, loss real, val_acc real, val_wrong integer, val_missed integer, val_n integer,
  seconds real, primary key (run, epoch)
);
create table if not exists model_reads (  -- 小模型在考卷上读的
  run integer, crop integer, text text, score real,
  value text,                      -- Evo 会采用的读数（格式、范围、把握都过了），没有是空
  ms real, primary key (run, crop)
);
"""


def connect(path=None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode=wal")
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("pragma table_info(crops)")}
    for name, kind in REVIEW_COLUMNS:  # 后来加的列（复核）：老的数据库补上
        if name not in have:
            conn.execute(f"alter table crops add column {name} {kind}")
    return conn


# 复核（tools/train_digits.py 的 mark_disputes 挑、网页"复核"页填）：小模型读的和答案对不上的块，不预先填数、让用户照图重填
REVIEW_COLUMNS = (
    ("review", "text"),         # 要复核：小模型读出的字（挑出来时）；没被挑出来是空
    ("review_run", "integer"),  # 第几次训练的小模型挑出来的
    ("reviewed_at", "text"),    # 复核过的时刻
    ("answer_before", "text"),  # 复核以前的答案（核对过的是用户的答案，没核对过的是引擎的读数 / 借来的答案）
)


def number(text):
    """答案、读数 → 整数（"+19" → 19）；不是数字返回 None。"""
    try:
        return int(str(text).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


# ---------- 在一张截图上找位置、裁小块（每个工作进程各有一套） ----------

_W = {}


def _tools():
    if not _W:
        from game_aspect import AspectGuess
        from hud import Hud
        from legends import LegendDetector

        legends = LegendDetector()
        _W["hud"] = Hud(engine=None, legends=legends)
        _W["guess"] = AspectGuess(legends, lambda: [])  # 只看左下角的传奇投票（对局中的画面）
        _W["engine"] = None
    return _W


def _engine():
    w = _tools()
    if w["engine"] is None:
        from ocr import SMALL_CROP_PARAMS, create_engine
        w["engine"] = create_engine(SMALL_CROP_PARAMS)
    return w["engine"]


def cells(hud, snip):
    """右上角这一块里每一项数字的位置（基准坐标 [x, y, 宽, 高]）：({项: roi}, 找到了图标没有)。找法和
    hud.Hud.read_top 一样：伤害、击杀那一行先找图标，只取图标右边。剩余小队、RP 位置固定，总是给出来；
    图标一个都没找到时（开局还没打出伤害，或者网上的视频有摄像头盖住了右上角）由 Slicer 决定留不留。"""
    from hud import best_match

    top = hud.cfg["top_right"]
    found = {}
    d = top["damage"]
    cell = d["cell"]
    score, x, _ = best_match(snip.gray(cell), hud.icon_template)
    seen = score >= d["min_similarity"]
    if seen:
        left = cell[0] + x + d["template_box"][2] + 1
        right = cell[0] + cell[2]
        if right - left >= 6:
            found["damage"] = [left, cell[1], right - left, cell[3]]
    row = top["kp_row"]
    icons = hud._kp_icons(snip, row)
    for name in KP_ROW:
        if name not in icons:
            continue
        left = icons[name][1] + 1
        right = left + row["digit_width"]
        later = [icons[k][0] for k in KP_ROW[KP_ROW.index(name) + 1:] if k in icons]
        if later:
            right = min(right, later[0] - row["gap_before_next"])
        if right - left >= 6:
            y, h = row["digits"]
            found[name] = [left, y, right - left, h]
    for name, spec in top["fields"].items():
        found[name] = list(spec["roi"])
    return found, bool(seen or icons)


def native(snip, roi):
    """按基准坐标从 snip 里裁出 roi，原分辨率、彩色（不放大）。"""
    x0, y0, x1, y1 = snip.map.box(roi)
    return snip.img[max(0, y0 - snip.y0):y1 - snip.y0, max(0, x0 - snip.x0):x1 - snip.x0].copy()


def thumb(piece, roi):
    gray = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (max(1, round(roi[2])), max(1, round(roi[3]))), interpolation=cv2.INTER_AREA)


def engine_input(piece, roi):
    """小块 → 引擎读的那张图：放大到"基准尺寸 × 3"、转灰度、四周加边（和 hud.Snip.crop 一样）。"""
    bw, bh = roi[2], roi[3]
    sx = piece.shape[1] / bw
    big = cv2.resize(piece, (round(bw * UPSCALE), round(bh * UPSCALE)),
                     interpolation=cv2.INTER_CUBIC if UPSCALE * sx > 1 else cv2.INTER_AREA)
    big = cv2.cvtColor(cv2.cvtColor(big, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    return cv2.copyMakeBorder(big, PAD, PAD, PAD, PAD, cv2.BORDER_REPLICATE)


def field_rule(field, top):
    """这一项读数的格式和范围（和 hud.py 里一样）。"""
    if field in top["fields"]:
        spec = top["fields"][field]
        return spec["pattern"], spec["range"]
    if field == "damage":
        return r"^(\d{1,5})$", top["damage"]["range"]
    return r"^(\d{1,3})$", top["kp_row"]["range"]


def engine_read(engine, piece, field, roi, top):
    """用现在的引擎读一块，判断和 hud.Hud._number 一样：格式、范围都对、把握到 min_score 才算读出来。"""
    from hud import clean_number

    started = time.perf_counter()
    result = engine(engine_input(piece, roi))
    ms = (time.perf_counter() - started) * 1000
    texts = list(zip(result.txts or (), result.scores or ()))
    pattern, bounds = field_rule(field, top)
    best_text, best_score = None, None
    for text, score in texts:
        m = re.search(pattern, clean_number(text, top["replace"]))
        if not m or score < top["min_score"] or not bounds[0] <= int(m[1]) <= bounds[1]:
            continue
        if best_score is None or score > best_score:
            best_text, best_score = m[1], float(score)
    if best_score is None and texts:
        best_score = float(max(score for _, score in texts))
    return {"engine_text": " | ".join(text for text, _ in texts), "engine_score": best_score,
            "engine_value": best_text, "engine_ms": ms}


class Slicer:
    """一个来源（一个截图文件夹或一个视频）裁小块、马上用引擎读：每一项只存和上一次存的不一样的。"""

    def __init__(self, kind, source_id):
        self.kind, self.source_id = kind, source_id
        self.folder = CROPS / kind / str(source_id)
        self.last = {}
        self.rows = []

    def frame(self, img, t, frame_name=None):
        """一张画面：不是自己的界面返回 False。"""
        w = _tools()
        hud = w["hud"]
        if hud.own_legend(img) is None:
            return False
        snip = hud.top_snip(img)
        top = hud.cfg["top_right"]
        found, seen = cells(hud, snip)
        for field, roi in found.items():
            piece = native(snip, roi)
            if piece.shape[0] < 4 or piece.shape[1] < 4:
                continue
            small = thumb(piece, roi)
            last = self.last.get(field)
            if last is not None and last.shape == small.shape and float(np.mean(cv2.absdiff(last, small))) < CHANGE:
                continue
            read = engine_read(_engine(), piece, field, roi, top)
            if not seen and read["engine_value"] is None:
                continue  # 图标一个都没有、引擎也读不出：可能是靶场、摄像头，不留
            self.last[field] = small
            rel = f"{self.kind}/{self.source_id}/{field}/{len(self.rows):05d}.png"
            path = CROPS / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imencode(".png", piece)[1].tofile(str(path))
            row = {"field": field, "t": round(float(t), 2), "frame": frame_name, "bx": roi[0], "by": roi[1],
                   "bw": roi[2], "bh": roi[3], "img": rel, "w": piece.shape[1], "h": piece.shape[0]}
            row.update(read)
            self.rows.append(row)
        return True


def vote_aspect(frames, need):
    """几帧画面投票认比例：票数最多、至少 need 票的那种比例（宽 / 高），认不出返回 None。"""
    guess = _tools()["guess"]
    votes = Counter()
    for frame in frames:
        aspect, _ = guess.vote(frame)
        if aspect is not None:
            votes[round(aspect, 4)] += 1
    if not votes:
        return None, votes
    aspect, count = votes.most_common(1)[0]
    return (aspect if count >= need else None), votes


def aspect_name(aspect):
    from game_aspect import name
    return name(aspect) if aspect else "-"


# ---------- 考卷：一个截图文件夹 ----------

def frame_time(name, index):
    """截图文件名里的时刻（如 0001_165229_2560x1440.jpg → 一天里第几秒）；没有就用第几张。"""
    m = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", name)
    if m and int(m[1]) < 24 and int(m[2]) < 60 and int(m[3]) < 60:
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
    return index


def run_exam_folder(source_id, folder, files):
    """工作进程：裁一个截图文件夹。返回 {aspect, rows, note, frames}。"""
    import layout
    from ocr import load_image

    files = [Path(f) for f in files]

    def load(path):
        try:
            return load_image(path)
        except (ValueError, OSError, cv2.error):
            return None

    step = max(1, len(files) // VOTES)
    sample = (img for img in (load(f) for f in files[::step][:VOTES]) if img is not None)
    aspect, votes = vote_aspect(sample, need=1 if len(files) < 20 else 2)
    if aspect is None:
        return {"source_id": source_id, "aspect": None, "rows": [], "frames": len(files),
                "note": "认不出画面比例：抽查的截图里没有认得出自己传奇的对局画面" if not votes else f"比例投票分不出来：{dict(votes)}"}
    layout.set_game_aspect(aspect)
    slicer = Slicer("exam", source_id)
    own = 0
    for index, path in enumerate(files):
        img = load(path)
        if img is not None and slicer.frame(img, frame_time(path.name, index), path.name):
            own += 1
    layout.set_game_aspect(None)
    size = None
    first = load(files[0])
    if first is not None:
        size = first.shape[1], first.shape[0]
    return {"source_id": source_id, "aspect": aspect, "rows": slicer.rows, "frames": len(files), "size": size,
            "note": f"{len(files)} 张截图，{own} 张是自己的界面"}


def folder_day(folder):
    """截图文件夹是哪天截的（路径里第一个像 20261110_212454 的文件夹名）："YYYYMMDD"，看不出返回 None。"""
    for part in Path(folder).parts:
        m = re.match(r"(20\d{6})(?:_|$)", part)
        if m:
            return m[1]
    return None


def exam_folders(data_dir=None, skip_days=()):
    """考卷用的截图文件夹：{文件夹: [图片, ...]}（按文件名排好）。数据文件夹里只取 live/*/hud（对局中的截图）；
    skip_days：这几天（YYYYMMDD）截的不进考卷（那几天有训练用的视频，可能是同一局，同一局不能又训练又考）。"""
    found = defaultdict(list)
    for name in EXAM_DIRS:
        base = ROOT / "captures" / name
        if base.is_dir():
            for path in base.rglob("*"):
                if path.suffix.lower() in IMAGE_EXT and path.is_file():
                    found[path.parent].append(path)
    if data_dir is not None:
        for path in Path(data_dir, "captures", "live").glob("*/hud/*"):
            if path.suffix.lower() in IMAGE_EXT:
                found[path.parent].append(path)
    return {folder: sorted(files) for folder, files in sorted(found.items()) if folder_day(folder) not in skip_days}


# ---------- 训练素材：一个视频 ----------

def ascii_path(path, source_id):
    """OpenCV 打不开路径里有中文的视频：给它在 captures/train/links/ 建一个英文名的硬链接（同一个盘，不占空间），
    建不了就复制一份。返回 (能打开的路径, 用完要删的临时文件或 None)。"""
    try:
        str(path).encode("ascii")
        return path, None
    except UnicodeEncodeError:
        pass
    link = TRAIN / "links" / f"video{source_id}{path.suffix.lower()}"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists():
        link.unlink()
    try:
        os.link(path, link)
    except OSError:
        shutil.copyfile(path, link)
    return link, link


def run_video(source_id, path):
    """工作进程：切一个视频。返回 {aspect, rows, note, ...}。"""
    import layout

    path = Path(path)
    opened, temp = ascii_path(path, source_id)
    cap = cv2.VideoCapture(str(opened))
    try:
        if not cap.isOpened():
            return {"source_id": source_id, "error": True, "rows": [], "note": "打不开这个视频"}
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        info = {"source_id": source_id, "fps": fps, "frames": total, "seconds": total / fps if fps else None,
                "size": (width, height)}

        def sample():
            for k in range(VOTES):
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / VOTES))
                ok, frame = cap.read()
                if ok:
                    yield frame

        aspect, votes = vote_aspect(sample(), need=3)
        if aspect is None:
            note = ("认不出画面比例：抽查的 30 帧里没有认得出自己传奇的对局画面（不是第一视角的整局画面？"
                    "画面被裁过、缩放过？传奇太新还没有参照图？）") if not votes else f"比例投票分不出来：{dict(votes)}"
            return {**info, "aspect": None, "rows": [], "note": note}
        layout.set_game_aspect(aspect)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        step = max(1, round(fps * EVERY))
        slicer = Slicer("video", source_id)
        index = looked = own = 0
        while cap.grab():
            if index % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    looked += 1
                    own += slicer.frame(frame, index / fps)
            index += 1
        layout.set_game_aspect(None)
        return {**info, "aspect": aspect, "rows": slicer.rows,
                "note": f"看了 {looked} 帧，{own} 帧是自己的界面（{own / looked:.0%}）" if looked else "一帧都没读出来"}
    finally:
        cap.release()
        if temp is not None:
            try:
                temp.unlink()
            except OSError:
                pass


# ---------- 存进数据库、分堆 ----------

def save_result(conn, kind, result):
    sid = result["source_id"]
    cols = ("field", "t", "frame", "bx", "by", "bw", "bh", "img", "w", "h",
            "engine_text", "engine_score", "engine_value", "engine_ms")
    conn.executemany(
        f"insert into crops (source, kind, {', '.join(cols)}) values (?, ?, {', '.join('?' * len(cols))})",
        [(sid, kind, *(row.get(c) for c in cols)) for row in result["rows"]])
    size = result.get("size") or (None, None)
    status = "error" if result.get("error") else ("done" if result.get("aspect") else "skipped")
    conn.execute("update sources set status=?, note=?, aspect=?, width=?, height=?, fps=?, seconds=?, frames=?, made_at=? "
                 "where id=?", (status, result.get("note"), aspect_name(result.get("aspect")) if result.get("aspect") else None,
                                size[0], size[1], result.get("fps"), result.get("seconds"), result.get("frames"),
                                datetime.now().isoformat(timespec="seconds"), sid))
    assign_piles(conn, sid)
    conn.commit()


def spread(items, limit):
    """从按时间排好的 items 里挑 limit 个，隔得尽量开。"""
    if len(items) <= limit:
        return items
    return [items[round(i * (len(items) - 1) / (limit - 1))] for i in range(limit)] if limit > 1 else items[:1]


def consistent(values, falling):
    """values：一局里按时间排好的 [(编号, 读数或 None)]。返回 {编号: 和前后对不对得上}（只算有读数的）：
    只增不减的项要 前一个 ≤ 自己 ≤ 后一个（剩余小队反过来），前后指最近的有读数的那块。"""
    known = [(cid, number(v)) for cid, v in values if number(v) is not None]
    out = {}
    for i, (cid, v) in enumerate(known):
        prev = known[i - 1][1] if i > 0 else None
        nxt = known[i + 1][1] if i + 1 < len(known) else None
        if falling:
            ok = (prev is None or prev >= v) and (nxt is None or v >= nxt)
        else:
            ok = (prev is None or prev <= v) and (nxt is None or v <= nxt)
        out[cid] = ok
    return out


def assign_piles(conn, source_id):
    """给一个来源的小块分堆（见文件开头）。已经核对过的不动答案，只更新 pile。"""
    rows = conn.execute("select id, field, t, engine_value, engine_score from crops where source=? order by field, t, id",
                        (source_id,)).fetchall()
    by_field = defaultdict(list)
    for r in rows:
        by_field[r["field"]].append(r)
    updates = []
    for field, items in by_field.items():
        groups = defaultdict(list)
        for r in items:
            groups[r["engine_value"] if number(r["engine_value"]) is None else number(r["engine_value"])].append(r)
        keep = set()
        for value, group in groups.items():
            limit = KEEP_NONE if value is None else KEEP_SAME
            keep.update(r["id"] for r in spread(group, limit))
        ok = {}
        segment = []
        last_t = None
        for r in items + [None]:
            if r is None or (last_t is not None and r["t"] - last_t > GAP):
                ok.update(consistent([(x["id"], x["engine_value"]) for x in segment], field in FALLING))
                segment = []
            if r is not None:
                segment.append(r)
                last_t = r["t"]
        for r in items:
            if r["id"] not in keep:
                pile = "extra"
            elif r["engine_value"] is not None and (r["engine_score"] or 0) >= AUTO_SCORE and ok.get(r["id"], False):
                pile = "auto"
            else:
                pile = "check"
            updates.append((pile, r["id"]))
    conn.executemany("update crops set pile=? where id=?", updates)


def new_source(conn, kind, path, name):
    row = conn.execute("select id, status from sources where path=?", (str(path),)).fetchone()
    if row is not None:
        if row["status"] in ("done", "skipped"):
            return None
        conn.execute("delete from crops where source=?", (row["id"],))  # 上次没切完（中断了）：重新切
        conn.execute("update sources set status='running' where id=?", (row["id"],))
        shutil.rmtree(CROPS / kind / str(row["id"]), ignore_errors=True)
        return row["id"]
    cur = conn.execute("insert into sources (kind, path, name, status) values (?, ?, ?, 'running')",
                       (kind, str(path), name))
    return cur.lastrowid


def run_jobs(conn, kind, jobs, workers):
    """jobs：[(来源编号, 函数, 参数...)]；几个进程同时跑，跑完一个存一个。"""
    if not jobs:
        print("没有新的要切。")
        return
    print(f"要切 {len(jobs)} 个，{workers} 个同时跑……", flush=True)
    started = time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, sid, *args): (sid, name) for sid, name, fn, *args in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            sid, name = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # 一个来源出错不影响别的
                result = {"source_id": sid, "error": True, "rows": [], "note": f"出错：{exc!r}"}
            save_result(conn, kind, result)
            piles = Counter(r["pile"] for r in conn.execute("select pile from crops where source=?", (sid,)))
            print(f"[{done}/{len(jobs)}] {name}：{result.get('note')}；比例 {aspect_name(result.get('aspect'))}；"
                  f"{len(result['rows'])} 块（规则核对过 {piles['auto']}，要人看 {piles['check']}，"
                  f"多出来的 {piles['extra']}）  {time.time() - started:.0f} 秒", flush=True)


# ---------- 引擎多快、占多少内存 ----------

def measure_engine(conn, count=200):
    """单独一个进程、1 个线程，从考卷里抽 count 块量引擎：平均每块几毫秒、加载以后多占多少内存。存进 meta。"""
    from ocr import SMALL_CROP_PARAMS, create_engine
    from ocr import load_config
    from process_info import memory_mb

    rows = conn.execute("select img, field, bx, by, bw, bh from crops where kind='exam' and pile!='extra' "
                        "order by random() limit ?", (count,)).fetchall()
    if not rows:
        return
    top = load_config("hud.json")["top_right"]
    pieces = [(cv2.imdecode(np.fromfile(CROPS / r["img"], dtype=np.uint8), cv2.IMREAD_COLOR), r) for r in rows]
    before, _ = memory_mb()
    engine = create_engine(SMALL_CROP_PARAMS)
    loaded, _ = memory_mb()
    engine_read(engine, pieces[0][0], pieces[0][1]["field"], [pieces[0][1][k] for k in ("bx", "by", "bw", "bh")], top)
    times = []
    for piece, r in pieces:
        times.append(engine_read(engine, piece, r["field"], [r[k] for k in ("bx", "by", "bw", "bh")], top)["engine_ms"])
    after, peak = memory_mb()
    meta = {"engine_ms": f"{np.mean(times):.1f}", "engine_ms_median": f"{np.median(times):.1f}",
            "engine_mem_mb": f"{after - before:.0f}", "engine_load_mb": f"{loaded - before:.0f}",
            "engine_measured": f"{datetime.now():%Y-%m-%d %H:%M}，{len(times)} 块，1 个线程"}
    conn.executemany("insert or replace into meta (key, value) values (?, ?)", meta.items())
    conn.commit()
    print(f"引擎：平均每块 {meta['engine_ms']} 毫秒（中位数 {meta['engine_ms_median']}），"
          f"加载以后多占约 {meta['engine_mem_mb']} MB 内存")


# ---------- 命令 ----------

def name_day(name):
    """文件名里的日期 → "YYYYMMDD"：2026-11-20_…（Evo 的录像）、…_11-10-2026_…（月-日-年）、20261110…；没有返回 None。"""
    m = re.search(r"(20\d\d)[-_.]?(\d\d)[-_.]?(\d\d)", name)
    if m and 1 <= int(m[2]) <= 12 and 1 <= int(m[3]) <= 31:
        return f"{m[1]}{m[2]}{m[3]}"
    m = re.search(r"(?<!\d)(\d\d)-(\d\d)-(20\d\d)(?!\d)", name)
    if m and 1 <= int(m[1]) <= 12 and 1 <= int(m[2]) <= 31:
        return f"{m[3]}{m[1]}{m[2]}"
    return None


def video_days(data_dir):
    """训练素材里的视频是哪几天录的（看文件名）：这几天数据文件夹里的截图不进考卷（可能是同一局）。"""
    paths = list(VIDEOS.rglob("*")) if VIDEOS.is_dir() else []
    if data_dir is not None:
        paths += [p for p in Path(data_dir, "recordings").rglob("*.mp4") if "_recording" not in p.parts]
    return {day for p in paths if p.suffix.lower() in VIDEO_EXT and (day := name_day(p.name))}


def drop_exam_days(conn, days):
    """考卷里已经有的、这几天的数据文件夹截图（还没核对过的）删掉。返回删了几个文件夹。"""
    dropped = 0
    for s in conn.execute("select id, path from sources where kind='exam'").fetchall():
        path = Path(s["path"])
        if folder_day(path) not in days:
            continue
        if conn.execute("select count(*) from crops where source=? and status!='todo'", (s["id"],)).fetchone()[0]:
            print(f"注意：{path} 和训练素材是同一天，但已经核对过一些，没有删")
            continue
        conn.execute("delete from crops where source=?", (s["id"],))
        conn.execute("delete from sources where id=?", (s["id"],))
        shutil.rmtree(CROPS / "exam" / str(s["id"]), ignore_errors=True)
        dropped += 1
    conn.commit()
    return dropped


def default_data_dir():
    from source_run import installed_data_dir
    path = installed_data_dir()
    return path if path and Path(path).is_dir() else None


def cmd_exam(args):
    conn = connect()
    data_dir = None if args.no_data else default_data_dir()
    days = video_days(data_dir)
    if drop_exam_days(conn, days):
        print(f"考卷里删掉了和训练视频同一天（{'、'.join(sorted(days))}）的截图文件夹")
    jobs = []
    for folder, files in exam_folders(data_dir, days).items():
        name = str(folder.relative_to(ROOT)) if folder.is_relative_to(ROOT) else str(folder)
        sid = new_source(conn, "exam", folder, name)
        if sid is not None:
            jobs.append((sid, name, run_exam_folder, str(folder), [str(f) for f in files]))
    conn.commit()
    run_jobs(conn, "exam", jobs, args.jobs)
    if jobs or args.measure:
        measure_engine(conn)
    cmd_status(args, conn)


def cmd_videos(args):
    conn = connect()
    days = video_days(None if args.no_data else default_data_dir())
    if drop_exam_days(conn, days):
        print(f"考卷里删掉了和训练视频同一天（{'、'.join(sorted(days))}）的截图文件夹")
    paths = sorted(p for p in VIDEOS.rglob("*") if p.suffix.lower() in VIDEO_EXT and p.is_file()) if VIDEOS.is_dir() else []
    data_dir = None if args.no_data else default_data_dir()
    if data_dir is not None:
        paths += sorted(p for p in Path(data_dir, "recordings").rglob("*.mp4") if "_recording" not in p.parts)  # Evo 自己的录像（按月分文件夹；_recording 是正在录的）：和 Evo 看到的最像
    jobs = []
    for path in paths:
        sid = new_source(conn, "video", path, path.name)
        if sid is not None:
            jobs.append((sid, path.name, run_video, str(path)))
    conn.commit()
    run_jobs(conn, "video", jobs, args.jobs)
    cmd_status(args, conn)


def cmd_status(args, conn=None):
    conn = conn or connect()
    for kind, title in (("exam", "考卷"), ("video", "训练素材")):
        sources = conn.execute("select status, count(*) n from sources where kind=? group by status", (kind,)).fetchall()
        if not sources:
            continue
        print(f"\n{title}：来源 " + "，".join(f"{r['status']} {r['n']}" for r in sources))
        print(f"  {'项':<6}{'规则核对过':>8}{'要人看':>8}{'多出来的':>8}{'核对过':>8}{'丢掉':>6}")
        for field in FIELDS:
            c = Counter()
            for r in conn.execute("select pile, status from crops where kind=? and field=?", (kind, field)):
                c[r["pile"]] += 1
                c[r["status"]] += 1
            print(f"  {LABELS[field]:<6}{c['auto']:>10}{c['check']:>10}{c['extra']:>10}{c['done']:>10}{c['skip']:>8}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="训练小模型的素材：裁小块、引擎先读、分堆")
    parser.add_argument("command", choices=("exam", "videos", "status"))
    parser.add_argument("-j", "--jobs", type=int, default=4, help="几个同时切（默认 4）")
    parser.add_argument("--no-data", action="store_true", help="不用数据文件夹里的截图、录像")
    parser.add_argument("--measure", action="store_true", help="exam：没有新的也重新量一次引擎")
    args = parser.parse_args()
    {"exam": cmd_exam, "videos": cmd_videos, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    main()
