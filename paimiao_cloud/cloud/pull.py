"""Evo 派喵云同步 · 取回（云端 → 本机）。

**这是整个派喵云里唯一会写 Evo 数据库的地方。**

和 push() 的三条约束正好相反，所以要单独一个文件、单独一套规矩：

|           | push（上传）           | pull（取回）                    |
|-----------|----------------------|-------------------------------|
| 打开方式   | `mode=ro`，物理写不坏   | 读写，**必须**（要插数据）        |
| 写入内容   | 无                     | 只插新行，**从不改、从不删**       |
| 触发       | 定时线程自动             | 只有用户点按钮                   |

为什么必须有写权限：唯一的用途是"换了电脑 / 重装以后，把云端的历史拉回本地库"。
不做这个就得让用户对着空白面板干瞪眼。

四条安全规矩（缺一条我都不会写这个文件）：

1. **只插不改**：全程只有 `INSERT` 和 `UPDATE match_id`（后者是插入后回填 `teammates` 的外键，
   因为 SQLite 的 `lastrowid` 才是真实主键，云端没有 match_id）。
2. **本地已有的一律跳过**：判重键和服务器用的是同一个 `client_id_of()`（`sha256(ended_at)`），
   也就是"服务器上认成同一局，本地也认成同一局"，两边口径不可能分叉。
3. **识别不准的数据也照样拉**（`needs_review` 保留），但**截图路径留空**——
   那是另一台电脑上的文件，本机没有。面板上点开这局右侧会是空的，这是诚实的降级。
4. **写库前先备份**：一次取回会改几百行，出错要能退回去。备份放 `backups/`，
   和现有 `tools/` 里手工备份的习惯一致。
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime

from cloud import pairing

# 一局要写回 matches 的列。键是本地列名，值是云端字段名。
# **故意不含** evidence_path（本机没有那张截图）和 app_version（云端不记）。
MATCH_COLUMNS = {
    "ended_at": "endedAt",
    "player_name": "playerName",
    "mode": "mode",
    "map": "map",
    "legend": "legend",
    "placement": "placement",
    "kills": "kills",
    "assists": "assists",
    "knockdowns": "knockdowns",
    "damage": "damage",
    "participation": "participation",
    "rp_delta": "rpDelta",
    "survival_time": "survivalSeconds",
    "needs_review": "needsReview",
    "squad_kills": "squadKills",
    "revives": "revives",
    "respawns": "respawns",
    "rank_tier": "rankTier",
    "rank_division": "rankDivision",
    "rank_progress": "rankProgress",
    "started_at": "startedAt",
    "rp_source": None,      # 云端没有：这条不是本机识别的，来源标成"云同步"
}
MATE_COLUMNS = {
    "name": "name",
    "legend": "legend",
    "kills": "kills",
    "assists": "assists",
    "damage": "damage",
    "knockdowns": "knockdowns",
    "survival_time": "survivalSeconds",
    "revives": "revives",
    "respawns": "respawns",
}
LOBBY_COLUMNS = {
    "read_at": "readAt",
    "player_name": "playerName",
    "rank_tier": "rankTier",
    "rank_division": "rankDivision",
    "rank_progress": "rankProgress",
    "score": "score",
}

PULL_SOURCE = "云同步"      # 写进 rp_source，一眼能看出这条是从云端取回来的


# ---------- 时间 ----------

def to_text(ms):
    """毫秒时间戳 → 库里统一的本地时间文本 `YYYY-MM-DD HH:MM:SS`。

    和 sync.to_ms() 互为逆运算：那边 `strptime(...).astimezone()` 也是本地时区。
    0 / None → ""（没数据就是没数据，不要编一个 1970 出来）。
    """
    try:
        value = int(ms or 0) / 1000
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


# ---------- 判重 ----------

def _have_columns(conn, table, wanted):
    try:
        got = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return []
    return [name for name in wanted if name in got]


def table_columns(conn, table):
    """这张表实际有哪些列。

    取回要往本地表里写，而老库没有后来加的列（比如 rank_progress 是迁移 6 才有的）。
    直接 INSERT 会 "table matches has no column named rank_progress" 让整次取回失败，
    所以只写本地真有的列——少写几列无所谓，一条都写不进去才要命。
    """
    return set(_have_columns(conn, table, _all_columns(conn, table)))


def _all_columns(conn, table):
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def local_index(conn):
    """本地已有的对局：{客户端键: match_id}。

    键用 `client_id_of()` 算，和服务器**完全同一个函数**——
    这样"服务器上是一条"和"本地是一条"永远是同一件事，不可能对不上。
    """
    from cloud.sync import client_id_of       # noqa: PLC0415  避免循环导入

    out = {}
    try:
        rows = conn.execute("SELECT match_id, ended_at FROM matches").fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        key = client_id_of({"ended_at": row[1]})
        out.setdefault(key, row[0])
    return out


def local_lobby_index(conn):
    """本地已有的大厅读数：{read_at 文本: reading_id}。"""
    out = {}
    try:
        rows = conn.execute("SELECT reading_id, read_at FROM lobby_readings").fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        if row[1]:
            out.setdefault(str(row[1]), row[0])
    return out


# ---------- 转换 ----------

def _scalar(value, default=0):
    """云端来的数字可能是 null，写进 NOT NULL 的列会炸，统一兜成 0。"""
    if value is None:
        return default
    return value


def match_row(item):
    """云端一条对局 → 本地 matches 一行。"""
    row = {}
    for local, remote in MATCH_COLUMNS.items():
        if remote is None:
            row[local] = PULL_SOURCE
        elif remote == "endedAt":
            row[local] = to_text(item.get("endedAt"))
        elif remote == "startedAt":
            text = to_text(item.get("startedAt"))
            row[local] = text or None
        elif remote in ("needsReview",):
            row[local] = 1 if item.get(remote) else 0
        elif remote in ("mode", "map", "legend", "player_name", "rank_tier", "rank_division"):
            row[local] = item.get(remote) or None
        else:
            row[local] = _scalar(item.get(remote))
    return row


def mate_rows(item):
    """云端一条对局的队友 → 本地 teammates 的若干行（还没填 match_id）。"""
    out = []
    for mate in (item.get("teammates") or [])[:2]:
        row = {}
        for local, remote in MATE_COLUMNS.items():
            if remote in ("name", "legend"):
                row[local] = mate.get(remote) or None
            else:
                row[local] = _scalar(mate.get(remote))
        if row.get("name"):
            out.append(row)
    return out


def lobby_row(item):
    row = {}
    for local, remote in LOBBY_COLUMNS.items():
        if remote == "readAt":
            row[local] = to_text(item.get("readAt"))
        elif remote in ("player_name", "rank_tier", "rank_division"):
            row[local] = item.get(remote) or None
        else:
            row[local] = _scalar(item.get(remote))
    return row


# ---------- 备份 ----------

def backup(db_path, keep=5):
    """写库前先备份一份。返回备份文件路径（失败返回 ""，不阻止取回）。

    只留最近 keep 份——每次取回都备份会一直长。
    """
    from pathlib import Path      # noqa: PLC0415

    src = Path(db_path)
    if not src.exists():
        return ""
    folder = src.parent / "backups"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = folder / f"{src.stem}-pull-{stamp}{src.suffix}"
        shutil.copy2(src, dest)
        old = sorted(folder.glob(f"{src.stem}-pull-*{src.suffix}"))
        for stale in old[:-keep]:
            stale.unlink(missing_ok=True)
        return str(dest)
    except OSError:
        return ""


# ---------- 主流程 ----------

def apply_matches(conn, items, dry_run=False):
    """把云端对局写进本地库，只插新的。返回统计。

    dry_run=True 只算不写，给"先看看会拉回几局"用。
    """
    from cloud.sync import client_id_of       # noqa: PLC0415

    have = local_index(conn)
    cols = table_columns(conn, "matches")
    mate_cols = table_columns(conn, "teammates")

    stats = {"fetched": len(items), "added": 0, "skipped": 0, "mates": 0}
    for item in items:
        try:
            key = client_id_of({"ended_at": to_text(item.get("endedAt"))})
        except Exception:
            stats["skipped"] += 1
            continue
        if key in have:
            stats["skipped"] += 1
            continue
        row = {k: v for k, v in match_row(item).items() if k in cols}
        if "ended_at" not in row or not row["ended_at"]:
            # 没有结束时间就没法定位这一局，跳过（正常情况不会发生：服务器强制要 endedAt）
            stats["skipped"] += 1
            continue
        stats["added"] += 1
        if dry_run:
            continue

        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        cur = conn.execute(f"INSERT INTO matches ({names}) VALUES ({marks})", list(row.values()))
        match_id = cur.lastrowid
        have[key] = match_id

        for slot, mate in enumerate(mate_rows(item)):
            data = {k: v for k, v in mate.items() if k in mate_cols}
            data["match_id"] = match_id
            data["slot"] = slot
            data_names = ", ".join(data)
            data_marks = ", ".join("?" for _ in data)
            conn.execute(f"INSERT INTO teammates ({data_names}) VALUES ({data_marks})",
                         list(data.values()))
            stats["mates"] += 1

    if not dry_run:
        conn.commit()
    return stats


def apply_lobby(conn, items, dry_run=False):
    """把云端大厅读数写进本地库，只插新的。"""
    have = local_lobby_index(conn)
    cols = table_columns(conn, "lobby_readings")
    stats = {"fetched": len(items), "added": 0, "skipped": 0}
    for item in items:
        row = lobby_row(item)
        stamp = row.get("read_at") or ""
        if not stamp or stamp in have:
            stats["skipped"] += 1
            continue
        data = {k: v for k, v in row.items() if k in cols}
        stats["added"] += 1
        if dry_run:
            continue
        names = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        cur = conn.execute(f"INSERT INTO lobby_readings ({names}) VALUES ({marks})",
                           list(data.values()))
        have[stamp] = cur.lastrowid
    if not dry_run:
        conn.commit()
    return stats


def fetch(snapshot, limit=200):
    """从云端下载对局和大厅读数（只下载，不写库）。"""
    matches = pairing.download(snapshot, limit)
    try:
        lobby = pairing.download_rank(snapshot, limit)
    except pairing.CloudError as exc:
        # 老服务端没有 /rank 时不该挡住取回战绩
        lobby = {"items": [], "error": str(exc)[:200]}
    return matches.get("items") or [], lobby.get("items") or [], lobby.get("error", "")


def pull_into(db_path, snapshot, limit=200, dry_run=False):
    """取回并写库。返回统计字典；失败抛 CloudError / sqlite3.Error。

    db_path 由调用方给（面板有 --db 时跟着它走，见 sync.set_db_path）。
    """
    items, lobby_items, lobby_error = fetch(snapshot, limit)
    if dry_run:
        # 试跑不备份、不写，只算会被跳过多少
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            stats = apply_matches(conn, items, dry_run=True)
            stats.update({"lobby_" + k: v for k, v in apply_lobby(conn, lobby_items, dry_run=True).items()})
            stats["backup"] = ""
            stats["lobby_error"] = lobby_error
            return stats
        finally:
            conn.close()

    saved = backup(db_path)
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        stats = apply_matches(conn, items)
        lobby_stats = apply_lobby(conn, lobby_items)
        stats["lobby_fetched"] = lobby_stats["fetched"]
        stats["lobby_added"] = lobby_stats["added"]
        stats["lobby_skipped"] = lobby_stats["skipped"]
        stats["backup"] = saved
        stats["lobby_error"] = lobby_error
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
