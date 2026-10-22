"""Evo 派喵云同步 · 分支说明弹窗 + 许可信息（纯注入，上游 0 文件改动）。

为什么要有这个文件
------------------
Evo 上游是 MIT 许可。MIT 允许修改和再分发，但有两条硬性要求：

    1. 再分发的副本里必须**保留原版权声明和许可文本**（"The above copyright
       notice and this permission notice shall be included in all copies or
       substantial portions of the Software."）。
    2. 不能暗示原作者为我们的修改背书。

派喵版是一个修改过的分支。只在磁盘上放一个 LICENSE、界面里不出现，用户是
看不见的；而界面上写着"关于 Evo"，容易被理解成这就是上游作者的作品。
所以这里做两件事：

    ① 首次启动弹一次「非官方分支说明」，说清我们是谁、和上游什么关系、
       遇到问题找谁，并给出许可证入口。
    ② 在设置页注入一张「关于本分支 / 第三方许可」卡片，把版权声明和 MIT
       许可全文放在用户点得到的地方。

怎么做到「不碰上游文件」
------------------------
两条都靠 jinja 的 preprocess 钩子改**内存里的模板源码**，磁盘上一个字节都不动：

    弹窗   插在 base.html 的 `{% block scripts %}{% endblock %}` 之前
           （base.html:317），这样每个页面渲染完就能 showModal()。
    卡片   把 settings.html 里的 `{% endblock %}` 逐层的配对算出来，
           在 content 块收尾的那个之前插一段 HTML。
           （不能用 `{% endblock %}` 的最后一个：文件末尾还有 scripts 块。）

上游哪天改了模板结构，配对算不出来就**什么都不插**，页面照常显示 ——
宁可少一块说明，也不能把 Evo 弄坏。

"已读"记在 state.json 而不是 localStorage
------------------------------------------
和派喵云同步的其他状态放一起（`cloud-sync/state.json`），用户打开
「数据文件夹」看得见、删得掉。localStorage 藏在浏览器缓存里，既看不见也不好清。

注意：这个钩子是"每次渲染都跑一遍"的（preprocess 没有缓存），所以
`ack()` 之后下一次渲染立刻就不弹了，不需要重启。
"""
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

from cloud import state as cloud_state

# ---------- 版本 ----------

# 改一次文案就升一次，已经点过「我已知晓」的用户会再看到一次。
# 和 CONSENT_VERSION 分开：那条是"数据处理行为变了"，这条只是"说明文案变了"。
NOTICE_VERSION = "2026-10-22"

# ---------- 外链 ----------

SOURCE_URL = "https://github.com/CuteLittleSky/Evo"          # 本分支源代码
ISSUES_URL = "https://github.com/CuteLittleSky/Evo/issues"  # 本分支的问题入口
UPSTREAM_URL = "https://github.com/AugustHuangMX/Evo"       # 上游（只用于归功/撇清）

# 维护者称谓。注意这句话的上下文是「由 X 维护」，
# 所以 X 里面别再出现"维护"两个字 —— 早先写成「派喵小程序维护者」，
# 渲染出来是「由派喵小程序维护者维护」，读者要顿一下才反应过来不是笔误。
MAINTAINER = "派喵小程序作者"

# 许可证入口：弹窗里的「关于 / 第三方许可」跳到设置页的锚点。
LICENSE_ANCHOR = "/settings#h-branch"
BRANCH_ANCHOR = "h-branch"

# ---------- 版权 ----------

# 取自仓库根目录的 LICENSE，**一字不改**。改一个字就不叫"保留原版权声明"了。
UPSTREAM_COPYRIGHT = "Copyright (c) 2026 AugustHuangMX"

MIT_TEXT = """MIT License

Copyright (c) 2026 AugustHuangMX

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""

# LICENSE 文件在就用真的（避免两边哪天不同步），读不到就用上面的常量。
# paimiao_cloud/cloud/notice.py → 上三级是仓库根目录。
_LICENSE_FILE = Path(__file__).resolve().parent.parent.parent / "LICENSE"


def license_text() -> str:
    try:
        text = _LICENSE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return MIT_TEXT
    # 只认 MIT：文件被换成别的内容就退回内置常量，不把奇怪的东西摆到界面上。
    return text if text.startswith("MIT License") else MIT_TEXT


# ---------- 是否该弹 ----------

def seen() -> str:
    return cloud_state.load().get("notice_seen") or ""


# 本进程里是否已经弹过一次。
#
# 为什么光靠 state.json 不够：那个值只有用户点了按钮才会写。
# 而"该不该弹"是**每渲染一页问一次**的，于是用户还没顾上点按钮就先切到
# 「战绩」看一眼 —— 又弹一遍；再切「云同步」—— 第三次。开一次软件被拦三回。
#
# 所以"弹过没"再记一份在进程内存里：同一个 Evo 进程只弹一次，
# 关掉重开才重新判断（跨启动仍然由 state.json 管）。
#
# 置位的地方是 _dialog_for_template()，不是 should_show() —— 后者被 Jinja
# 在一次渲染里求值两遍，"问一次就消耗"会让弹窗静默消失，详见那边的注释。
_SHOWN = False


def should_show() -> bool:
    """现在该不该弹。**纯读，不改任何状态。**

    ⚠️ 这里必须是无副作用的判断，原因是个真踩过的坑：
    Jinja 在**同一次渲染**里会把 `{% if branch_notice_pending() %}` 求值两遍
    （实测：一次在决定分支时，一次在拼输出时）。如果"问一次就消耗掉"写在
    这个函数里，第一遍返回 True、第二遍返回 False，`if` 整体判假 ——
    弹窗 HTML 一个字节都进不去页面，而且是沉默失败，最难查的那种。

    所以"正在弹"的记账挪到 _dialog_for_template()：那是 if 为真之后才会被
    调用的，一次渲染最多跑一次，天然幂等。

    `EVO_NO_NOTICE=1` 可以关掉它 —— 自检和截图脚本要一张干净的设置页时用；
    这种模式不消耗机会，去掉环境变量后照样会弹。
    """
    if os.environ.get("EVO_NO_NOTICE") == "1":
        return False
    return not _SHOWN and seen() != NOTICE_VERSION


def mark_seen(version: str | None = None) -> None:
    snapshot = cloud_state.load()
    snapshot["notice_seen"] = version or NOTICE_VERSION
    cloud_state.save(snapshot)


# 曾经有个 reset_seen() 和一个 /cloud/notice/again 路由（设置页那张卡片底下的
# 「再读一遍分支说明」按钮）。2026-10-22 去掉了：说明本身是"知道一次就够了"的
# 东西，留个入口反而让人以为每次开软件都该被它拦一下。想再看一遍？
# 设置页的许可卡片一直在，内容一样。要重新触发弹窗，删掉 state.json 里的
# notice_seen 后重启即可。


# ---------- 弹窗 HTML ----------

def dialog_html(version: str = "", token: str = "") -> str:
    """弹窗 HTML。

    ⚠️ 这里是 **Python 的 f-string**，不是 Jinja 模板。返回的字符串会经过
    `| safe` 原样输出，Jinja 不会再看它第二遍 —— 所以里面**不能**写
    `{{ token }}` 指望模板引擎替换（那样只会输出字面量 `{ token }`，
    请求就会带个假口令去撞 403）。

    需要页面上的变量（Evo 的 POST 口令）就老老实实当参数传进来，
    再用 json.dumps 转成 JS 字面量。
    """
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
      本分支新增了「派喵」微信小程序云同步功能（<strong>可选</strong>）。该功能涉及战绩数据上传，详见派喵云同步页
      <a href="/cloud/">《派喵云同步 · 数据传输指引》</a>。
    </p>
    <p class="dialog-desc">
      使用中如遇问题，请优先向本分支维护者反馈：
      <a href="{e(ISSUES_URL)}" target="_blank" rel="noopener noreferrer">GitHub Issues</a>。
      请勿将本分支特有问题直接提交至上游；上游项目作者不对本分支的修改、新增功能及数据行为负责。
    </p>
    <p class="dialog-desc">
      本分支依据 <a href="{e(LICENSE_ANCHOR)}">MIT License</a> 分发，并保留原项目的版权声明与许可证文本。
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


# ---------- 卡片 HTML ----------

_CARD_TEMPLATE = Path(__file__).resolve().parent / "templates" / "branch_card.html"

# 卡片那些样式：上游的 style.css 里没有 .branch-* 这些类，所以随卡片一起注入。
#
# ⚠️ 底色/描边/圆角/内边距/标题字号**一律不在这里写**：卡片外层带的是
#    `card settings-card`，那几条上游已经定义好了（style.css:219 .card、
#    :370 .settings-card padding:24px、:371 h2 字号）。再写一遍就会打架 ——
#    上一版就是自己写了 padding:20px + border，结果比上下两张卡"瘦一圈"。
#    这里只补上游确实没有的东西：<details> 的折叠头、链接颜色、许可块。
#
# 颜色全用 CSS 变量（--muted / --input-border / --info-text / --p3-bg），
# 深浅色两套皮肤自动跟着走。
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


# ---------- 注入 ----------

def card_html(branch: dict) -> str:
    """渲染设置页那张卡片。

    ⚠️ 这里**必须用 Jinja** 渲染，不能用 str.format / f-string：
       卡片里有 `{% if %}` 之类的语句，许可证全文里也有 `{}` 之外的可疑字符。
    用 `Environment().from_string` 而不是 `render_template`：这段源码马上要
    被拼进 settings.html 一起编译，先渲染成纯 HTML 再拼，免得 Jinja 再解释一遍。
    """
    from jinja2 import Environment          # noqa: PLC0415

    source = _CARD_TEMPLATE.read_text(encoding="utf-8")
    env = Environment(autoescape=True)
    body = env.from_string(source).render(branch=branch, branch_anchor=BRANCH_ANCHOR)
    return _CARD_CSS + "\n" + body


_DIALOG_MARK = "{% block scripts %}"
# 认标记要用**带引号的属性值**：`branch-dialog` 这串也会出现在
# `aria-labelledby="h-branch-dialog"` 和 `querySelector('button[...]')` 里，
# 光看裸字符串会把"还没插"误判成"已经插过"。
_DIALOG_SENTINEL = 'id="branch-dialog"'
_CARD_SENTINEL = f'id="{BRANCH_ANCHOR}"'
# 卡片插在这个开标签之后（还要再过 div 配对，见 inject_card）。
# 为什么是 .settings-main 而不是 content 块尾：它是 grid 的左列
# （style.css:368 `1fr minmax(280px,350px)`），插在它外面卡片会横跨整行。
_CARD_INJECT_AT = '<div class="settings-main">'
_BACKREF = re.compile(r"""\{%-?\s*(?:extends|include)\s+(["'])(.+?)\1""")


def inject_dialog(source: str, version: str = "") -> str:
    """把弹窗插在 `{% block scripts %}` 之前。

    ⚠️ 这里**不判断 should_show()**，只管插不插得进去。

    为什么：Jinja 会缓存编译好的模板，preprocess 只在**第一次编译**时跑一次。
    如果在这里读"看过没有"，第一次请求时是"没看过"→ 插进去 → 缓存下来，
    用户点了「我已知晓」之后模板还带着弹窗，必须重启 Evo 才消失。

    所以模板里插的是一个**占位符**（`_PLACEHOLDER`），在模板里用 Jinja 的
    if 包起来；每次渲染时由 `notice_dialog()` 现算，配合
    `app.jinja_env.auto_reload` 无关 —— 检查的是渲染期，不是编译期。
    """
    if not source or _DIALOG_SENTINEL in source:
        return source
    at = source.find(_DIALOG_MARK)
    if at < 0:
        return source                       # 不是 base 那一派的东西，不动
    block = (
        "{% if branch_notice_pending() %}"
        # token 是 Evo 的 context_processor 给的（panel_app.py template_globals），
        # 所有模板都能拿到。用 default 兜一下：万一以后哪个页面不经过那个
        # context_processor，也只是记住"看过了"失败，不至于整页报错。
        "{{ branch_notice_dialog(token|default('')) | safe }}"
        "{% endif %}"
    )
    return source[:at] + block + source[at:]


def inject_card(source: str, branch: dict) -> str:
    """把许可卡片插进 `.settings-main` 那一列的收尾 `</div>` 之前。

    ⚠️ 位置必须是"左列里面"，不能是"content 块末尾"。踩过一次：
    一开始插在 content 收尾 endblock 前，而 settings.html 那个位置在
    `.settings-layout` 的 grid 外面、`</div>` 也早关了 —— 结果这张卡
    横跨整行（1120px），比上面「更新」「关于 Evo」那些 750px 的分区宽一大截。
    `.settings-layout` 是 `grid-template-columns: 1fr minmax(280px,350px)`
    （style.css:368），只有直接住在 `.settings-main` 里才拿得到 1fr 那一列。

    定位办法是数 div 配对：从 `<div class="settings-main">` 起点开始
    `<div` +1、`</div>` -1，第一次回到 0 的那个 `</div>` 就是它的收尾。
    settings.html 里它前面只有 `.settings-layout` 一层外壳，模板结构稳定。
    """
    if not source or _CARD_SENTINEL in source:
        return source
    anchor = _CARD_INJECT_AT
    at = source.find(anchor)
    if at < 0:
        return source                       # 不是设置页（或上游改了结构），不动
    if source.find(_CARD_INJECT_AT, at + 1) >= 0:
        return source                       # 不唯一就不猜，宁可没有卡片
    pos = at + len(anchor)
    depth = 1
    # 正则必须带 `[^>]*>`：最早写成光秃秃的 `<div\b`，于是
    # `<div class="setting">` 这类**带属性的开标签全都数不到**，深度回不到 0，
    # 卡片静默不插 —— 而自检还是绿的（断言只查"在不在"，没查插在哪一列）。
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
    """preprocess 钩子的全部逻辑：弹窗对所有页生效，卡片只给设置页。"""
    source = inject_dialog(source, version)
    if template == "settings.html":
        source = inject_card(source, branch)
    return source


def _referenced_templates(source: str) -> list[str]:
    """模板源码里 extends/include 了谁。用来判断"这次渲染的页面里会不会有它"。"""
    return [match.group(2) for match in _BACKREF.finditer(source or "")]


## ---------- 装上钩子 ----------

def register(app, module=None):
    """装 preprocess 钩子。upstream.py 已经装了同名的钩子，两层可以叠加。"""
    _install_preprocess(app.jinja_env, module)
    return ["notice_preprocess"]


def _dialog_for_template(token="", version="") -> str:
    """给 Jinja 用的包装：模板里是 `branch_notice_dialog(token)`，
    第一个位置参数是**口令**，不是版本号（版本号走默认值）。

    这个包装当年是被一个 bug 逼出来的：一开始直接把 dialog_html 挂上去，
    模板写 `branch_notice_dialog(token|default(''))`，结果 token 落到了
    `version` 参数上，弹窗里的口令是空串 → 点按钮永远记不住状态。
    两个参数长得像的时候，位置传参就是个陷阱，索性包一层把名字钉死。

    「这一进程已经弹过」的记账也在这里做（见 should_show 的注释）：
    本函数只在 `{% if branch_notice_pending() %}` 为真时才被调用，
    一次渲染最多跑一次，在这儿置位比放在判断函数里安全。
    """
    global _SHOWN
    _SHOWN = True
    return dialog_html(version=version or NOTICE_VERSION, token=str(token or ""))


def _install_preprocess(env, module):
    if getattr(env, "_paimiao_notice_preprocess", False):
        return []
    original = env.preprocess
    branch = branch_view(module)

    def preprocess(source, name, filename=None):
        # 先让前面的钩子（upstream.py 的措辞替换）自己跑完
        try:
            source = original(source, name, filename)
        except TypeError:               # 老版本 jinja 没有 filename
            source = original(source, name)
        return patch_source(source, name, branch, NOTICE_VERSION)

    env.preprocess = preprocess
    env._paimiao_notice_preprocess = True
    # 模板里用到的两个函数：一个问"现在该不该弹"，一个给弹窗 HTML。
    # 都挂在渲染期，所以点完「我已知晓」下一次刷新就不弹了，不用重启。
    env.globals["branch_notice_pending"] = should_show
    env.globals["branch_notice_dialog"] = _dialog_for_template
    return ["notice_preprocess"]


def branch_view(module=None) -> dict:
    """卡片/弹窗要的那些值。取不到版本就留空，界面显示"未知"。"""
    version = ""
    try:
        import storage                  # noqa: PLC0415  Evo 自己的模块
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
