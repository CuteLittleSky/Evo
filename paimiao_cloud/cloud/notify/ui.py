"""应用内弹窗（注入到 Evo 面板模板）。

只弹「缓存里、未读、非 silent」里优先级最高的一条；用户点任意按钮/关闭/按 Esc 都记为已读
（POST 到 `/cloud/notify/ack`，带面板口令）。文案全部 html 转义，动作值走白名单校验。
"""
from __future__ import annotations

import html
import json

from cloud.notify import fetch, store

# 本次进程内已经显示过的通知 id。避免「没点知道了就每翻一页都弹」：
# 一次运行里同一条只弹一次；没点已读的话，下次启动 Evo 还会再弹。
_shown: set[str] = set()

_LEVEL_STYLE = {
    "info": "#2b6cb0",
    "success": "#2f855a",
    "warning": "#b7791f",
    "error": "#c53030",
}


def pending_item():
    for item in fetch.cached_items():
        if not isinstance(item, dict):
            continue
        if item.get("silent"):
            continue
        nid = item.get("id")
        if not nid or store.is_seen(nid) or nid in _shown:
            continue
        return item
    return None


def pending() -> bool:
    return pending_item() is not None


def _log(message: str) -> None:
    try:
        from cloud import state as cloud_state  # noqa: PLC0415
        cloud_state.log(message)
    except Exception:
        pass


def _ack_url() -> str:
    try:
        from flask import url_for  # noqa: PLC0415
        return url_for("notify.ack")
    except Exception:
        return "/cloud/notify/ack"


def _token() -> str:
    try:
        from flask import current_app  # noqa: PLC0415
        return str(current_app.config.get("TOKEN") or "")
    except Exception:
        return ""


def _script_json(value) -> str:
    """把值转成可安全嵌入内联 <script> 的 JSON 字面量。

    json.dumps 不转义 `/`，服务端可控的字符串若含 `</script>` 会提前闭合脚本标签，
    后面的内容被当作新标签执行（存储型 XSS）。这里把 `</` 转义成 `<\\/`，
    在 JS 字符串里 `\\/` 与 `/` 等价，所以取值不变，但 HTML 解析器不会再看到闭合标签。
    """
    return json.dumps(value).replace("</", "<\\/")


def render_dialog() -> str:
    item = pending_item()
    if not item:
        return ""
    nid_for_shown = str(item.get("id") or "")
    if nid_for_shown:
        _shown.add(nid_for_shown)
    _log(f"[通知] 弹窗显示：{item.get('title') or nid_for_shown}")
    e = html.escape
    level = item.get("level") if item.get("level") in _LEVEL_STYLE else "info"
    color = _LEVEL_STYLE[level]
    title = e(item.get("title") or "Evo 通知")
    body = e(item.get("body") or "")
    nid = str(item.get("id") or "")
    token = _token()
    ack = _ack_url()

    buttons = []
    for action in item.get("actions") or []:
        label = e(str(action.get("label") or ""))
        kind = str(action.get("type") or "")
        value = e(str(action.get("value") or ""), quote=True)
        if not label or kind not in ("page", "update", "web"):
            continue
        buttons.append(
            f'<button type="button" class="btn btn-primary" data-act="go" '
            f'data-type="{e(kind)}" data-value="{value}">{label}</button>'
        )
    buttons_html = "\n        ".join(buttons)

    js_token = _script_json(token)
    js_id = _script_json(nid)
    js_ack = _script_json(ack)

    return f"""
<style>
  .evo-notice-dialog .dialog-body {{ display: flex; flex-direction: column; gap: 14px; padding: 24px; }}
  .evo-notice-dialog .evo-notice-title {{ margin: 0; font-size: 17px; padding-left: 10px; border-left: 4px solid {color}; }}
  .evo-notice-dialog .evo-notice-body {{ margin: 0; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }}
  .evo-notice-dialog .dialog-actions {{ display: flex; gap: 10px; justify-content: flex-end; flex-wrap: wrap; }}
</style>
<dialog class="dialog evo-notice-dialog" id="evo-notice-dialog" aria-labelledby="evo-notice-title">
  <div class="dialog-body">
    <h2 class="evo-notice-title" id="evo-notice-title" tabindex="-1" autofocus>{title}</h2>
    <p class="evo-notice-body">{body}</p>
    <div class="dialog-actions">
      <button type="button" class="btn-text" data-act="dismiss">知道了</button>
      {buttons_html}
    </div>
  </div>
</dialog>
<script>
  (function () {{
    var d = document.getElementById("evo-notice-dialog");
    if (!d) return;
    if (typeof d.showModal !== "function") return;
    d.showModal();
    d.addEventListener("cancel", function (event) {{ event.preventDefault(); finish("dismiss"); }});

    function ack(action) {{
      try {{
        var body = new URLSearchParams();
        body.set("token", {js_token});
        body.set("id", {js_id});
        body.set("action", action);
        fetch({js_ack}, {{ method: "POST", body: body }}).catch(function () {{}});
      }} catch (e) {{}}
    }}
    function finish(action) {{
      try {{ d.close(); }} catch (e) {{}}
      try {{ d.remove(); }} catch (e) {{}}
      ack(action);
    }}
    var buttons = d.querySelectorAll("[data-act]");
    for (var i = 0; i < buttons.length; i++) {{
      buttons[i].addEventListener("click", function (ev) {{
        var btn = ev.currentTarget;
        var act = btn.getAttribute("data-act");
        if (act === "dismiss") {{ finish("dismiss"); return; }}
        var type = btn.getAttribute("data-type") || "";
        var value = btn.getAttribute("data-value") || "";
        if (type === "web") {{
          if (/^https?:\\/\\//i.test(value)) {{
            try {{ window.open(value, "_blank", "noopener,noreferrer"); }} catch (e) {{}}
          }}
          finish("go");
          return;
        }}
        if ((type === "page" || type === "update") && value.charAt(0) === "/" && value.charAt(1) !== "/") {{
          finish("go");
          try {{ location.href = value; }} catch (e) {{}}
          return;
        }}
        finish("go");
      }});
    }}
  }})();
</script>
"""
