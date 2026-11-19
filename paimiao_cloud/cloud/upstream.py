from __future__ import annotations

import functools
import os
from urllib.parse import urlparse

from cloud import state as cloud_state

def _default_update_base() -> str:
    try:
        from cloud import net_hosts  # noqa: PLC0415

        return net_hosts.api_root().rstrip("/") + "/battlestats"
    except Exception:
        return "https://tools.skycraft.cn/apex/api/battlestats"


UPDATE_BASE = os.environ.get("EVO_UPDATE_BASE") or _default_update_base()
UPDATE_URL = UPDATE_BASE.rstrip("/") + "/latest.json"

UPDATE_PAGE = "https://apex.skycraft.cn/#evo"

UPDATE_HOSTS = {"tools.skycraft.cn", "tools1.skycraft.cn", "tools3.skycraft.cn",
                "apex.skycraft.cn", "cfr2-apex.skycraft.cn",
                "paimiaoesa.skycraft.cn",
                "github.com", "www.github.com", "objects.githubusercontent.com"}

SOURCE_URL = "https://github.com/CuteLittleSky/Evo"

OLD_NO_UPLOAD = "不上传任何数据"
NEW_NO_UPLOAD = "若不使用派喵云同步功能，将不会上传任何数据"

OLD_ASK = "只下载官网上一个公开的版本号文件"
_NEW_ASK = "只下载公开的版本号文件"

OLD_SETTING = "只下载官网上公开的版本号文件"
_NEW_SETTING = "只下载公开的版本号文件"

_FEEDBACK_START = "{# 意见反馈"
_FEEDBACK_OPEN = '<div class="feedback">'

# 分享卡片（panel/templates/share/_base.svg）底部原来写的是上游官网 evotracker.cn，
# 派喵用户分享出去的图上会显示官方站名 —— 换成派喵自己的下载页（2026-11-05 用户要的）。
CARD_BRAND_OLD = "<span>evotracker.cn</span>"
CARD_BRAND_NEW = "<span>apex.skycraft.cn</span>"

# 派喵匿名统计卡：注入到上游「设置 → 数据与隐私」卡里（运行时拼接，不改上游磁盘）
_STATS_SETTINGS_ANCHOR = '<h2 id="h-data">数据与隐私</h2>'
_STATS_SETTINGS_BLOCK = """
      <div class="setting" id="paimiao-stats">
        <div class="setting-text">
          <div class="setting-name">匿名统计</div>
          <div class="setting-desc">未绑定派喵云同步时上报去标识的对局统计（模式/地图/传奇/名次/伤害/RP/段位/队友数据/武器时长等，含随机匿名编号与 Evo 版本号；不含游戏名、玩家 ID、队友名字/ID、截图、日志）。绑定派喵云同步后自动停发。</div>
          {% if not paimiao_stats_on %}<div class="setting-desc stats-status">已关闭：不发送任何数据</div>{% endif %}
        </div>
        <form method="post" action="/cloud/stats/toggle"{% if paimiao_stats_on %} onsubmit="return confirm('确定关闭匿名统计吗？关闭后不会再上传任何统计数据，可随时重新开启。')"{% endif %}>
          <input type="hidden" name="token" value="{{ token }}">
          <input type="hidden" name="on" value="{{ '0' if paimiao_stats_on else '1' }}">
          <input type="hidden" name="next" value="/settings">
          <button type="submit" class="btn">{{ '关闭匿名统计' if paimiao_stats_on else '开启匿名统计' }}</button>
        </form>
      </div>"""

FOLLOW_ENABLED = os.environ.get("EVO_PAIMIAO_FOLLOW", "0") == "1"
# 派喵版隐藏了上游的「关注我」卡（开发者的 B 站/小黑盒），那块现在放我们自己的
# 地图轮换卡（cloud/map_rotation_ui.py）。
#
# 插卡是在**渲染之后**做的（map_rotation_ui 包了 app.jinja_env.get_template，
# 见那边的说明）：preprocess 在渲染前处理模板源码，而上游的 `_hide_follow_block`
# 也在同一层，谁在外面谁先跑 —— 插在 preprocess 里会被这次删除连带吃掉
# （2026-10-26 踩过）。渲染之后再插就和这层删除无关了，所以顺序随便。
#
# 插的位置是总览页最后那张卡（`settings-card` 收尾的 `</section>`）后面；
# 「关注我」被删掉之后那里正好是空出来的位置。跑一遍
# paimiao_cloud/cloud/tests/map_rotation_preview.py 就知道还插不插得上。

_FOLLOW_START = "{# 关注我"
_FOLLOW_OPEN = 'aria-labelledby="h-follow"'
_FOLLOW_CLOSE = "</section>"

def apply(module, env=None):

    done = []
    done += _patch_update_check()

    if env is not None:
        done += _install_template_hook(env)
    if module is not None:
        done += _patch_panel_module(module)
    _log("派喵版差异：" + ("、".join(done) if done else "没有需要改的"))
    return done

def _patch_update_check():

    try:
        from update_check import TRUSTED_HOSTS, DOWNLOAD_HOSTS
        import update_check
    except Exception:
        return []

    done = []
    if getattr(update_check, "UPDATE_URL", "") != UPDATE_URL:
        update_check.UPDATE_URL = UPDATE_URL
        done.append("update_url")
    if getattr(update_check, "DEFAULT_PAGE", "") != UPDATE_PAGE:
        update_check.DEFAULT_PAGE = UPDATE_PAGE
        done.append("update_page")

    before = len(TRUSTED_HOSTS)
    TRUSTED_HOSTS |= UPDATE_HOSTS
    if len(TRUSTED_HOSTS) != before:
        done.append("trusted_hosts")
    before = len(DOWNLOAD_HOSTS)
    DOWNLOAD_HOSTS |= UPDATE_HOSTS
    if len(DOWNLOAD_HOSTS) != before:
        done.append("download_hosts")

    # 官方源的地址从信任列表里去掉（2026-11-04 用户报「一更新就装成官方版」）：
    # 只要地址还是 dl.evotracker.cn，用户点一下更新就会下载官方安装包、覆盖掉派喵版。
    # 去掉以后，就算设置里还留着一条官方源的「新版本」记录，也打不开、下不了。
    for host in ("dl.evotracker.cn", "evotracker.cn", "www.evotracker.cn"):
        TRUSTED_HOSTS.discard(host)
        DOWNLOAD_HOSTS.discard(host)

    # 设置里缓存的那条「新版本」也要校验：老版本可能已经把官方源查到的 0.5.3 写进去了。
    # 不是派喵的下载地址就清掉，免得面板一直显示一个点了会装成官方版的按钮。
    if not getattr(update_check.known_newer, "_paimiao_hosts", False):
        original_known_newer = update_check.known_newer

        @functools.wraps(original_known_newer)
        def known_newer():
            latest = original_known_newer()
            if latest is None or _is_fork_update(latest):
                return latest
            try:
                import app_settings          # noqa: PLC0415  上游模块
                app_settings.set_value("update_latest", None)
                # 托盘可能已经为这个官方版本弹过通知；清掉，免得派喵自己发同号版本时不再提醒
                if app_settings.get("update_notified") == latest.get("version"):
                    app_settings.set_value("update_notified", None)
            except Exception:
                pass
            _log("设置里那条「新版本」不是派喵的下载地址，已清掉（免得更新成官方版）")
            return None

        known_newer._paimiao_hosts = True      # type: ignore[attr-defined]
        update_check.known_newer = known_newer
        done.append("update_hosts_guard")

    # 新版本优先从 esa 镜像下载。evo-latest.json 里的 download 现在同时带 url（cfr2，老版本读它）
    # 和 esa（paimiaoesa 派喵 ESA 加速域名，和 cfr2 是同一个文件）。新版本走这段注入：优先用 esa，
    # 下不动 / 格式不对再退回 url。老版本没有这段注入，仍然读 url=cfr2，照常能一键更新。
    if not getattr(update_check.safe_download, "_paimiao_esa", False):
        original_safe_download = update_check.safe_download

        @functools.wraps(original_safe_download)
        def safe_download(info):
            if isinstance(info, dict) and info.get("esa"):
                merged = dict(info)
                merged["url"] = info["esa"]
                picked = original_safe_download(merged)
                if picked:
                    return picked
            return original_safe_download(info)

        safe_download._paimiao_esa = True
        update_check.safe_download = safe_download
        done.append("prefer_esa")
    return done


def _is_fork_update(latest):
    """这条「新版本」记录的下载地址是不是派喵的（老版本可能缓存了官方源的结果）。"""
    info = latest.get("download") if isinstance(latest, dict) else None
    if not isinstance(info, dict):
        return False
    for key in ("esa", "url"):
        url = info.get(key)
        if not url:
            continue
        try:
            host = urlparse(str(url)).hostname or ""
        except ValueError:
            continue
        if host in UPDATE_HOSTS:
            return True
    return False


def patch_update_source():
    """把「检查更新」的地址换成派喵的，托盘进程也要用。

    为什么单独拿出来：托盘进程（`evo_app.start_update_checker`）**不会 import panel_app**，
    而 `apply()` 只在 `attach(panel_app)` 里调用 —— 不在这里补一次的话，托盘会去官方源
    查更新：官方发新版那天，用户一点更新就装成官方 Evo，派喵版被覆盖（2026-11-04 用户报）。
    由 `bootstrap.install()` 在进程启动时调用（源码运行的 sitecustomize / 打包版的 runtime_hook
    都会走这里），比托盘 15 秒后的第一次检查早。
    """
    done = _patch_update_check()
    if done:
        _log("更新地址：" + "、".join(done))
    return done

def _patch_panel_module(module):
    done = []
    if getattr(module, "SOURCE_URL", "") != SOURCE_URL:
        module.SOURCE_URL = SOURCE_URL
        done.append("source_url")
    if getattr(module, "FEEDBACK_URL", "") != "":
        module.FEEDBACK_URL = ""
        done.append("feedback_url")
    if getattr(module, "FEEDBACK_GROUP", "") != "":
        module.FEEDBACK_GROUP = ""
        done.append("feedback_group")

    if FOLLOW_ENABLED:
        pass
    elif getattr(module, "FOLLOW_LINKS", None):
        module.FOLLOW_LINKS = ()
        done.append("follow_off")
    return done

def _install_template_hook(env):

    if getattr(env, "_paimiao_cloud_preprocess", False):
        return []
    original = env.preprocess

    def preprocess(source, name, filename=None):

        try:
            source = original(source, name, filename)
        except TypeError:
            source = original(source, name)
        return _patch_template(source)

    env.preprocess = preprocess
    env._paimiao_cloud_preprocess = True
    return ["template_hook"]

def _patch_template(source):

    if CARD_BRAND_OLD in source:
        source = source.replace(CARD_BRAND_OLD, CARD_BRAND_NEW)
    if OLD_NO_UPLOAD in source:
        source = source.replace(OLD_NO_UPLOAD, NEW_NO_UPLOAD)
    if OLD_ASK in source:
        source = source.replace(OLD_ASK, _NEW_ASK)
    if OLD_SETTING in source:
        source = source.replace(OLD_SETTING, _NEW_SETTING)
    if _FEEDBACK_START in source:
        source = _drop_feedback_block(source)
    if _FOLLOW_START in source and not FOLLOW_ENABLED:
        source = _hide_follow_block(source)
    if _STATS_SETTINGS_ANCHOR in source and "paimiao-stats" not in source:
        source = source.replace(_STATS_SETTINGS_ANCHOR, _STATS_SETTINGS_ANCHOR + _STATS_SETTINGS_BLOCK)
    return source

def _hide_follow_block(source):

    start = source.find(_FOLLOW_START)
    if start < 0:
        return source
    if source.find(_FOLLOW_START, start + 1) >= 0:
        return source
    open_at = source.find(_FOLLOW_OPEN, start)
    if open_at < 0:
        return source
    close = source.find(_FOLLOW_CLOSE, open_at)
    if close < 0:
        return source
    end = close + len(_FOLLOW_CLOSE)
    return source[:start].rstrip() + "\n" + source[end:].lstrip("\r\n")

def _drop_feedback_block(source):
    start = source.find(_FEEDBACK_START)
    if start < 0:
        return source
    open_at = source.find(_FEEDBACK_OPEN, start)
    if open_at < 0:
        return source
    end = _match_div(source, open_at)
    if end < 0:
        return source
    return source[:start].rstrip() + "\n" + source[end:].lstrip("\r\n")

def _match_div(source, open_at):

    depth = 0
    index = open_at
    while True:
        nxt_open = source.find("<div", index)
        nxt_close = source.find("</div>", index)
        if nxt_close < 0:
            return -1
        if nxt_open >= 0 and nxt_open < nxt_close:
            depth += 1
            index = nxt_open + 4
        else:
            depth -= 1
            index = nxt_close + 6
            if depth == 0:
                return index

def _log(message):
    try:
        cloud_state.log(message)
    except Exception:
        pass
