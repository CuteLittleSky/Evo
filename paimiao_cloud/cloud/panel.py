from __future__ import annotations

import time

import app_settings

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from cloud import notice, pairing, pull, qr, sync
from cloud import state as cloud_state

bp = Blueprint("cloud", __name__, url_prefix="/cloud")

MODULE = None
NAV_LABEL = "派喵云同步"

def build():
    return bp

def _stats_on():
    try:
        return not bool(app_settings.get("paimiao_stats_opt_out"))
    except Exception:
        return True

@bp.post("/stats/toggle")
def stats_toggle():
    """匿名统计开关（默认开）。关掉后不再上报；不影响云同步与其它功能。"""
    on = request.form.get("on") == "1"
    nxt = request.form.get("next") or url_for("settings")
    if not str(nxt).startswith("/"):
        nxt = url_for("settings")
    try:
        app_settings.set_value("paimiao_stats_opt_out", not on)
    except Exception as exc:
        flash(f"保存失败：{exc}", "error")
        return redirect(nxt)
    flash("已开启匿名统计。" if on else "已关闭匿名统计，不会再发送任何数据。", "ok")
    return redirect(nxt)

@bp.after_request
def _no_store(response):
    """面板是本地动态页，禁止 WebView2 / 浏览器缓存。
    否则「生成配对码」POST→跳转回 /cloud/ 时可能命中缓存里的旧（未配对）页，
    表现为日志有码、页面却一直显示『生成配对码』。"""
    try:
        response.headers["Cache-Control"] = "no-store"
    except Exception:
        pass
    return response

@bp.app_context_processor
def inject():
    snapshot = cloud_state.load()
    data = {"cloudcard": card_view(snapshot)}
    nav = nav_items()
    if nav:
        data["nav"] = nav
    return data

def nav_items():
    """侧边栏的导航 + 「派喵云同步」。

    上游 0.7.x 起有自己的 `panel_app.nav_items()`（录像页只在打开了录像、或已经有录像时才列出来），
    有它就先用它，不然直接拿 NAV 会把录像那一项的条件绕过去；老版本没有它时照旧读 NAV。"""
    upstream_items = getattr(MODULE, "nav_items", None)
    base = None
    if callable(upstream_items):
        try:
            base = upstream_items()
        except Exception:
            base = None
    if base is None:
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

@bp.get("/legal/<doc>")
def legal_page(doc):
    """在 Evo 面板里看官网的《用户协议》《隐私政策》《版权与免责声明》：
    运行时从 apex.skycraft.cn/legal/<doc>.md 拉取（缓存 10 分钟），和小程序同一份真源。"""
    from cloud import legal  # noqa: PLC0415 避免与 cloud 包初始化互相牵连

    try:
        view = legal.get_doc(doc, force=request.args.get("refresh") == "1")
    except KeyError:
        abort(404)
    return render_template("legal_page.html", view=view)

@bp.post("/terms/accept")
def terms_accept():
    """首次启动协议弹窗：同意《用户协议》《隐私政策》，并按勾选写入匿名统计开关。"""
    from cloud import terms  # noqa: PLC0415

    stats_on = request.form.get("stats_on") == "1"
    terms.accept(stats_on)
    return redirect(_safe_next(request.form.get("next") or "", url_for("settings")))

@bp.post("/terms/decline")
def terms_decline():
    """不同意协议：退出 Evo（首启还没有对局，直接退出不会丢数据）。"""
    from cloud import terms  # noqa: PLC0415

    terms.decline()
    return (
        "<!doctype html><meta charset='utf-8'><title>退出</title>"
        "<body style='font-family:system-ui, sans-serif; padding:40px; color:#333'>"
        "已选择不同意，Evo 正在退出……</body>",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )

def page_view(snapshot):
    agreed = cloud_state.agreed(snapshot)
    paired = cloud_state.paired(snapshot)
    left = seconds_left(snapshot)

    if snapshot.get("pair_code") and left <= 0:
        cloud_state.log(
            "配对诊断 page_view 判定过期并隐藏：码=%s 剩余=%s秒 过期依据=%s"
            % (snapshot.get("pair_code"), left,
               "expires_ms" if snapshot.get("pair_expires_ms") else "pair_code_at"))
        clear_pair_fields(snapshot)
        cloud_state.save(snapshot)
    stats = snapshot.get("stats") or {}
    device = snapshot.get("device") or {}
    data = {
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
        "stats_on": _stats_on(),
    }
    if not agreed:
        try:
            from cloud import legal  # noqa: PLC0415 避免与 cloud 包初始化互相牵连
            data["legal_docs"] = [legal.get_doc("agreement"), legal.get_doc("privacy")]
        except Exception as exc:  # 获取失败也给空列表，页面照常显示、不 500
            data["legal_docs"] = []
            cloud_state.log(f"法务文档获取失败：{exc!r}")
    return data

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

def clear_hashes(snapshot):
    """清掉去重指纹。撤回同意 / 解除配对时要连 lobby/season/weapon 一起清：
    这三项已写进 DEFAULTS 并持久化（见 state.py），只清 hashes 会让重新配对后的
    段位读数、赛季聚合、武器持有因为指纹还在而被跳过，导致云端缺这几块数据。"""
    snapshot["hashes"] = {}
    snapshot["lobby_hashes"] = {}
    snapshot["season_hashes"] = {}
    snapshot["weapon_hashes"] = {}

def seconds_left(snapshot):
    # 以「本机收到配对码的时刻」pair_code_at 为准算剩余：两端都取本机时钟，天然抵消系统时间偏差。
    # 不能先拿服务端的绝对 expiresAt 去减本机 time.time() —— 用户电脑时间只要快 2 分钟，
    # 刚生成的码就会被判成立即过期、被 page_view 静默清掉，表现正是「日志有码、页面不显示」。
    # 真正的过期仍由服务端 settlePair 按它自己的 expires_at 拦（这里只影响倒计时与显隐）。
    at = float(snapshot.get("pair_code_at") or 0)
    if at:
        return max(0, int(pairing.PAIR_TTL - (time.time() - at)))
    expires_ms = int(snapshot.get("pair_expires_ms") or 0)
    if expires_ms:
        return max(0, int((expires_ms / 1000) - time.time()))
    return 0

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
    cloud_state.log("用户同意了《用户协议》与《隐私政策》")
    return back()

@bp.post("/revoke")
def revoke():

    snapshot = cloud_state.load()
    pairing.revoke_qr(snapshot)
    snapshot["consent"] = {}
    snapshot["device"] = {}
    snapshot["enabled"] = False
    clear_hashes(snapshot)
    clear_pair_fields(snapshot)
    cloud_state.save(snapshot)
    cloud_state.log("用户撤回同意")
    flash("已撤回同意，并清除配对和设备令牌。", "error")
    return back()

@bp.post("/pair")
def pair():
    snapshot = cloud_state.load()
    if not cloud_state.agreed(snapshot):
        if wants_json():
            return jsonify({"status": "error", "message": "您还没有同意《用户协议》与《隐私政策》。"}), 200
        flash("您还没有同意《用户协议》与《隐私政策》。", "error")
        return back()
    pairing.revoke_qr(snapshot)
    try:
        code = pairing.start(snapshot)
    except pairing.CloudError as exc:
        clear_pair_fields(snapshot)
        cloud_state.save(snapshot)
        cloud_state.log(f"生成配对码失败：{exc}")
        if wants_json():
            return jsonify({"status": "error", "message": f"配对码生成失败：{exc}"}), 200
        flash(f"配对码生成失败：{exc}", "error")
        return back()
    snapshot["pair_code_at"] = time.time()
    cloud_state.save(snapshot)
    # 诊断：保存后立刻回读一次，确认「GET /cloud/ 渲染那一侧」到底看不看得到这张码
    # （若回读码为空或剩余≤0，就能一眼解释“日志有码、页面不显示”）
    reloaded = cloud_state.load()
    cloud_state.log(
        "配对诊断 保存后回读：state=%s 存盘码=%s 回读码=%s 剩余=%s秒"
        % (cloud_state.state_path(), code, reloaded.get("pair_code") or "", seconds_left(reloaded)))
    cloud_state.log(f"用户点了生成配对码（{code}，{pairing.PAIR_TTL} 秒内有效）")
    if wants_json():
        # 直接把码渲染成片段返回，前端无需再靠「跳转→重新读 state.json」来显示
        view = page_view(cloud_state.load())
        try:
            html = render_template("_pair_panel.html", view=view)
        except Exception as exc:
            cloud_state.log(f"配对诊断 渲染片段失败：{exc!r}")
            return jsonify({"status": "error",
                            "message": "配对码已生成，但页面渲染失败，请刷新重试"}), 200
        return jsonify({"status": "ok", "left": view.get("pair_left", 0), "html": html})
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
        # 绑定后：把本机固定匿名编号报给服务端，删掉之前的匿名数据（后台、幂等、失败不影响配对）
        try:
            import threading as _threading
            from cloud.stats import report as _stats_report
            _threading.Thread(target=_stats_report.purge_if_bound, daemon=True).start()
        except Exception:
            pass
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
    clear_hashes(snapshot)
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
