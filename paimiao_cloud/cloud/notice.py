from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

from cloud import state as cloud_state

NOTICE_VERSION = "2026-10-22"

# 「推荐绑定派喵云同步」弹窗的版本。改了推荐文案或想再问一次老用户时改这里。
RECOMMEND_VERSION = "2026-10-27"

SOURCE_URL = "https://github.com/CuteLittleSky/Evo"
ISSUES_URL = "https://github.com/CuteLittleSky/Evo/issues"
UPSTREAM_URL = "https://github.com/AugustHuangMX/Evo"

MAINTAINER = "派喵小程序作者"

LICENSE_ANCHOR = "/settings#h-branch"
BRANCH_ANCHOR = "h-branch"

UPSTREAM_COPYRIGHT = "Copyright (c) 2026 AugustHuangMX"

MIT_TEXT = """GNU AFFERO GENERAL PUBLIC LICENSE
Version 3, 19 November 2007

Copyright (c) 2026 AugustHuangMX

本程序（含上游代码与本分支修改）依据 GNU Affero General Public License v3.0（AGPL-3.0）分发。
上游代码在并入本程序时原依据 MIT License 授权，其版权声明已按许可要求保留。
完整许可证文本见仓库根目录的 LICENSE 文件，或访问 https://www.gnu.org/licenses/agpl-3.0.txt。

「派喵云同步」所连接的派喵后端服务是独立开发的软件，
仅通过公开 API 与本程序交互，不受本许可证约束，不开放源代码。"""

_LICENSE_FILE = Path(__file__).resolve().parent.parent.parent / "LICENSE"

def license_text() -> str:
    try:
        text = _LICENSE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return MIT_TEXT

    return text if "AFFERO" in text.upper() else MIT_TEXT

def seen() -> str:
    return cloud_state.load().get("notice_seen") or ""

_SHOWN = False

def should_show() -> bool:

    if os.environ.get("EVO_NO_NOTICE") == "1":
        return False
    return not _SHOWN and seen() != NOTICE_VERSION

def mark_seen(version: str | None = None) -> None:
    snapshot = cloud_state.load()
    snapshot["notice_seen"] = version or NOTICE_VERSION
    cloud_state.save(snapshot)

def dialog_html(version: str = "", token: str = "") -> str:

    e = html.escape
    ver = e(version or NOTICE_VERSION)
    js_token = json.dumps(token or "")
    return f"""
<style>
  /* 上游 .dialog 是 padding:0，内边距由 .dialog form 提供（:176）。
     我们故意不用 form（点按钮不该提交表单），所以自己补一份同样的留白。 */
  .branch-dialog .dialog-body {{ display: flex; flex-direction: column; gap: 16px; padding: 24px; }}
  /* 上游 a 选择器是 "color: inherit"（style.css:88）→ 不显式上色的话，
     正文里的链接和普通文字一个颜色，用户根本认不出那是能点的。 */
  .branch-dialog a {{ color: var(--p3-bg); text-decoration: underline; text-underline-offset: 3px; }}
  .branch-dialog a:hover {{ text-decoration-thickness: 2px; }}
</style>
<dialog class="dialog branch-dialog" id="branch-dialog" aria-labelledby="h-branch-dialog">
  <div class="dialog-body">
    <h2 id="h-branch-dialog" tabindex="-1" autofocus>Evo 派喵云同步版本 · 非官方分支说明</h2>
    <p class="dialog-desc">
      本软件是 <strong>Evo 的非官方修改分支</strong>，由 {e(MAINTAINER)} 维护，
      与上游项目（<a href="{e(UPSTREAM_URL)}" target="_blank" rel="noopener noreferrer">AugustHuangMX/Evo</a>）
      及其作者无隶属、合作或背书关系。
    </p>
    <p class="dialog-desc">
      本分支新增了「派喵云同步」功能（<strong>可选</strong>）。该功能涉及战绩数据上传，详见派喵云同步页
      <a href="/cloud/">《派喵云同步 · 数据传输指引》</a>。
    </p>
    <p class="dialog-desc">
      使用中如遇问题，请优先向本分支维护者反馈：
      <a href="{e(ISSUES_URL)}" target="_blank" rel="noopener noreferrer">GitHub Issues</a>。
      请勿将本分支特有问题直接提交至上游；上游项目作者不对本分支的修改、新增功能及数据行为负责。
    </p>
    <p class="dialog-desc">
      本分支依据 <a href="{e(LICENSE_ANCHOR)}">AGPL-3.0 License</a> 分发，并保留原项目的版权声明与许可证文本。
      完整许可证及修改说明见<a href="{e(LICENSE_ANCHOR)}">「关于 / 第三方许可」</a>。
    </p>
    <div class="dialog-actions">
      <button type="button" class="btn-text" data-notice="never">不再显示</button>
      <button type="button" class="btn btn-primary" data-notice="ok">我已知晓</button>
    </div>
  </div>
</dialog>
<script>
  (function () {{
    var dialog = document.getElementById("branch-dialog");
    if (!dialog) return;
    if (typeof dialog.showModal !== "function") return;   // 老引擎：不弹，别留个关不掉的黑块
    dialog.showModal();
    dialog.querySelector('[data-notice="ok"]').focus();

    function close() {{
      dialog.close();
      dialog.remove();                                    // 连占位节点一起拿掉
    }}

    // ------------------------------------------------------------------
    // 两个按钮都在**本地**处理，不发任何多余请求。
    //
    // 一开始这里是 `type="submit"` 的表单，点完 POST /cloud/notice/ack。
    // 那样有两个问题：① 用户还没同意任何东西，就已经在用他的电脑发请求了；
    // ② 在 Evo 的 WebView2 窗口里，表单提交会把页面导走，点一下黑屏。
    //
    // 现在：先把弹窗关掉，再顺手告诉本机"看过了"（就这一个请求，
    // 目标是 127.0.0.1 自己）。失败也不拦着用户 —— 大不了下次再弹。
    // ------------------------------------------------------------------
    function ack(action) {{
      close();
      var body = new URLSearchParams();
      body.set("action", action);
      body.set("version", {json.dumps(ver)});
      body.set("token", {js_token});
      fetch("/cloud/notice/ack", {{ method: "POST", body: body }}).catch(function () {{}});
    }}

    dialog.querySelector('[data-notice="ok"]').addEventListener("click", function () {{ ack("ok"); }});
    dialog.querySelector('[data-notice="never"]').addEventListener("click", function () {{ ack("never"); }});

    // 按 Esc 和点「我已知晓」一样：说明已经看到了，不用每次开窗都拦一下。
    dialog.addEventListener("cancel", function (event) {{
      event.preventDefault();
      ack("ok");
    }});
  }})();
</script>
"""

def recommend_html() -> str:

    return """
<style>
  /* 上游 .dialog 是 padding:0，内边距由 .dialog form 提供（:176）。
     我们不用 form 包住弹窗，所以自己补一份同样的留白。 */
  .recommend-dialog .dialog-body { display: flex; flex-direction: column; gap: 16px; padding: 24px; }
</style>
<dialog class="dialog recommend-dialog" id="cloud-recommend" aria-labelledby="h-cloud-recommend">
  <div class="dialog-body">
    <h2 id="h-cloud-recommend" tabindex="-1" autofocus>把战绩同步到手机</h2>
    <p class="dialog-desc">
      Evo 现在只把战绩记在这台电脑上。绑定派喵云同步以后，每一局结束都会自动传到云端，
      用微信小程序「派喵」随时在手机上看名次、击杀、伤害和排位分。
    </p>
    <p class="dialog-desc">
      功能是可选的，不绑定也照常记录。绑定以后随时可以在同一页解绑，解绑后不再上传任何数据。
    </p>
    <div class="dialog-actions">
      <button type="submit" form="cloud-recommend-later" class="btn-text" data-recommend="later">以后再说</button>
      <button type="submit" form="cloud-recommend-go" class="btn btn-primary" data-recommend="go">去绑定</button>
    </div>
  </div>
</dialog>
<!-- 两个按钮都用表单提交，服务端顺手记下已看过（.btn / .btn-text 的类选择器对 button 同样生效）：
     ① 没有 form 包住弹窗，所以按钮用 form 属性关联，样式和布局都不变；
     ② 「去绑定」也记已看过（next 指向云同步页），点过一次以后不再弹。
     注意：这里必须用 HTML 注释，不能用 Jinja 的 {# #}：recommend_html() 是作为
     变量用 `| safe` 输出的，Jinja 不会再解析它的内容，写 {# #} 只会原样显示在页面上。 -->
<form method="post" action="__LATER_URL__" id="cloud-recommend-later">
  <!-- 上游 security_checks（code/panel_app.py:156）要求所有 POST 带口令，不带就 403 -->
  <input type="hidden" name="token" value="__TOKEN__">
  <input type="hidden" name="next" value="__NEXT_PATH__">
</form>
<form method="post" action="__LATER_URL__" id="cloud-recommend-go">
  <!-- 同上：POST 要带口令；next 指向云同步页，点「去绑定」= 记已看过 + 跳过去 -->
  <input type="hidden" name="token" value="__TOKEN__">
  <input type="hidden" name="next" value="__CLOUD_URL__">
</form>
<script>
  (function () {
    var dialog = document.getElementById("cloud-recommend");
    if (!dialog) return;
    if (typeof dialog.showModal !== "function") return;   // 老引擎：不弹，别留个关不掉的黑块
    dialog.showModal();
    dialog.querySelector('[data-recommend="go"]').focus();

    // 只是推荐，不是条款：点「去绑定」提交表单（记下已看过，再跳到云同步页），
    // 点「以后再说」或按 Esc 关掉，并告诉本机「看过了」，以后不再打扰。
    // 按 Esc 和点「以后再说」一样：也算看过了，下次开窗不再拦一下。
    dialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      document.querySelector('[data-recommend="later"]').click();
    });
  })();
</script>
"""

_CARD_TEMPLATE = Path(__file__).resolve().parent / "templates" / "branch_card.html"

_CARD_CSS = """
<style>
  /* ---------- 折叠头 ---------- */
  /* .settings-card h2 自带 margin:0 0 16px，折叠时下面没有内容，
     那 16px 会把卡片撑出一截空白，所以收掉。 */
  .branch-card .branch-summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; cursor: pointer; }
  .branch-card .branch-summary h2 { margin: 0; }
  .branch-card .branch-summary::-webkit-details-marker { display: none; }
  .branch-card .branch-summary { list-style: none; }
  .branch-card .branch-summary:focus-visible { outline: 2px solid var(--info-text); outline-offset: 2px; border-radius: 4px; }
  /* 折叠提示：文案靠这两个 span 切换，箭头靠 details[open] 旋转，都不用 JS。 */
  .branch-toggle { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); }
  .branch-toggle svg { transition: transform .15s ease; }
  .branch-card details[open] .branch-toggle svg { transform: rotate(180deg); }
  .t-open { display: none; }
  .branch-card details[open] .t-closed { display: none; }
  .branch-card details[open] .t-open { display: inline; }
  .branch-card .branch-summary:hover .branch-toggle { color: var(--text); }

  /* ---------- 正文 ---------- */
  .branch-body { margin-top: 16px; }
  /* 上游 style.css:88 是 `a { color: inherit }` —— 不显式上色的话，
     链接和正文一个颜色，用户根本认不出那行字能点。照 .source-link(:427) 的色。 */
  .branch-card a { color: var(--p3-bg); text-decoration: underline; text-underline-offset: 3px; }
  .branch-card a:hover { text-decoration-thickness: 2px; }
  .branch-card a:focus-visible { outline: 2px solid var(--p3-bg); outline-offset: 2px; border-radius: 4px; }
  .branch-lead { margin: 0 0 16px; line-height: 1.7; }
  .branch-facts { display: grid; grid-template-columns: max-content 1fr; gap: 8px 16px; margin: 0 0 20px; }
  .branch-facts dt { color: var(--muted); }
  .branch-facts dd { margin: 0; line-height: 1.6; overflow-wrap: anywhere; }
  .branch-sub { margin: 18px 0 8px; font-size: 14px; font-weight: 600; }
  .branch-copy { margin: 0 0 6px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
  .branch-note { margin: 0 0 8px; color: var(--muted); font-size: 13px; line-height: 1.7; }
  .branch-license {
    margin: 0 0 16px; padding: 14px; border: 1px solid var(--input-border); border-radius: 6px;
    background: color-mix(in srgb, var(--info-text) 6%, transparent);
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 12px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere;
  }
</style>
"""

def card_html(branch: dict) -> str:

    from jinja2 import Environment

    source = _CARD_TEMPLATE.read_text(encoding="utf-8")
    env = Environment(autoescape=True)
    body = env.from_string(source).render(branch=branch, branch_anchor=BRANCH_ANCHOR)
    return _CARD_CSS + "\n" + body

_DIALOG_MARK = "{% block scripts %}"

_DIALOG_SENTINEL = 'id="branch-dialog"'
_RECOMMEND_SENTINEL = 'id="cloud-recommend"'

# 源码里可见的幂等标记（见 inject_recommend 注释）
_RECOMMEND_MARK = "cloud_recommend_dialog()"
_CARD_SENTINEL = f'id="{BRANCH_ANCHOR}"'

_CARD_INJECT_AT = '<div class="settings-main">'
_BACKREF = re.compile(r"""\{%-?\s*(?:extends|include)\s+(["'])(.+?)\1""")

def inject_dialog(source: str, version: str = "") -> str:

    if not source or _DIALOG_SENTINEL in source:
        return source
    at = source.find(_DIALOG_MARK)
    if at < 0:
        return source
    block = (
        "{% if branch_notice_pending() %}"

        "{{ branch_notice_dialog(token|default('')) | safe }}"
        "{% endif %}"
    )
    return source[:at] + block + source[at:]

def inject_recommend(source: str) -> str:

    # 幂等标记只能用**模板源码里看得见**的东西：弹窗的 HTML 是渲染期才展开的，
    # 源码里根本没有 id="cloud-recommend"，拿它当哨兵会重复插（踩过）。
    if not source or _RECOMMEND_MARK in source:
        return source
    at = source.find(_DIALOG_MARK)
    if at < 0:
        return source
    block = "{% if cloud_recommend_pending() %}" "{{ cloud_recommend_dialog() | safe }}" "{% endif %}"
    return source[:at] + block + source[at:]

def inject_card(source: str, branch: dict) -> str:

    if not source or _CARD_SENTINEL in source:
        return source
    anchor = _CARD_INJECT_AT
    at = source.find(anchor)
    if at < 0:
        return source
    if source.find(_CARD_INJECT_AT, at + 1) >= 0:
        return source
    pos = at + len(anchor)
    depth = 1

    for match in re.finditer(r"<div\b[^>]*>|</div>", source[pos:], re.I):
        if match.group(0).lower().startswith("</div"):
            depth -= 1
            if depth == 0:
                seam = pos + match.start()
                return source[:seam] + card_html(branch) + source[seam:]
        else:
            depth += 1
    return source

def patch_source(source: str, template: str, branch: dict, version: str = "") -> str:

    # 2026-10-25 用户决定：不再弹启动说明（上游 0.4.0 的引导页已经够多了），
    # 设置页的「关于本分支 / 第三方许可」卡片保留。inject_dialog / dialog_html /
    # should_show / mark_seen 的代码留着不删 —— /cloud/notice/ack 路由还在用它
    # （已看过记录），想恢复弹窗时把下面这行 if 打开即可。
    if False:
        source = inject_dialog(source, version)
    # 2026-10-27 用户决定：启动说明改成「推荐绑定派喵云同步」，点按钮直接去 /cloud/。
    source = inject_recommend(source)
    if template == "settings.html":
        source = inject_card(source, branch)
    return source

def _referenced_templates(source: str) -> list[str]:

    return [match.group(2) for match in _BACKREF.finditer(source or "")]

def register(app, module=None):

    _install_preprocess(app.jinja_env, module)
    return ["notice_preprocess"]

def _dialog_for_template(token="", version="") -> str:

    global _SHOWN
    _SHOWN = True
    return dialog_html(version=version or NOTICE_VERSION, token=str(token or ""))

def _recommend_for_render() -> str:

    # 地址和当前路径都在渲染时现算（拿不到就用固定值，免得把整页带崩）。
    try:
        from flask import url_for
        target = url_for("cloud.page")
    except Exception:
        target = "/cloud/"
    try:
        from flask import url_for
        later = url_for("cloud.recommend_dismiss")
    except Exception:
        later = "/cloud/recommend/dismiss"
    try:
        from flask import request
        here = (request.full_path or "/").rstrip("?")
        if not (here.startswith("/") and not here.startswith("//")):
            here = ""
    except Exception:
        here = ""
    page = html.escape(here, quote=True)
    try:
        from flask import current_app
        token = str(current_app.config.get("TOKEN") or "")
    except Exception:
        token = ""
    token = html.escape(token, quote=True)

    return (
        recommend_html()
        .replace("__CLOUD_URL__", target)
        .replace("__LATER_URL__", later)
        .replace("__NEXT_PATH__", page)
        .replace("__TOKEN__", token)
    )

def recommend_pending() -> bool:

    if os.environ.get("EVO_NO_NOTICE") == "1":
        return False
    # 已经在「派喵云同步」页本身：别再拿推荐弹窗把它自己挡住。否则点「去绑定」跳到这页后
    # 弹窗又盖上来，看起来像点了没反应（推荐只在别的页面出现）。
    try:
        from flask import request
        here = (request.path or "").rstrip("/")
    except Exception:
        here = ""
    if here == "/cloud":
        return False
    snapshot = cloud_state.load()
    # 已经同意并配对成功的用户不用再推荐一次；跟卡片一致，以本机快照为准。
    if cloud_state.agreed(snapshot) and cloud_state.paired(snapshot):
        return False
    # 点过「以后再说」就按版本记住：文案/功能变了（升 RECOMMEND_VERSION）才会再问一次。
    return snapshot.get("recommend_seen") != RECOMMEND_VERSION

def mark_recommend_seen() -> None:

    snapshot = cloud_state.load()
    snapshot["recommend_seen"] = RECOMMEND_VERSION
    cloud_state.save(snapshot)
    cloud_state.log(f"推荐绑定弹窗已关闭（{RECOMMEND_VERSION}）")

def _install_preprocess(env, module):
    if getattr(env, "_paimiao_notice_preprocess", False):
        return []
    original = env.preprocess
    branch = branch_view(module)

    def preprocess(source, name, filename=None):

        try:
            source = original(source, name, filename)
        except TypeError:
            source = original(source, name)
        return patch_source(source, name, branch, NOTICE_VERSION)

    env.preprocess = preprocess
    env._paimiao_notice_preprocess = True

    env.globals["branch_notice_pending"] = should_show
    env.globals["branch_notice_dialog"] = _dialog_for_template
    env.globals["cloud_recommend_pending"] = recommend_pending
    env.globals["cloud_recommend_dialog"] = _recommend_for_render
    return ["notice_preprocess"]

def branch_view(module=None) -> dict:

    version = ""
    try:
        import storage
        version = str(getattr(storage, "APP_VERSION", "") or "")
    except Exception:
        pass
    return {
        "name": "Evo 派喵云同步版",
        "upstream_version": version,
        "maintainer": MAINTAINER,
        "source_url": SOURCE_URL,
        "issues_url": ISSUES_URL,
        "upstream_url": UPSTREAM_URL,
        "copyright": UPSTREAM_COPYRIGHT,
        "license_text": license_text(),
        "notice_version": NOTICE_VERSION,
    }
