# 带派喵云同步的打包配置（替代 tools/evo.spec，上游文件一个字不动）。
#
# 用法：  python tools/build.py --spec paimiao_cloud/evo.spec
#     或  pyinstaller --clean --noconfirm paimiao_cloud/evo.spec
#
# 为什么这么写：
#     PyInstaller 的 spec 就是一个被 exec 的 Python 脚本。与其复制一份
#     tools/evo.spec 然后两边各自漂移（升级上游时必然漏改），不如把上游那份
#     exec 进来，只在 Analysis 被调用的那一刻把我们的东西塞进去。
#     上游改了 datas / excludes / upx / 图标，这里全自动跟着变。
#
#     注入两样东西：
#       runtime_hooks=[paimiao_cloud/runtime_hook.py]  打包版没有 site 模块，
#                                                  runtime_hook 是唯一的注入时机
#       datas+=[(paimiao_cloud 目录, "paimiao_cloud")]      把 cloud 包本身也打进 _internal
#
# 不打包这个 spec 时（直接跑源码），用的仍是 inject/sitecustomize.py，
# 两条路互不依赖，各自都能单独工作。

import os

# PyInstaller 只给 spec 注入 SPECPATH / DISTPATH / WORKPATH / SPEC 这几个全局名（没有 __SPEC__）。
# SPECPATH = 本文件所在目录 = paimiao_cloud/，所以项目根是它的上一级。
HERE = os.path.abspath(SPECPATH)                            # paimiao_cloud/
ROOT = os.path.dirname(HERE)                                # 项目根
UPSTREAM = os.path.join(ROOT, "tools", "evo.spec")

_runtime_hook = os.path.join(HERE, "runtime_hook.py")
if not os.path.isfile(_runtime_hook):
    raise SystemExit(f"找不到 {_runtime_hook}")

# ---- 拦截 Analysis：把我们的 runtime_hook 和 cloud 包塞进去 ----
# cloud/ 是作为 datas 打包的（普通文件），所以 PyInstaller 的静态依赖分析
# **看不到它里面的 import**。凡是 cloud/ 用到、而 Evo 主程序又不一定会用到的
# 模块，都必须在这里显式声明，否则打包版会在第一次用到时报 ModuleNotFoundError。
# 实测漏掉 sqlite3 时，真 exe 里 attach() 抛异常、页面没有第 4 项。
_HIDDEN = [
    "sqlite3",          # sync.py 读数据库
    "hashlib",          # 内容哈希
    "urllib.request",   # pairing.py 跟服务器说话
    "urllib.error",     # 上面那个的异常
    "jinja2",           # templates.py 拼接 loader
    "flask",            # panel.py 的 Blueprint
]

_RealAnalysis = Analysis


def Analysis(*args, **kwargs):                              # noqa: N802  (PyInstaller 的约定名)
    hooks = list(kwargs.get("runtime_hooks") or [])
    hooks.append(_runtime_hook)
    kwargs["runtime_hooks"] = hooks
    datas = list(kwargs.get("datas") or [])
    # 目录放成 paimiao_cloud/，于是 _internal/paimiao_cloud/cloud/bootstrap.py 就是 runtime_hook 的第一个候选
    datas.append((HERE, "paimiao_cloud"))
    kwargs["datas"] = datas
    hidden = list(kwargs.get("hiddenimports") or [])
    hidden.extend(h for h in _HIDDEN if h not in hidden)
    kwargs["hiddenimports"] = hidden
    return _RealAnalysis(*args, **kwargs)


# ---- 执行上游 spec ----
# 上游 spec 用 SPECPATH 找项目根（第 7 行 os.path.dirname(SPECPATH)），
# 所以要把 SPECPATH 设成 tools/，否则它会按 paimiao_cloud/ 去算，路径全错。
_g = {
    "__name__": "__main__",
    "__file__": UPSTREAM,
    "SPECPATH": os.path.dirname(UPSTREAM),
    "Analysis": Analysis,       # 用我们的包装版覆盖掉刚定义的真的
    "PYZ": PYZ,
    "EXE": EXE,
    "COLLECT": COLLECT,
    "SPEC": None,
}
with open(UPSTREAM, "r", encoding="utf-8") as fh:
    _src = fh.read()
exec(compile(_src, UPSTREAM, "exec"), _g)
