"""Evo 派喵云同步 · 覆盖上游的几处「派喵版」差异（全部在内存里改，磁盘文件不动）。
"""
from __future__ import annotations

import os

from cloud import state as cloud_state

# ---------- ① 更新检查 ----------

# 派喵版把「检查更新」也指向自己的服务器：版本信息由我们自己的服务器下发。
# 这样 Evo 只和一家打交道，用户不用再多信任一个陌生域名。
UPDATE_BASE = os.environ.get(
    "EVO_UPDATE_BASE", "https://tools.skycraft.cn/apex/api/battlestats")
UPDATE_URL = UPDATE_BASE.rstrip("/") + "/latest.json"

# 下载页：更新提示条上的「查看更新内容并下载」跳这里（GitHub Releases 由用户自己看）
UPDATE_PAGE = "https://apex.skycraft.cn/#evo"
# cfr2-apex.skycraft.cn = Cloudflare R2 自定义域，安装包正式下载源（出站免费、国内比 GitHub 稳）。
# 必须同时进 UPDATE_HOSTS 和 DOWNLOAD_HOSTS，否则 update_check 会判定「只能在官网下载」而静默降级。
UPDATE_HOSTS = {"tools.skycraft.cn", "apex.skycraft.cn", "cfr2-apex.skycraft.cn",
                "github.com", "www.github.com", "objects.githubusercontent.com"}

# ---------- ③④ 关于卡片 ----------

SOURCE_URL = "https://github.com/CuteLittleSky/Evo"

# ---------- ⑤ 隐私措辞 ----------

OLD_NO_UPLOAD = "不上传任何数据"
NEW_NO_UPLOAD = "若不使用派喵云同步功能，将不会上传任何数据"

OLD_ASK = "只下载官网上一个公开的版本号文件"
_NEW_ASK = "只下载公开的版本号文件"

OLD_SETTING = "只下载官网上公开的版本号文件"
_NEW_SETTING = "只下载公开的版本号文件"

# 意见反馈那一块：从 `{# 意见反馈` 注释开始，到它后面那个 `</div>` 为止。
# 用括号配对找结尾，不用正则硬啃嵌套（模板里那块只有一层 div）。
_FEEDBACK_START = "{# 意见反馈"
_FEEDBACK_OPEN = '<div class="feedback">'


# ---------- 入口 ----------

def apply(module, env=None):
    """把派喵版差异打上去。module 是已加载的 panel_app。

    module 传 None 时只打 update_check 那部分（命令行自检用，不需要起面板）。
    env 给了就同时装模板钩子（Flask 的 app.jinja_env）。
    返回改动了哪些项的列表，便于日志和自检断言。
    """
    done = []
    done += _patch_update_check()
    if module is not None:
        done += _patch_panel_module(module)
    if env is not None:
        done += _install_template_hook(env)
    _log("派喵版差异：" + ("、".join(done) if done else "没有需要改的"))
    return done


# ---------- ① 更新检查 ----------

def _patch_update_check():
    """改 update_check 的常量。

    注意 DOWNLOAD_HOSTS：一键更新只从白名单主机下安装包，我们的 latest.json
    指向 GitHub Releases，所以要把 github 相关主机加进去，否则下载按钮点了会被
    safe_download 拒掉（它返回 None → 只能"去官网手动下载"）。
    """
    try:
        from update_check import TRUSTED_HOSTS, DOWNLOAD_HOSTS  # noqa: PLC0415
        import update_check                                   # noqa: PLC0415
    except Exception:
        return []

    done = []
    if getattr(update_check, "UPDATE_URL", "") != UPDATE_URL:
        update_check.UPDATE_URL = UPDATE_URL
        done.append("update_url")
    if getattr(update_check, "DEFAULT_PAGE", "") != UPDATE_PAGE:
        update_check.DEFAULT_PAGE = UPDATE_PAGE
        done.append("update_page")
    # 这三个是模块级 set，要原地改（别赋值新对象，别的模块可能已经持有引用）
    before = len(TRUSTED_HOSTS)
    TRUSTED_HOSTS |= UPDATE_HOSTS
    if len(TRUSTED_HOSTS) != before:
        done.append("trusted_hosts")
    before = len(DOWNLOAD_HOSTS)
    DOWNLOAD_HOSTS |= UPDATE_HOSTS
    if len(DOWNLOAD_HOSTS) != before:
        done.append("download_hosts")
    return done


# ---------- ③④ 面板常量 ----------

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
    return done


# ---------- ⑤ 模板文本 ----------

def _install_template_hook(env):
    """在 jinja 环境上装 preprocess 钩子。重复调用安全（同一个 env 只包一层）。"""
    if getattr(env, "_paimiao_cloud_preprocess", False):
        return []
    original = env.preprocess

    def preprocess(source, name, filename=None):
        # 先让 jinja 自己做它原本的预处理（默认是恒等），再换我们的文本
        try:
            source = original(source, name, filename)
        except TypeError:      # 老版本签名没有 filename
            source = original(source, name)
        return _patch_template(source)

    env.preprocess = preprocess
    env._paimiao_cloud_preprocess = True
    return ["template_hook"]


def _patch_template(source):
    """对一个模板的源码做派喵版替换。找不到就不动，绝不抛异常。"""
    if OLD_NO_UPLOAD in source:
        source = source.replace(OLD_NO_UPLOAD, NEW_NO_UPLOAD)
    if OLD_ASK in source:
        source = source.replace(OLD_ASK, _NEW_ASK)
    if OLD_SETTING in source:
        source = source.replace(OLD_SETTING, _NEW_SETTING)
    if _FEEDBACK_START in source:
        source = _drop_feedback_block(source)
    return source


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
    """从 <div ...> 的起点，返回配对 </div> 之后的下标；找不到返回 -1。"""
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
