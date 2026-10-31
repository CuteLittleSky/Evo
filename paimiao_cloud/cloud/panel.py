from __future__ import annotations

import time

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from cloud import notice, pairing, pull, qr, sync
from cloud import state as cloud_state

bp = Blueprint("cloud", __name__, url_prefix="/cloud")

MODULE = None
NAV_LABEL = "派喵云同步"

def build():
    return bp

@bp.app_context_processor
def inject():
    snapshot = cloud_state.load()
    data = {"cloudcard": card_view(snapshot)}
    nav = nav_items()
    if nav:
        data["nav"] = nav
    return data

def nav_items():

    base = getattr(MODULE, "NAV", None)
    if not base:
        return None
    base = tuple(base)
    if any(endpoint == "cloud.page" for endpoint, _ in base):
        return base
    return base + (("cloud.page", NAV_LABEL),)

def card_view(snapshot):

    return {
        "agreed": cloud_state.agreed(snapshot),
        "paired": cloud_state.paired(snapshot),
        "device_name": (snapshot.get("device") or {}).get("name", ""),
    }

@bp.get("/")
def page():
    return render_template("cloud_page.html", view=page_view(cloud_state.load()))

def page_view(snapshot):
    agreed = cloud_state.agreed(snapshot)
    paired = cloud_state.paired(snapshot)
    left = seconds_left(snapshot)

    if snapshot.get("pair_code") and left <= 0:
        clear_pair_fields(snapshot)
        cloud_state.save(snapshot)
    stats = snapshot.get("stats") or {}
    device = snapshot.get("device") or {}
    return {
        "agreed": agreed,
        "paired": paired,
        "server": cloud_state.default_server(),
        "server_host": cloud_state.default_server().split("//")[-1].split("/")[0],
        "pair_code": snapshot.get("pair_code") if left > 0 else "",
        "pair_left": left,

        "pair_qr_url": pair_qr_url(snapshot) if left > 0 else "",

        "pair_mini_code": pair_mini_code(snapshot) if left > 0 else "",
        "device": {"name": device.get("name", ""), "expires_text": device.get("expires_text", "")},
        "enabled": bool(snapshot.get("enabled")),

        **_upload_view(),
        "consent_version": cloud_state.CONSENT_VERSION,
        "consent_at": (snapshot.get("consent") or {}).get("at", ""),
    }

def pair_qr_url(snapshot):

    if snapshot.get("pair_qr_revoked"):
        return ""
    return str(snapshot.get("pair_qr_url") or "")

def pair_mini_code(snapshot):

    if snapshot.get("pair_qr_revoked"):
        return ""
    data = str(snapshot.get("pair_mini_code") or "").strip()
    if not data:
        return ""
    if data.startswith("data:"):
        return data
    mime = str(snapshot.get("pair_mini_code_mime") or "").strip()

    if mime not in ("image/png", "image/jpeg", "image/gif"):
        mime = "image/png"
    return "data:" + mime + ";base64," + data

def clear_pair_fields(snapshot):

    snapshot["pair_code"] = ""
    snapshot["pair_hint"] = ""
    snapshot["pair_ticket"] = ""
    snapshot["pair_qr_ticket"] = ""
    snapshot["pair_qr_url"] = ""
    snapshot["pair_mini_code"] = ""
    snapshot["pair_qr_revoked"] = False
    snapshot["pair_code_at"] = 0.0
    snapshot["pair_expires_ms"] = 0

def seconds_left(snapshot):

    expires_ms = int(snapshot.get("pair_expires_ms") or 0)
    if expires_ms:
        return max(0, int((expires_ms / 1000) - time.time()))
    at = float(snapshot.get("pair_code_at") or 0)
    if not at:
        return 0
    return max(0, int(pairing.PAIR_TTL - (time.time() - at)))

def back():
    return redirect(url_for("cloud.page"))

def _after_notice(default: str) -> str:

    target = (request.form.get("next") or "").strip()
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default

@bp.post("/notice/ack")
def notice_ack():

    version = (request.form.get("version") or "").strip() or notice.NOTICE_VERSION
    action = request.form.get("action") or "ok"
    notice.mark_seen(version)
    cloud_state.log(f"分支说明已读（{version}，{action}）")
    return redirect(_after_notice(url_for("settings")))

@bp.post("/recommend/dismiss")
def recommend_dismiss():

    notice.mark_recommend_seen()
    back_to = request.form.get("next") or request.referrer or url_for("settings")
    return redirect(_safe_next(back_to, url_for("settings")))

def _safe_next(target: str, default: str) -> str:

    # 只允许站内相对路径，免得变成一个开放跳转。
    target = (target or "").strip()
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default

def wants_json():
    return request.form.get("ajax") == "1" or "application/json" in request.headers.get("Accept", "")

@bp.post("/consent")
def consent():
    if request.form.get("agree") != "1":
        flash("需要勾选同意之后才能继续。", "error")
        return back()
    snapshot = cloud_state.load()
    snapshot["consent"] = {
        "agreed": True,
        "version": cloud_state.CONSENT_VERSION,
        "at": time.strftime("%Y-%m-%d %H:%M"),
    }
    cloud_state.save(snapshot)
    cloud_state.log("用户同意了数据传输指引")
    return back()

@bp.post("/revoke")
def revoke():

    snapshot = cloud_state.load()
    pairing.revoke_qr(snapshot)
    snapshot["consent"] = {}
    snapshot["device"] = {}
    snapshot["enabled"] = False
    snapshot["hashes"] = {}
    clear_pair_fields(snapshot)
    cloud_state.save(snapshot)
    cloud_state.log("用户撤回同意")
    flash("已撤回同意，并清除配对和设备令牌。", "error")
    return back()

@bp.post("/pair")
def pair():
    snapshot = cloud_state.load()
    if not cloud_state.agreed(snapshot):
        flash("您还没有同意数据传输指引。", "error")
        return back()
    pairing.revoke_qr(snapshot)
    try:
        pairing.start(snapshot)
    except pairing.CloudError as exc:
        clear_pair_fields(snapshot)
        cloud_state.save(snapshot)
        flash(f"配对码生成失败：{exc}", "error")
        return back()
    snapshot["pair_code_at"] = time.time()
    cloud_state.save(snapshot)
    return back()

@bp.post("/revoke-qr")
def revoke_qr():

    snapshot = cloud_state.load()
    done = pairing.revoke_qr(snapshot)
    if done:
        cloud_state.save(snapshot)
    return jsonify({"status": "ok", "revoked": bool(done)})

@bp.post("/qrcode")
def qrcode():

    snapshot = cloud_state.load()
    if not _same_token(_body_field("token")):
        return jsonify({"status": "error", "message": "口令不对"}), 403
    url = _body_field("url").strip()
    current = pair_qr_url(snapshot)
    if not url or not current or url != current:
        return jsonify({"status": "error", "message": "二维码已失效"}), 200
    try:
        rows = qr.encode(url)
    except qr.QrError as exc:

        return jsonify({"status": "error", "message": str(exc)}), 200
    return jsonify({"status": "ok", "rows": [[1 if v else 0 for v in row] for row in rows]})

def _body_field(name):

    value = request.form.get(name)
    if value is None:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            value = body.get(name)
    return "" if value is None else str(value)

def _same_token(value):

    try:
        from flask import current_app
        expected = str(current_app.config.get("TOKEN") or "")
    except Exception:
        expected = ""
    if not expected:
        return True
    return str(value or "") == expected

@bp.post("/poll")
def poll():
    snapshot = cloud_state.load()
    try:
        done = pairing.poll(snapshot)
    except pairing.CloudError as exc:
        if wants_json():
            return jsonify({"status": "error", "message": str(exc)}), 200
        flash(str(exc), "error")
        return back()
    cloud_state.save(snapshot)
    if done:
        cloud_state.log("配对成功")
        if wants_json():
            return jsonify({"status": "confirmed"})
        flash("配对成功，这台电脑已经连上派喵云同步了。", "ok")
        return back()
    if wants_json():
        return jsonify({"status": "pending", "left": seconds_left(snapshot)})
    return back()

@bp.post("/toggle")
def toggle():
    snapshot = cloud_state.load()
    if not cloud_state.paired(snapshot):
        flash("您没有进行配对。", "error")
        return back()
    snapshot["enabled"] = request.form.get("enabled") == "1"
    cloud_state.save(snapshot)
    if snapshot["enabled"]:
        sync.arm_once()
    return back()

@bp.post("/unpair")
def unpair():
    snapshot = cloud_state.load()
    pairing.revoke_qr(snapshot)
    snapshot["device"] = {}
    snapshot["enabled"] = False
    snapshot["hashes"] = {}
    clear_pair_fields(snapshot)
    cloud_state.save(snapshot)
    cloud_state.log("用户解除了配对")
    flash("已解除配对，这台电脑不会再上传任何数据。", "error")
    return back()

@bp.post("/sync-now")
def sync_now():

    snapshot = cloud_state.load()
    if not cloud_state.paired(snapshot):
        if wants_json():
            return jsonify({"status": "error", "message": "您没有进行配对。"}), 200
        flash("您没有进行配对。", "error")
        return back()
    if not snapshot.get("enabled"):
        if wants_json():
            return jsonify({"status": "error", "message": "同步开关已被关闭。"}), 200
        flash("同步开关已被关闭。", "error")
        return back()
    sent = sync.push_quiet(snapshot)
    if wants_json():
        return jsonify({"status": "ok", "sent": sent, "view": _upload_view()})
    after = cloud_state.load().get("stats") or {}
    if after.get("rank_error"):
        flash(f"同步完成，但段位没传上去：{after['rank_error']}", "error")
    elif after.get("last_error"):
        flash(f"同步没成功：{after['last_error']}", "error")
    elif sent:
        flash(f"同步完成，这次上传了 {sent} 局。", "ok")
    else:
        flash("同步完成，没有新数据需要上传。", "ok")
    return back()

def _upload_view():

    stats = cloud_state.load().get("stats") or {}
    return {
        "uploaded": int(stats.get("uploaded") or 0),
        "failed": int(stats.get("failed") or 0),
        "last_ok_at": stats.get("last_ok_at") or "",
        "last_error": stats.get("last_error") or "",
    }

@bp.get("/pull/preview")
def pull_preview():

    snapshot = cloud_state.load()
    if not cloud_state.paired(snapshot):
        return jsonify({"status": "error", "message": "您没有进行配对。"}), 200
    try:
        stats = pull.pull_into(sync._db_path(), snapshot, dry_run=True)
    except pairing.CloudError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 200
    except Exception as exc:
        return jsonify({"status": "error",
                        "message": f"{type(exc).__name__}: {exc}"[:200]}), 200
    return jsonify({"status": "ok", "view": _pull_view(stats)})

@bp.post("/pull")
def pull_now():
    snapshot = cloud_state.load()
    if not cloud_state.paired(snapshot):
        flash("您没有进行配对。", "error")
        return back()
    try:
        stats = pull.pull_into(sync._db_path(), snapshot)
    except pairing.CloudError as exc:
        flash(f"取回失败：{exc}", "error")
        return back()
    except Exception as exc:
        flash(f"取回失败：{type(exc).__name__}: {exc}"[:200], "error")
        return back()

    cloud_state.log(f"取回 {stats['added']} 局、{stats['lobby_added']} 条段位读数")
    if stats["added"] or stats["lobby_added"]:
        flash(f"已取回 {stats['added']} 局战绩、{stats['lobby_added']} 条段位读数"
              f"（本地已有 {stats['skipped']} 局，跳过）。", "ok")
    else:
        flash(f"云端没有新的对局（{stats['skipped']} 局本地都已经有了）。", "ok")
    return back()

def _pull_view(stats):

    return {
        "fetched": int(stats.get("fetched") or 0),
        "added": int(stats.get("added") or 0),
        "skipped": int(stats.get("skipped") or 0),
        "lobby_added": int(stats.get("lobby_added") or 0),
        "lobby_error": stats.get("lobby_error") or "",
        "backup": stats.get("backup") or "",
    }
