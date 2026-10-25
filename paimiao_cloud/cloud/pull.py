"""Evo 派喵云同步 · 取回（云端 → 本机）。"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime

from cloud import pairing

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
    "rp_source": None,
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

PULL_SOURCE = "云同步"


def to_text(ms):
    try:
        value = int(ms or 0) / 1000
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _have_columns(conn, table, wanted):
    try:
        got = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return []
    return [name for name in wanted if name in got]


def table_columns(conn, table):
    return set(_have_columns(conn, table, _all_columns(conn, table)))


def _all_columns(conn, table):
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def local_index(conn):
    from cloud.sync import client_id_of       # noqa: PLC0415

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
    out = {}
    try:
        rows = conn.execute("SELECT reading_id, read_at FROM lobby_readings").fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        if row[1]:
            out.setdefault(str(row[1]), row[0])
    return out


def _scalar(value, default=0):
    if value is None:
        return default
    return value


def match_row(item):
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


def backup(db_path, keep=5):
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


def apply_matches(conn, items, dry_run=False):
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
    matches = pairing.download(snapshot, limit)
    try:
        lobby = pairing.download_rank(snapshot, limit)
    except pairing.CloudError as exc:
        lobby = {"items": [], "error": str(exc)[:200]}
    return matches.get("items") or [], lobby.get("items") or [], lobby.get("error", "")


def pull_into(db_path, snapshot, limit=200, dry_run=False):
    items, lobby_items, lobby_error = fetch(snapshot, limit)
    if dry_run:
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
