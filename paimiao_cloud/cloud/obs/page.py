"""OBS 源投影 · 设置页那一栏（纯注入，上游 settings.html 一个字节都不动）。

做法和 `cloud/notice.py` 一样：在 `app.jinja_env` 上再叠一层 preprocess 钩子，
对 settings.html 的**源码字符串**插一段 `<section>`，渲染期数据由
`@bp.app_context_processor` 提供（和导航第 4 项同一套办法）。

插入位置：`{# 提示音（sounds.py` 这个注释之前。为什么插在这儿：
    - 它前面是 `</section>`（开机启动那一块结束），后面是提示音那一块，
      插在两者中间不会嵌进任何已有的 section 里；
    - 这个锚点在 settings.html 里**唯一**（实测 `h-sounds` 出现 2 次，
      但带 `{# 提示音` 的注释只有 1 处），比按 `<h2>` 文字匹配稳；
    - 位置在页面上半部分，用户点「设置」一眼就能看到。

为什么「能不能用」必须放在渲染期判断（不能放进 preprocess）：
    Jinja 会把编译结果缓存起来，preprocess 只跑一次；投影有没有、置没置顶
    是随时会变的，所以源码里只写 `{% if obs.enabled %}` 这种条件，
    真正的数据每次都从 app_context_processor 现取。
"""
from __future__ import annotations

import json

from flask import Blueprint, flash, redirect, request, url_for
from markupsafe import Markup

from cloud import state as cloud_state
from cloud.obs import projector
from cloud.obs import settings as obs_settings

bp = Blueprint("obs", __name__, url_prefix="/obs")

# settings.html 里的锚点：提示音那块前面的注释。
ANCHOR = "{# 提示音（sounds.py"
SECTION_ID = 'id="h-obs"'


def register(app):
    """挂上蓝图 + 模板钩子。幂等：重复调用不会重复注册。"""
    if not app.blueprints.get("obs"):
        app.register_blueprint(bp)
    _install_preprocess(app.jinja_env)


def _install_preprocess(env):
    if getattr(env, "_paimiao_obs_preprocess", False):
        return
    original = env.preprocess

    def preprocess(source, name, filename=None):
        try:
            source = original(source, name, filename)
        except TypeError:      # 老版 jinja 的签名没有 filename
            source = original(source, name)
        return _patch(source)

    env.preprocess = preprocess
    env._paimiao_obs_preprocess = True


def _patch(source: str) -> str:
    """把 OBS 那一栏插进去。找不到锚点 / 已经插过 / 锚点不唯一 → 原样返回。"""
    if SECTION_ID in source:              # 幂等：插过了
        return source
    if ANCHOR not in source:
        return source
    if source.count(ANCHOR) != 1:         # 不唯一：宁可不插，也不能插错位置
        return source
    at = source.index(ANCHOR)
    return source[:at] + SECTION + "\n\n    " + source[at:]


# ---------- 渲染期数据 ----------

@bp.app_context_processor
def inject():
    """所有页面都能拿到 `obs`。只有设置页的模板会用到它。"""
    return {"obs": view()}


def view():
    """设置页那一栏要显示的全部内容。永不抛异常（渲染期炸了整个设置页就白屏了）。"""
    status = {"available": False, "reason": "读不到状态", "monitors": [], "projector": None,
              "topmost": False, "candidates": []}
    try:
        status = projector.describe()
    except Exception as exc:  # noqa: BLE001
        status["reason"] = f"检测投影窗口时出错：{exc}"

    source = obs_settings.source()
    found = status.get("projector")
    # 截哪块屏（找到了才显示）。不提「第几块屏」：Windows 的显示器编号、dxcam 的
    # output 序号、从左到右的位置序号三者可以全都不一样（2026-10-23 实测差 1），
    # 说个数反而让用户去找不存在的「3 号屏」。分辨率 + 相对主屏的位置最好认。
    where = (f"{_describe_place(found)}（{found['width']} × {found['height']}）"
             if found else "")
    available = bool(status.get("available"))
    reason = status.get("reason", "")
    return {
        "enabled": source == obs_settings.SOURCE_PROJECTOR,
        "source": source,
        "source_screen": obs_settings.SOURCE_SCREEN,
        "source_projector": obs_settings.SOURCE_PROJECTOR,
        "available": available,
        "reason": reason,
        "topmost": bool(status.get("topmost")),
        "where": where,
        "monitor_count": len(status.get("monitors") or []),
        "pause_when_closed": obs_settings.pause_when_closed(),
        # ⚠️ 「当前状态」那段前端脚本必须**从这里传进模板**。一开始是写成
        #    `{% if obs.enabled %}{{ STATUS_SCRIPT }}{% endif %}`，直接引用本模块的
        #    全局名 —— Jinja 只看模板上下文，看不到模块全局，于是静默渲染成空字符串，
        #    页面上一切正常、只是永远不自动刷新（2026-10-23 实测踩过）。
        "status_script": _status_script(),
        "status_every": STATUS_EVERY,
        # 「当前状态」那行字。模板首屏直接用它，脚本轮询拿到的 status_text() 也是
        # 同一句 —— 措辞只有一处，改的时候不可能只顾一边（以前是各写一遍）。
        "_status": {
            "enabled": source == obs_settings.SOURCE_PROJECTOR,
            "ok": available,
            "text": (f"{where}，已置顶，现在就能截到画面。" if available else reason),
            "where": where,
            "reason": reason,
        },
    }


# ---------- 动作 ----------

def _describe_place(found: dict) -> str:
    """把「投影在哪」说成用户看得懂的话。

    不报序号，只说相对于主屏的位置；主屏自己就说「主屏」。
    找不到主屏参照时退回「另一块屏」，总比报一个对不上的编号强。
    """
    mon = found.get("monitor") or {}
    if mon.get("primary"):
        return "主屏"
    left, top = mon.get("left", 0), mon.get("top", 0)
    if left < 0:
        return "主屏左边那块屏"
    if left > 0:
        return "主屏右边那块屏"
    if top < 0:
        return "主屏上方那块屏"
    if top > 0:
        return "主屏下方那块屏"
    return "另一块屏"


def _settings_url() -> str:
    # 设置页的路由名是 panel_app 里的 `settings`，不要写死路径
    return url_for("settings") + "#obs"


# ---------- 「当前状态」的自动刷新 ----------
#
# 一开始「当前状态」只在页面渲染时算一次：用户开着设置页去开 OBS、去勾「始终置顶」，
# 页面上还是「没找到投影窗口」，得手动刷新才知道好了没有（2026-10-23 用户反馈）。
#
# 现在设置页里带一段脚本，每 STATUS_EVERY 秒问一次 /obs/status，原地更新那两行字。
# 几个刻意的地方：
#   · **只在「OBS 投影」来源下才轮询**：选「整屏」时这一栏根本不显示，发请求是白费；
#   · **只在设置页可见时轮询**（document.hidden / visibilitychange）：Evo 窗口最小化
#     或切到别的页签时停掉，符合项目「空闲时 CPU 接近 0%」的原则；
#   · **文字完全一样就不动 DOM**：避免屏幕阅读器每 3 秒重念一遍，也不闪。
#   · 拿不到数据时**不把已经有字的区域清空**（宁可显示旧状态，也别变成空白）。

STATUS_EVERY = 3000          # 毫秒


@bp.get("/status")
def status():
    """给设置页的脚本用：现在能不能截、截哪儿、为什么不能。

    只读，不改任何设置，所以不需要 token。返回的 `text` 是**已经拼好的一句话**，
    由服务端统一措辞 —— 前端只负责替换，免得两处各写一套说法慢慢跑偏。
    """
    return _status_view()


def _status_view() -> dict:
    """`/obs/status` 的返回体。用 `view()` 的同一份数据拼，措辞和首屏完全一致。"""
    data = view()
    if not data["enabled"]:
        return {"enabled": False, "ok": False, "text": ""}
    return {"enabled": True, "ok": data["available"], "text": status_text(data),
            "where": data["where"], "reason": data["reason"]}


def status_text(data: dict) -> str:
    """「当前状态」那行字。首屏模板和轮询返回的都是这一句，只有一处措辞。"""
    return (data.get("_status") or {}).get("text", "")


@bp.post("/source")
def set_source():
    """切换画面来源：整屏（上游默认）/ OBS 投影。"""
    value = request.form.get("source") or ""
    if value not in obs_settings.SOURCES:
        flash("没认出这个画面来源。", "error")
        return redirect(_settings_url())
    if not obs_settings.set_source(value):
        flash("没保存成功，请再试一次。", "error")
        return redirect(_settings_url())

    if value == obs_settings.SOURCE_PROJECTOR:
        # 立刻告诉用户现在能不能用，省得他以为开了就万事大吉
        try:
            status = projector.describe()
        except Exception as exc:  # noqa: BLE001
            status = {"available": False, "reason": str(exc)}
        if status.get("available"):
            found = status.get("projector") or {}
            flash(f"已改用 OBS 投影：正在截{_describe_place(found)}。"
                  "正在记录的话，几秒内生效。", "ok")
        else:
            flash(f"已改用 OBS 投影，但现在还截不到画面：{status.get('reason', '')}", "error")
    else:
        flash("已改用整屏截取（Evo 原来的方式）。", "ok")
    cloud_state.log(f"画面来源改成：{value}")
    return redirect(_settings_url())


@bp.post("/pause-when-closed")
def set_pause():
    """投影消失时：暂停截屏（默认）/ 退回整屏。"""
    on = request.form.get("on") == "1"
    if not obs_settings.set_pause_when_closed(on):
        flash("没保存成功，请再试一次。", "error")
    return redirect(_settings_url())


# ---------- 模板片段 ----------

SECTION = """
    {# OBS 源投影（cloud/obs/）：主机玩家把画面送进 OBS，用全屏投影投到一块屏上，Evo 截那块屏。
       插在「提示音」前面（cloud/obs/page.py 的 ANCHOR）。这里只写条件，数据由 app_context_processor 给。 #}
    <section class="card settings-card" aria-labelledby="h-obs" id="obs">
      <h2 id="h-obs">画面来源</h2>
      <form method="post" action="{{ url_for('obs.set_source') }}" class="setting">
        <input type="hidden" name="token" value="{{ token }}">
        <div class="setting-text">
          <div class="setting-name" id="obs-source-name">从哪里读游戏画面</div>
          <div class="setting-desc">PC 版选「整屏进程」。玩主机（Switch / PS5 / Xbox）时，把画面送进 OBS，
            用 OBS 的「全屏投影」把它投到一块屏幕上，再选「OBS 投影」——Evo 只截那块屏，识别规则完全一样。</div>
        </div>
        <div class="setting-actions inline">
          <select name="source" aria-labelledby="obs-source-name">
            <option value="{{ obs.source_screen }}" {{ 'selected' if not obs.enabled }}>整屏进程（PC 游戏）</option>
            <option value="{{ obs.source_projector }}" {{ 'selected' if obs.enabled }}>OBS 投影（主机）</option>
          </select>
          <button type="submit" class="btn">保存</button>
        </div>
      </form>

      {% if obs.enabled %}
      <div class="setting">
        <div class="setting-text">
          <div class="setting-name" id="obs-state-name">当前状态</div>
          {# 这段文字每 STATUS_EVERY 毫秒被下面的脚本原地换掉（data 属性 + textContent），
             所以模板这里只放首屏的那一份，措辞来自 view()['_status']['text']，只有一处。 #}
          <div class="setting-desc" id="obs-state-text"
               data-ok="{{ '1' if obs.available else '0' }}">{{ obs._status.text }}</div>
          <div class="setting-desc">投影窗口必须<strong>置顶</strong>（在窗口上右键 → 勾选「始终置顶」），
            否则会被别的窗口盖住。OBS 关掉、或投影被切走时，
            {% if obs.pause_when_closed %}Evo 会暂停截屏等它回来{% else %}Evo 会退回整屏截取{% endif %}。</div>
        </div>
      </div>
      <form method="post" action="{{ url_for('obs.set_pause') }}" class="setting">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="on" value="{{ '0' if obs.pause_when_closed else '1' }}">
        <div class="setting-text">
          <div class="setting-name" id="obs-pause-name">投影消失时</div>
          <div class="setting-desc">开：暂停截屏，等投影回来继续（不会误记别的窗口）。关：退回整屏截取。</div>
        </div>
        <button type="submit" class="switch" role="switch"
                aria-checked="{{ 'true' if obs.pause_when_closed else 'false' }}"
                aria-labelledby="obs-pause-name">
          <span class="switch-track" aria-hidden="true"><span class="switch-knob"></span></span>
          <span class="switch-state" aria-hidden="true">{{ '暂停截屏' if obs.pause_when_closed else '退回整屏' }}</span>
        </button>
      </form>
      {% endif %}

      <div class="setting-desc rec-note">选「OBS 投影」之后，Evo 每 5 秒看一眼投影还在不在：
        投影挪到别的屏上会跟着走，投影没了按上面的设置暂停或退回整屏。多块屏时截投影所在的那一块。</div>
      {% if obs.enabled %}{{ obs.status_script }}{% endif %}
    </section>"""


# 「当前状态」自动刷新用的脚本。只在「OBS 投影」来源下才插进页面（选整屏时这一栏
# 根本不显示，没必要每 3 秒发一个请求）。
#
# 为什么不整页刷新：用户可能正在改别的设置（下拉框刚拨到一半），页面一跳就白改了；
# 而且整页刷新会重新算一遍所有设置项，比一次 200 字节的 JSON 重得多。
#
# ⚠️ 为什么这里的 {{ __URL__ }} / {{ __EVERY__ }} 是**我们自己在 Python 里替换**的，
#    而不是交给 Jinja：这段字符串是通过 `{{ obs.status_script }}` 输出的一个**变量**，
#    Jinja 只编译模板源码，不会把变量的内容再当模板解释一遍（2026-10-23 实测踩过：
#    页面上原样显示 `{{ url_for(...) }}`，而且整个 <script> 被 autoescape 转成了
#    `&lt;script&gt;`，脚本根本不执行）。所以值要在这里填好，再用 Markup 标成安全 HTML。
STATUS_SCRIPT = """
      <script>
      (function () {
        var box = document.getElementById("obs-state-text");
        var url = {{ __URL__ }};
        var every = {{ __EVERY__ }};

        // 只在设置页真的看得见时轮询：Evo 窗口最小化、或用户切到别的页签就停下，
        // 切回来立刻补一次 —— 不用等下一个 3 秒。
        // （自动化测试里标签页始终是 hidden，会看到一次请求都不发 —— 那是这条判断在
        //  生效，不是坏了；要验功能得先把它摘掉再跑。）
        var timer = null;
        function stop() {
          if (timer) { clearInterval(timer); timer = null; }
        }

        function render(data) {
          if (!data || !data.enabled) return;         // 用户刚切回整屏：别乱改字
          var text = data.text || "";
          if (!text) return;
          // 一字不差就不碰 DOM：否则屏幕阅读器每 3 秒重念一遍，视觉上也会闪
          if (box.textContent !== text) box.textContent = text;
          // 用 data 属性带着"能不能截"，样式由上游的 .ok / .err 决定；
          // 上游没这两个类时也不影响阅读（字本身已经说清楚了）。
          var ok = data.ok ? "1" : "0";
          if (box.dataset.ok !== ok) {
            box.dataset.ok = ok;
            box.classList.toggle("ok", !!data.ok);
            box.classList.toggle("err", !data.ok);
          }
        }

        function tick() {
          if (document.hidden) return;
          // 带上 Accept: application/json，服务端出错时也不会往这里塞一整页 HTML
          fetch(url, { headers: { "Accept": "application/json" }, cache: "no-store" })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) { if (data) render(data); })
            .catch(function () {});   // 网络抖动/面板正重启：保持上一次的字，别清空
        }

        function start() {
          if (timer) return;
          tick();                     // 立刻补一次，别让用户等满 3 秒
          timer = setInterval(tick, every);
        }

        document.addEventListener("visibilitychange", function () {
          if (document.hidden) stop(); else start();
        });
        // 页面卸载时收尾（WebView2 里翻到别的设置页会走这个）
        window.addEventListener("pagehide", stop);

        if (!document.hidden) start();
      })();
      </script>"""


def _status_script() -> Markup:
    """把 STATUS_SCRIPT 里的两个占位换成真值，并标成安全 HTML（否则 <script> 会被转义）。

    取值都用 tojson，和模板里 `|tojson` 的效果一样（字符串带引号、能直接塞进 JS）。
    url_for 需要请求上下文，取不到时退回写死的相对地址 —— 脚本坏了不该让设置页白屏。
    """
    url = "/obs/status"
    try:
        url = url_for("obs.status")
    except Exception:  # noqa: BLE001 没有请求上下文时退回相对路径
        pass
    return Markup(
        STATUS_SCRIPT
        .replace("{{ __URL__ }}", json.dumps(str(url)))
        .replace("{{ __EVERY__ }}", json.dumps(int(STATUS_EVERY)))
    )
