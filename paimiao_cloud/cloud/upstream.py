from __future__ import annotations

import functools
import os

from cloud import state as cloud_state

UPDATE_BASE = os.environ.get(
    "EVO_UPDATE_BASE", "https://tools.skycraft.cn/apex/api/battlestats")
UPDATE_URL = UPDATE_BASE.rstrip("/") + "/latest.json"

UPDATE_PAGE = "https://apex.skycraft.cn/#evo"

UPDATE_HOSTS = {"tools.skycraft.cn", "apex.skycraft.cn", "cfr2-apex.skycraft.cn",
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

TELEMETRY_ENABLED = os.environ.get("EVO_PAIMIAO_TELEMETRY", "1") != "0"

_TELEMETRY_START = "{# 匿名统计（telemetry.py）"
_TELEMETRY_SECTION = '<section class="card settings-card" id="stats"'

_ASK_OPEN = "{% if ask_telemetry %}"
_ASK_HIDDEN = "{% if false and ask_telemetry %}"

OLD_ABOUT_ONLINE = "只有更新和你同意的匿名统计会联网"
NEW_ABOUT_ONLINE = "只有检查更新和派喵云同步会联网"

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
    done += _patch_telemetry()
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

def _patch_telemetry():

    if not TELEMETRY_ENABLED:
        return []
    try:
        import telemetry
    except Exception:
        return []
    done = []
    if not getattr(telemetry.consent, "_paimiao_patched", False):

        def consent():
            return False

        consent._paimiao_patched = True
        telemetry.consent = consent
        done.append("telemetry_off")

    # 用户 2026-10-25 的决定：光让 `consent()` 恒为假，`send()` 仍是「先判断、再发」的流程，
    # 哪天上游调换顺序、或者别的代码直接调 `_post()`，请求就发出去了。这里把**发送端**
    # 整个包住：不联网、不写设置，并补上 consent()=False 时原来的 return None 分支
    # （上游 send() 在 :230 就是「没同意 → 返回 None」，这里不改那个行为）。
    #
    # ⚠ 不要再把整个 `send()` 换成「直接返回一个 dict」的空函数：上游 send() 的第一句
    # 就是 `if consent() is not True: return None`，调用方（面板、云同步、测试）都按
    # 「None = 没发」来判断。替换成 dict 会让 tests/test_global_stats.py 在
    # `install not in json.dumps(...)` 上炸 TypeError（install 为 None），事实是那条断言
    # 之前从未被走到 —— 它默认「开了匿名统计就会联网」，正是我们要断的。
    if not getattr(telemetry.send, "_paimiao_noop", False):
        original_send = telemetry.send

        @functools.wraps(original_send)
        def send(*args, **kwargs):
            if consent() is not True:
                return None
            return {"ok": True, "matches": 0, "ping": False, "error": None, "at": None}

        send._paimiao_noop = True
        telemetry.send = send
        done.append("telemetry_send_noop")

    # 最后一道闸：连底层 POST 也封掉。上游 `_post()` 是唯一出网的入口
    # （send() 只经它发；别的模块要发也得走它）。
    if not getattr(telemetry._post, "_paimiao_noop", False):
        original_post = telemetry._post

        def _post(path, body, timeout=None, *args, **kwargs):
            raise telemetry.SendError("派喵版不发送匿名统计")

        _post._paimiao_noop = True
        telemetry._post = _post
        done.append("telemetry_post_blocked")
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

    if OLD_NO_UPLOAD in source:
        source = source.replace(OLD_NO_UPLOAD, NEW_NO_UPLOAD)
    if OLD_ASK in source:
        source = source.replace(OLD_ASK, _NEW_ASK)
    if OLD_SETTING in source:
        source = source.replace(OLD_SETTING, _NEW_SETTING)
    if _FEEDBACK_START in source:
        source = _drop_feedback_block(source)
    if _TELEMETRY_START in source and TELEMETRY_ENABLED:
        source = _hide_telemetry_section(source)
    if _ASK_OPEN in source and TELEMETRY_ENABLED:
        source = _hide_ask_card(source)
    if OLD_ABOUT_ONLINE in source and TELEMETRY_ENABLED:
        source = source.replace(OLD_ABOUT_ONLINE, NEW_ABOUT_ONLINE)
    if _FOLLOW_START in source and not FOLLOW_ENABLED:
        source = _hide_follow_block(source)
    return source

_SETTINGS_BLOCK_END = "</section>"

_TELEMETRY_END = _SETTINGS_BLOCK_END

def _hide_ask_card(source):

    if _ASK_HIDDEN in source:
        return source
    if source.count(_ASK_OPEN) != 1:
        return source
    return source.replace(_ASK_OPEN, _ASK_HIDDEN)

def _hide_telemetry_section(source):

    start = source.find(_TELEMETRY_START)
    if start < 0:
        return source
    if source.find(_TELEMETRY_START, start + 1) >= 0:
        return source
    section = source.find(_TELEMETRY_SECTION, start)
    if section < 0:
        return source
    end = source.find(_TELEMETRY_END, section)
    if end < 0:
        return source
    end += len(_TELEMETRY_END)
    return source[:start] + source[end:].lstrip("\r\n")

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
