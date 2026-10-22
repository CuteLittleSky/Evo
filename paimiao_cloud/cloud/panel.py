"""Evo 派喵云同步 · 独立页面（/cloud/）与导航注入。

页面状态严格三分，界面上永远只显示当前这一步该出现的东西：
    未同意协议  →  只有协议正文 + 同意按钮
    已同意未配对 →  只有服务器地址 + 配对
    已配对      →  同步开关、上传状态、解除配对
"""
from __future__ import annotations

import threading
import time

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from cloud import notice, pairing, pull, sync
from cloud import state as cloud_state

bp = Blueprint("cloud", __name__, url_prefix="/cloud")

MODULE = None          # bootstrap 把 panel_app 模块塞进来（用来读它的 NAV）
NAV_LABEL = "派喵云同步"


def build():
    return bp


# ---------- 上下文：导航第 4 项 + 卡片数据 ----------

@bp.app_context_processor
def inject():
    snapshot = cloud_state.load()
    data = {"cloudcard": card_view(snapshot)}
    nav = nav_items()
    if nav:
        data["nav"] = nav
    return data


def nav_items():
    """在 Evo 自己的 NAV 后面追加一项。

    Blueprint 的 app_context_processor 覆盖同名键（已实测），所以不用改 panel_app.py。
    Evo 的 NAV 要是哪天变了，这里跟着变；读不到就干脆不覆盖，界面保持原样。
    """
    base = getattr(MODULE, "NAV", None)
    if not base:
        return None
    base = tuple(base)
    if any(endpoint == "cloud.page" for endpoint, _ in base):
        return base
    return base + (("cloud.page", NAV_LABEL),)


def card_view(snapshot):
    """设置页那张小卡片（不配对就不显示任何同步数据）。"""
    return {
        "agreed": cloud_state.agreed(snapshot),
        "paired": cloud_state.paired(snapshot),
        "device_name": (snapshot.get("device") or {}).get("name", ""),
    }


# ---------- 页面 ----------

@bp.get("/")
def page():
    return render_template("cloud_page.html", view=page_view(cloud_state.load()))


def page_view(snapshot):
    agreed = cloud_state.agreed(snapshot)
    paired = cloud_state.paired(snapshot)
    left = seconds_left(snapshot)
    stats = snapshot.get("stats") or {}
    device = snapshot.get("device") or {}
    return {
        "agreed": agreed,
        "paired": paired,
        "server": cloud_state.default_server(),
        "server_host": cloud_state.default_server().split("//")[-1].split("/")[0],
        "pair_code": snapshot.get("pair_code") if left > 0 else "",
        "pair_left": left,
        "device": {"name": device.get("name", ""), "expires_text": device.get("expires_text", "")},
        "enabled": bool(snapshot.get("enabled")),
        "uploaded": int(stats.get("uploaded") or 0),
        "failed": int(stats.get("failed") or 0),
        "last_ok_at": stats.get("last_ok_at") or "",
        "last_error": stats.get("last_error") or "",
        "consent_version": cloud_state.CONSENT_VERSION,
        "consent_at": (snapshot.get("consent") or {}).get("at", ""),
    }


def seconds_left(snapshot):
    """配对码还剩多少秒。用服务器给的 expiresAt（毫秒戳）算，没有才回落到本地时刻。

    以前用 pair_code_at + PAIR_TTL 猜，遇上一秒级误差就会多显示一会儿；
    服务器明明说了 expiresAt，没必要猜。
    """
    expires_ms = int(snapshot.get("pair_expires_ms") or 0)
    if expires_ms:
        return max(0, int((expires_ms / 1000) - time.time()))
    at = float(snapshot.get("pair_code_at") or 0)
    if not at:
        return 0
    return max(0, int(pairing.PAIR_TTL - (time.time() - at)))


def back():
    return redirect(url_for("cloud.page"))


# ---------- 分支说明弹窗（cloud/notice.py） ----------

def _after_notice(default: str) -> str:
    """弹窗提交后回到哪一页。

    只认本站相对路径：`next` 是隐藏域，别人可以改，不能让它跳到外站。
    这里不校验 token —— 它不写业务数据，只记一个"看过了"的版本号；
    而且 setting.html 里的按钮本身没有 token，页面上也不存在 CSRF 风险面。
    """
    target = (request.form.get("next") or "").strip()
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


@bp.post("/notice/ack")
def notice_ack():
    """「我已知晓」/「不再显示」。两个按钮都是记同一个版本号。"""
    version = (request.form.get("version") or "").strip() or notice.NOTICE_VERSION
    action = request.form.get("action") or "ok"
    notice.mark_seen(version)
    cloud_state.log(f"分支说明已读（{version}，{action}）")
    return redirect(_after_notice(url_for("settings")))


def wants_json():
    return request.form.get("ajax") == "1" or "application/json" in request.headers.get("Accept", "")


# ---------- 第一步：同意协议 ----------

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
    """撤回同意：连带清掉配对和设备令牌，等于彻底退出。"""
    snapshot = cloud_state.load()
    snapshot["consent"] = {}
    snapshot["device"] = {}
    snapshot["enabled"] = False
    snapshot["hashes"] = {}
    snapshot["pair_code"] = snapshot["pair_hint"] = snapshot["pair_ticket"] = ""
    snapshot["pair_code_at"] = 0.0
    cloud_state.save(snapshot)
    cloud_state.log("用户撤回同意")
    flash("已撤回同意，并清除配对和设备令牌。", "error")
    return back()


# ---------- 第二步：配对 ----------
# （服务器地址是内置常量 cloud/state.py DEFAULT_SERVER，界面上不再让用户填）

@bp.post("/pair")
def pair():
    snapshot = cloud_state.load()
    if not cloud_state.agreed(snapshot):
        flash("您还没有同意数据传输指引。", "error")
        return back()
    try:
        pairing.start(snapshot)
    except pairing.CloudError as exc:
        snapshot["pair_code"] = ""
        snapshot["pair_code_at"] = 0.0
        snapshot["pair_expires_ms"] = 0
        cloud_state.save(snapshot)
        flash(f"配对码生成失败：{exc}", "error")
        return back()
    snapshot["pair_code_at"] = time.time()
    cloud_state.save(snapshot)
    return back()


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


# ---------- 第三步：配对之后才出现的东西 ----------

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
    snapshot["device"] = {}
    snapshot["enabled"] = False
    snapshot["hashes"] = {}
    snapshot["pair_code"] = snapshot["pair_hint"] = snapshot["pair_ticket"] = ""
    snapshot["pair_code_at"] = 0.0
    cloud_state.save(snapshot)
    cloud_state.log("用户解除了配对")
    flash("已解除配对，这台电脑不会再上传任何数据。", "error")
    return back()


@bp.post("/sync-now")
def sync_now():
    snapshot = cloud_state.load()
    if not cloud_state.paired(snapshot):
        flash("您没有进行配对。", "error")
        return back()
    if not snapshot.get("enabled"):
        flash("同步开关已被关闭。", "error")
        return back()
    threading.Thread(target=sync.push_quiet, args=(snapshot,), daemon=True).start()
    flash("正在进行同步，请稍等片刻。", "ok")
    return back()


# ---------- 取回：云端 → 本机 ----------

@bp.get("/pull/preview")
def pull_preview():
    """先看看会拉回几局，不写库。

    为什么要先预览：取回会往 Evo 自己的库里插行。用户点了按钮之前应该知道
    "会插 12 局，跳过 88 局"，而不是点完才发现插了一堆。
    """
    snapshot = cloud_state.load()
    if not cloud_state.paired(snapshot):
        return jsonify({"status": "error", "message": "您没有进行配对。"}), 200
    try:
        stats = pull.pull_into(sync._db_path(), snapshot, dry_run=True)
    except pairing.CloudError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 200
    except Exception as exc:          # 库读不了之类，别让页面 500
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
    """取回统计 → 页面上显示的样子。"""
    return {
        "fetched": int(stats.get("fetched") or 0),
        "added": int(stats.get("added") or 0),
        "skipped": int(stats.get("skipped") or 0),
        "lobby_added": int(stats.get("lobby_added") or 0),
        "lobby_error": stats.get("lobby_error") or "",
        "backup": stats.get("backup") or "",
    }
