"""从派喵获取的段位（只读）：总览页段位卡片里加一小块。

2026-11-04 用户要求：放在总览页的段位卡片里，**不和 Evo 自己读到的段位合并**
（来源不同，混在一起就分不出来了）；界面上只写「从派喵获取的段位」。

做法（纯注入，上游文件一个字节不动）：
  ① 蓝图 `GET /paimiaorank`：把派喵那边给的段位整理成页面要的文字；
  ② 模板 preprocess：在 `overview.html` 的 `<div class="mini-stats">` 前面插一小块，
     页面打开后由一小段脚本去读 ①；拿不到数据时整块藏起来。

为什么用脚本异步读、不在服务端渲染时就取：取一次要连网络（超时 8 秒），
放在渲染路径上会让总览页卡住；异步读失败就整块不显示，页面照常。
"""
from __future__ import annotations

import time

from flask import Blueprint, jsonify

from cloud import pairing
from cloud import state as cloud_state

bp = Blueprint("paimiaorank", __name__)

# 插在总览页段位卡片里「本赛季 RP / 今日 RP / 今日局数」那一排前面
_ANCHOR = '<div class="mini-stats">'

_BLOCK = """<div class="paimiaorank" id="paimiaorank-box" hidden>
        <div class="label">从派喵获取的段位</div>
        <div id="paimiaorank-body" class="muted" style="margin-top:4px"></div>
      </div>
      <script>
      (function () {
        var box = document.getElementById("paimiaorank-box");
        var body = document.getElementById("paimiaorank-body");
        if (!box || !body) return;
        fetch({{ url_for('paimiaorank.paimiaorank')|tojson }}, { headers: { "Accept": "application/json" } })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (!data || data.status !== "ok") return;
            var v = data.view || {};
            if (!v.rank) return;      // 没绑 / 还没数据：整块不显示
            var text = v.rank;
            if (v.score !== null && v.score !== undefined) text += "，总分 " + v.score;
            if (v.player) text += "（" + v.player + "）";
            if (v.scanned) text += " · 更新于 " + v.scanned;
            if (v.stale) text += "（超过 48 小时没更新）";
            body.textContent = text;
            box.hidden = false;
          })
          .catch(function () { /* 读不到就整块不显示，页面照常 */ });
      })();
      </script>
      """


def register(app):
    """注册蓝图 + 模板 preprocess。幂等；失败由调用方兜住（不影响其他页面）。"""
    if "paimiaorank" not in app.blueprints:
        app.register_blueprint(bp)
    _install_preprocess(app.jinja_env)
    return ["paimiaorank"]


def _install_preprocess(env):
    if getattr(env, "_paimiao_rank_preprocess", False):
        return
    original = env.preprocess

    def preprocess(source, name, filename=None):
        source = original(source, name, filename)
        if (name or filename or "").endswith("overview.html"):
            source = _insert_block(source)
        return source

    env.preprocess = preprocess
    env._paimiao_rank_preprocess = True


def _insert_block(source):
    """把那一块插到总览页段位卡片的 mini-stats 前面。找不到锚点就原样返回（降级）。"""
    if "paimiaorank-box" in source or _ANCHOR not in source:
        return source
    return source.replace(_ANCHOR, _BLOCK + _ANCHOR, 1)


@bp.get("/paimiaorank")
def paimiaorank():
    """从派喵获取的段位（只读）：只做展示，不写进本机战绩。"""
    snapshot = cloud_state.load()
    if not cloud_state.paired(snapshot):
        return jsonify({"status": "error", "message": "您没有进行配对。"}), 200
    try:
        data = pairing.download_paimiao_rank(snapshot)
    except pairing.CloudError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 200
    except Exception as exc:
        return jsonify({"status": "error",
                        "message": f"{type(exc).__name__}: {exc}"[:200]}), 200
    return jsonify({"status": "ok", "view": paimiao_rank_view(data)})


def paimiao_rank_view(data):
    """把派喵给的字段整理成页面要显示的文字（认不出的就留空，不猜）。"""
    view = {
        "available": bool(data.get("available", True)),
        "bound": bool(data.get("bound")),
        "pending": bool(data.get("pending")),
        "rank": "",
        "score": None,
        "player": data.get("matchedName") or data.get("playerName") or "",
        "scanned": "",
        "stale": bool(data.get("stale")),
    }
    if not view["available"] or not view["bound"] or view["pending"]:
        return view

    # 段位名用派喵给的中文名；段用罗马数字（派喵那边已经换算好）
    tier = data.get("rankTierCn") or data.get("rankTier") or ""
    div = data.get("rankDivRoman") or ""
    progress = data.get("rankProgress")
    text = str(tier)
    if div:
        text += " " + str(div)
    if progress not in (None, ""):
        text += " · " + str(progress)
    view["rank"] = text.strip(" ·")
    score = data.get("score")
    if score not in (None, ""):
        try:
            view["score"] = int(score)
        except (TypeError, ValueError):
            view["score"] = None
    scanned = data.get("scannedAt")
    if scanned:
        try:
            view["scanned"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(scanned) / 1000))
        except (TypeError, ValueError, OSError):
            view["scanned"] = ""
    return view
