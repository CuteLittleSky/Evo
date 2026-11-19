"""首次启动「协议弹窗」：全应用强制同意《用户协议》《隐私政策》，并附匿名统计开关。

- 第一次启动（或 `TERMS_VERSION` 变了）弹一个不可关闭的模态框，正文是从官网
  `/legal/*.md` 实时拉取并渲染的《用户协议》《隐私政策》全文（复用 cloud.legal）。
- 点「同意并继续」：记下 terms_seen，同时按《隐私政策》视为已同意云同步的数据上传
  （consent.agreed），并把匿名统计开关写进 app_settings（默认开，可取消勾选）。
- 点「不同意」：写日志，结束记录/窗口子进程后退出 Evo（首启还没有对局，直接退出不会丢数据）。
- 同意之后才会轮到「把战绩同步到手机」的推荐弹窗（见 notice.patch_source 的取舍）。

布局（2026-11-20 用户反馈「界面太长，同意按钮要下拉」）：弹窗固定高度，只有中间
《用户协议》《隐私政策》正文区滚动；标题、说明、匿名统计、按钮都常驻可见。
"""

from __future__ import annotations

import html
import os
import threading
import time
from datetime import datetime

from cloud import state as cloud_state

TERMS_VERSION = "2026-11-20"


def pending() -> bool:
    if os.environ.get("EVO_NO_NOTICE") == "1":
        return False
    return (cloud_state.load().get("terms_seen") or "") != TERMS_VERSION


def _docs_inner() -> str:
    """《用户协议》《隐私政策》全文（渲染成 HTML）；取不到时给可重试的提示。"""
    try:
        from cloud import legal  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        return f'<p class="terms-err">暂时获取不到《用户协议》《隐私政策》：{html.escape(str(exc))}</p>'
    parts = []
    for doc in ("agreement", "privacy"):
        item = legal.get_doc(doc)
        parts.append(f'<h3 class="terms-doc-title">{html.escape(item["title"])}</h3>')
        if item.get("html"):
            parts.append(item["html"])
        else:
            parts.append(
                '<p class="terms-err">暂时获取不到《' + html.escape(item["title"]) + '》：'
                + html.escape(item.get("error") or "网络异常")
                + '</p><p class="terms-err"><a href="/cloud/legal/'
                + html.escape(doc) + '?refresh=1" target="_blank">重试</a></p>'
            )
    return "\n".join(parts)


def _token() -> str:
    try:
        from flask import current_app

        return str(current_app.config.get("TOKEN") or "")
    except Exception:
        return ""


def _next_path() -> str:
    try:
        from flask import request

        here = (request.full_path or "/").rstrip("?")
        if here.startswith("/") and not here.startswith("//"):
            return here
    except Exception:
        pass
    return ""


def dialog_html() -> str:
    token = html.escape(_token(), quote=True)
    next_path = html.escape(_next_path(), quote=True)
    return f"""
<style>
  /* 弹窗固定高度：标题/说明/统计/按钮都不动，只有中间正文区滚动 */
  .terms-dialog {{ width: min(720px, 92vw); max-height: 90vh; overflow: hidden; }}
  .terms-dialog .dialog-body {{
    display: flex; flex-direction: column; gap: 12px;
    padding: 22px 26px; height: min(86vh, 760px); max-height: 86vh; box-sizing: border-box;
  }}
  .terms-dialog h2 {{ margin: 0; font-size: 17px; flex: 0 0 auto; }}
  .terms-intro {{ margin: 0; font-size: 13px; line-height: 1.65; color: var(--muted); flex: 0 0 auto; }}

  .terms-scroll {{
    flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 14px 16px;
    border: 1px solid var(--input-border); border-radius: 6px;
    background: var(--inset); font-size: 13px; line-height: 1.75;
  }}
  .terms-scroll .terms-doc-title {{ margin: 0 0 8px; font-size: 14px; font-weight: 700; }}
  .terms-scroll .terms-doc-title:not(:first-child) {{ margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--input-border); }}
  .terms-scroll h2 {{ margin: 16px 0 6px; font-size: 13px; font-weight: 600; }}
  .terms-scroll h3 {{ margin: 14px 0 6px; font-size: 13px; font-weight: 600; }}
  .terms-scroll p {{ margin: 0 0 8px; }}
  .terms-scroll ul {{ margin: 6px 0; padding-left: 20px; }}
  .terms-scroll li {{ margin: 3px 0; }}
  .terms-scroll a {{ color: var(--p3-bg); text-decoration: underline; text-underline-offset: 3px; }}
  .terms-scroll hr {{ margin: 12px 0; border: 0; border-top: 1px solid var(--input-border); }}
  .terms-scroll code {{ padding: 1px 4px; border-radius: 4px; background: var(--input-border); font-size: 12px; }}
  .terms-err {{ color: var(--muted); }}

  .terms-stats {{ flex: 0 0 auto; padding: 10px 14px; border: 1px solid var(--input-border); border-radius: 6px; background: var(--inset); }}
  .terms-stats p {{ margin: 0 0 8px; font-size: 13px; line-height: 1.6; }}
  .terms-agree {{ display: flex; align-items: flex-start; gap: 10px; font-size: 13px; line-height: 1.6; }}
  .terms-agree input {{ margin-top: 3px; flex: none; }}

  .terms-actions {{ flex: 0 0 auto; display: flex; justify-content: flex-end; gap: 12px; }}
  .terms-actions form {{ display: inline; margin: 0; }}
</style>
<dialog class="dialog terms-dialog" id="terms-dialog" aria-labelledby="h-terms-dialog">
  <div class="dialog-body">
    <h2 id="h-terms-dialog" tabindex="-1" autofocus>使用前请阅读并同意</h2>
    <p class="terms-intro">
      欢迎使用 派喵战绩记录器。使用前请阅读并同意下面的《用户协议》与《隐私政策》；不同意将无法使用本软件。
      同意即表示接受这两份文档；开启「派喵云同步」后，战绩将按《隐私政策》第 2.4 节所述上传。
    </p>
    <div class="terms-scroll" tabindex="0">{_docs_inner()}</div>
    <div class="terms-stats">
      <p><strong>匿名统计</strong>：仅发送模式、地图、传奇、名次、击杀/伤害、RP 变化等，不含游戏名、ID、截图；数据仅用于维护派喵APEX工具数据库及优化程序体验，后续可在设置关闭。</p>
      <label class="terms-agree">
        <input type="checkbox" name="stats_on" value="1" form="terms-accept-form" checked>
        <span>参与匿名统计</span>
      </label>
    </div>
    <div class="terms-actions">
      <form method="post" action="/cloud/terms/decline" id="terms-decline-form">
        <input type="hidden" name="token" value="{token}">
        <button type="submit" class="btn-text">不同意</button>
      </form>
      <form method="post" action="/cloud/terms/accept" id="terms-accept-form">
        <input type="hidden" name="token" value="{token}">
        <input type="hidden" name="next" value="{next_path}">
        <button type="submit" class="btn btn-primary">同意并继续</button>
      </form>
    </div>
  </div>
</dialog>
<script>
  (function () {{
    var dialog = document.getElementById("terms-dialog");
    if (!dialog) return;
    if (typeof dialog.showModal !== "function") return;
    dialog.showModal();
    var ok = document.querySelector("#terms-accept-form button");
    if (ok) ok.focus();
    // 不可关闭：Esc / 点遮罩都不关，必须选同意或不同意
    dialog.addEventListener("cancel", function (event) {{ event.preventDefault(); }});
  }})();
</script>
"""


def accept(stats_on: bool) -> None:
    snapshot = cloud_state.load()
    snapshot["terms_seen"] = TERMS_VERSION
    # 同意本协议即视为已同意《隐私政策》所述的数据处理；云同步仍需用户去配对才会上传
    snapshot["consent"] = {
        "agreed": True,
        "version": cloud_state.CONSENT_VERSION,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    cloud_state.save(snapshot)
    try:
        import app_settings  # noqa: PLC0415 上游模块

        app_settings.set_value("paimiao_stats_opt_out", not stats_on)
    except Exception:
        pass
    cloud_state.log(f"用户同意《用户协议》《隐私政策》（匿名统计{'开启' if stats_on else '关闭'}）")


def decline() -> None:
    cloud_state.log("用户不同意《用户协议》与《隐私政策》，Evo 将退出")

    def _quit():
        time.sleep(0.8)  # 先把响应/提示送回页面，再退
        try:
            import multiprocessing  # noqa: PLC0415

            for proc in multiprocessing.active_children():
                try:
                    proc.terminate()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            import sys

            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_quit, name="terms-decline-quit", daemon=True).start()
