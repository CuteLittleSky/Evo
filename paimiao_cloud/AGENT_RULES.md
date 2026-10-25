# 派喵云同步分支 · 修改规范（给 AI Agent 看）

> 适用范围：本文件所在目录 `paimiao_cloud/` 及其下所有文件。
> 本文件是**硬约束**，不是建议。和任何其他说明冲突时，以本文件为准；
> 本文件没写到、又拿不准的地方，停下来问人，不要自己假设。

---

## 0. 一句话铁律

**只准修改 `paimiao_cloud/` 里的文件。`paimiao_cloud/` 以外的任何一个字节，都不许改。**
所有对上游 Evo 的改动，一律通过**运行时注入**实现（内存里改，磁盘文件不动）。

---

## 1. 绝对禁止（违反即为错误答案）

### 1.1 禁止修改、创建、删除、重命名 `paimiao_cloud/` 以外的文件

包括但不限于：`code/`、`tools/`、`tests/`、`config/`、`panel/`、`webview/`、`docs/`、
`release/`、`assets/`、`build/`、`data/`、`logs/`、`.github/`、仓库根目录的任何文件
（`CLAUDE.md`、`README.md`、`PRIVACY.md`、`LICENSE`、`settings.json`、`.gitignore`……）。

- **读**上游文件是允许的，而且常常必须读（要先读清楚再决定怎么注入）。
- **写**上游文件一律禁止。不要以"只改一行"、"先临时改一下等下改回来"为由动手。
- 不要用脚本、格式化工具、`sed`、Git 操作等任何方式间接改动上游文件。
- 上游文件里有 bug 或写得不顺手，**不是**修改它的理由 —— 见第 3 节。

### 1.2 禁止把上游代码复制进 `paimiao_cloud/` 整份拷贝

比如"把 `code/panel_app.py` 复制过来改一改，然后靠 `sys.path` 优先级盖掉上游版本"。
这条被明确否决，原因：

- 上游升级后，拷贝的那份还是旧的，用户装的是新旧混合体，坏得莫名其妙；
- 上游改动永远传不进来，等于分叉了一整个 Evo。

正确做法见第 3 节。

### 1.3 禁止在 `paimiao_cloud/` 里出现上游文件的"影子副本"

任何"把上游某个文件整份搬过来当自己的"都算犯规，包括 `.json`、`.html`、`.css`、`.spec`。
（`evo.spec` 是例外：它是**包装器**，`exec` 上游 `tools/evo.spec` 后再往里塞东西，
不是上游 spec 的副本。新写这类包装器时必须保持"上游改了什么这里自动跟着变"。）

### 1.4 注入失败必须静默降级，禁止抛出去

上游必须能在没有本文件夹时照常运行。所有注入代码都要包 `try/except`，
失败只写日志（`cloud/state.py` 的 `log()`），绝不能让 Evo 起不来、报错、或白屏。
现有各注入点（`inject/sitecustomize.py`、`runtime_hook.py`、`cloud/bootstrap.py` 的
`_AfterImportLoader.exec_module`）都已这样处理，改动时不要破坏这个性质。

### 1.5 禁止扩宽 Evo 的既有边界

以下几条是 Evo 的项目原则，注入同样受约束（详见仓库根 `CLAUDE.md`）：

- 不注入游戏、不读游戏内存、不 hook、不模拟键鼠；只做被动截屏。
- 本地优先：除"派喵云同步"（用户显式同意并配对后才上传）和"更新检查"外，不允许新增联网。
- 不在用户不知情时上传/下载任何东西；上传内容严格限定在 `README.md`「数据协议」里列的那份清单。
- 不新增竞技优势类功能（看别人段位、实时战术提示等）。
- 面板 / 官网的界面改动：先看当前 DSH 会话里有没有界面设计类的 skill（技能选择器里列的），有就加载照着做；没有就沿用现有组件的样式和写法，别另起一套视觉风格（见仓库根 `CLAUDE.md` 原则 9）。
- 想改上游文件（`code/`、`tools/`、`tests/`、`config/`、`panel/`、`webview/` 或根目录的文档）同样禁止；能改的只有本文件夹。
---

## 2. 允许修改的范围

`paimiao_cloud/` 下的文件，都可以改、可以新增。当前布局：

| 文件 | 职责 |
|---|---|
| `inject/sitecustomize.py` | 源码运行时的注入入口（靠 `PYTHONPATH` 指向本目录，解释器自动导入） |
| `runtime_hook.py` | 打包版（PyInstaller）的注入入口（打包后 `site` 模块不初始化，`sitecustomize` 走不通） |
| `run_evo.py` | 源码运行启动器：设好 `PYTHONPATH` + 显式导入注入层 + `runpy` 跑 `code/evo_app.py` |
| `build_evo.py` | 打包启动器：导入上游 `tools/build.py`，只把其中的 `tools/evo.spec` 换成我们的 spec |
| `evo.spec` | 包装上游 spec：拦 `Analysis` 塞 `runtime_hooks`、`datas`、`hiddenimports` |
| `cloud/bootstrap.py` | 注入层核心：装 `meta_path` 钩子，等 `panel_app` 导入完成后再 `attach()` |
| `cloud/upstream.py` | 派喵版差异：改 `update_check` 常量、改 `panel_app` 常量、模板文本替换 |
| `cloud/notice.py` | 分支说明弹窗 + 设置页许可卡片（纯内存注入模板） |
| `cloud/panel.py` | 独立页面 `/cloud/`、导航第 4 项、上下文注入 |
| `cloud/sync.py` / `pull.py` / `pairing.py` | 同步线程、拉取、配对 |
| `cloud/state.py` | 状态文件、日志、数据目录定位 |
| `cloud/templates.py` | 模板加载：上游 loader 优先，我们的目录兜底 |
| `cloud/templates/*.html` | 我们自己的页面（`cloud_page.html`、`branch_card.html`） |
| `README.md` | 分支对外说明（功能、数据协议、许可） |

---

## 3. 上游改动的正确实现方式（按顺序挑，优先用前面的）

### 3.1 改模块级常量 / 属性

`panel_app`、`update_check`、`storage` 等模块加载完成后，直接改它们的模块属性。
现有样例：`cloud/upstream.py` 的 `_patch_update_check()`、`_patch_panel_module()`。

```python
import update_check
update_check.UPDATE_URL = UPDATE_URL
```

- 只改**属性**，不要去改源码。
- 模块级 `set` / `dict` / `list` 必须**原地改**（`|=`、`.update()`、`.append()`），
  不要赋值一个新对象 —— 别的模块可能已经持有旧引用。
- 改之前先比对当前值，没变就不动手（便于日志、也避免重复执行出问题）。

### 3.2 包函数 / 方法

要改某个上游函数的行为，就导入那个模块、把它命名空间里的名字换成自己的包装。

```python
real = module.some_func
def patched(*args, **kwargs):
    result = real(*args, **kwargs)
    return result.replace(...)
module.some_func = patched
```

必须做到：先调原函数再加工（或明确说明为什么不能），保留原有签名语义，
**幂等**（重复执行不会套两层或重复生效，用模块上的标记位判断）。
现有样例：`cloud/upstream.py` 的 `_install_template_hook()` 包 `env.preprocess`，
`cloud/bootstrap.py` 的 `_install_flask_hook()` 包 `Flask.run`。

### 3.3 加 Flask 内容（路由、页面、上下文）

- 新页面：自己开 `Blueprint`，用 `app.register_blueprint()` 挂上
  （见 `cloud/panel.py`，前缀 `/cloud`）。会自动继承 `panel_app` 的 `before_request`
  安全策略（只认 `127.0.0.1`/`localhost`；POST 必须带 token），**不要绕过它**。
- 改模板渲染用的变量：用 `@bp.app_context_processor`（覆盖同名键，实测有效），
  不要改上游视图函数 —— 导航第 4 项就是这么加的（`cloud/panel.py: nav_items()`）。
- 新模板：加到 `cloud/templates/`，靠 `cloud/templates.py` 的 `splice_loader`
  （`ChoiceLoader([上游 loader, 我们的 loader])`）被找到。**同名模板不要放进来**，
  那会抢上游的模板；要改上游模板内容，走 3.4。
- 需要和上游共享 DB：读 `app.config.get("DB")`（面板可能用 `--db` 指向演示库），
  不要硬编码 `paths.DB_PATH`。

### 3.4 改上游模板的内容

只有一条路：在 `app.jinja_env` 上套 `preprocess` 钩子，对模板**源码字符串**做插入/替换。
现有两层，按顺序叠加（先上游措辞，后分支内容）：

1. `cloud/upstream.py: _install_template_hook()` —— 文本替换、整块删除；
2. `cloud/notice.py: _install_preprocess()` —— 插入弹窗、插入许可卡片。

写这类钩子时：

- 必须调 `original(source, name, filename)`（老版 jinja 没有 `filename`，用 `try/except TypeError` 兜），再加工返回值。
- 定位一律用**带引号的属性值**或明确的配对结构，别用裸字符串：
  `notice.py` 里 `branch-dialog` 这串也出现在 `aria-labelledby="h-branch-dialog"` 里，
  裸字符串匹配会把"没插"误判成"插过了"。
- 数 `div` 配对时正则必须是 `<div\b[^>]*>|</div>`，光写 `<div\b` 会漏掉带属性的开标签，
  深度回不到 0，注入静默失效（见 `cloud/notice.py: inject_card()` 注释）。
- 找不到锚点就**原样返回**，不要抛异常，不要"差不多就插进去"。
- 插入前检查唯一性与幂等：锚点不唯一时宁可不插。
- **判"该不该显示"不能放在 preprocess 里**：Jinja 会缓存编译结果，preprocess 只跑一次。
  要按用户状态变化的东西，插一个占位符 + 渲染期函数（见 `notice.py: inject_dialog()`
  与 `branch_notice_pending()`、`branch_notice_dialog()` 的写法）。

### 3.5 拦截模块导入 / 在特定时机执行（不推荐，最后手段）

`cloud/bootstrap.py` 用 `sys.meta_path` 钩子包住 `panel_app` 的 loader，在它执行完后
调 `attach()`。这条路径**不要随意扩展**：钩子对 spawn 出来的记录子进程也会跑一遍，
必须保证对它们无害。判断"是不是主程序"不要用 `sys.argv[0]` 或环境变量
（实测 spawn 子进程会继承，不可靠），要用"某个只在主程序里导入的模块被导入了"这类判据。

### 3.6 打包版特有的注意事项

- 打包后 `site` 模块不初始化，`sitecustomize` 不生效，只能靠 `runtime_hooks`；
  且 `runtime_hook` 里 `__file__` 不可靠，要按候选目录找（见 `runtime_hook.py: _candidates()`）。
- `cloud/` 是以 `datas`（普通文件）打进去的，PyInstaller 静态分析**看不到它里面的 import**。
  凡是 `cloud/` 用到、主程序又不一定用到的模块，必须加进 `evo.spec` 的 `_HIDDEN`，
  否则打包版第一次用到就 `ModuleNotFoundError`（实测漏 `sqlite3` 时页面直接没有第 4 项）。
- 改了注入方式，必须两种形态都验证：源码运行 **和** 打包运行。

---

## 4. 硬性兼容要求（每次改动都要满足）

1. **上游可独立运行**：删掉整个 `paimiao_cloud/` 后，上游照常跑；有它但注入失败，也照常跑。
2. **幂等**：注入函数重复调用不会重复插入、重复套壳、重复注册路由。
3. **上游结构变化要降级而不是崩**：上游改了模板/函数/常量名 → 我们那一项静默不生效并记日志，
   其余功能不受影响。所有 `getattr`/锚点查找都要带默认值和存在性检查。
4. **不改变 Evo 的加载顺序**：不要为了注入提前 import 上游的重模块
   （`bootstrap.py` 用 `meta_path` 钩子而不是直接 `import panel_app`，就是这个原因）。
5. **后台开销可控**：新加的后台线程 / 定时逻辑不得拖慢启动，原则上等第一个请求进来再启动
   （见 `cloud/sync.py: arm_once`）。
6. **数据落在 Evo 数据目录**，不落在仓库里（见 `cloud/state.py: data_dir()`：
   优先 `paths.DB_PATH` 的同级目录，其次 `EVO_DATA_DIR`，最后才是项目 `data/`）。
7. **日志里不带敏感信息**：不打印配对口令、token、玩家 ID 原文、完整数据库路径以外的个人数据。
8. **中文注释**：新增/修改的代码注释和日志用中文，说清"为什么这么做"和踩过的坑；
   代码风格与现有文件一致（`from __future__ import annotations`、4 空格、`snake_case`）。
9. **数据协议同步**：如果上传的字段有增减，必须同时更新 `README.md` 的「数据协议」一节，
   并考虑是否需要升 `CONSENT_VERSION`（数据处理行为变了 → 升，让用户重新同意；
   只是说明文案变了 → 升 `notice.py` 的 `NOTICE_VERSION`）。

---

## 5. 改完之后必须自检

- [ ] `git status --short` 里**只出现** `paimiao_cloud/` 下的路径。出现任何别的路径，全部还原。
- [ ] 源码运行：`python paimiao_cloud/run_evo.py` 能起来，打开面板页面确认
      导航第 4 项在、`/cloud/` 打得开、设置页底部卡片在、控制台/日志无 traceback。
- [ ] 降级运行：直接把 `paimiao_cloud/cloud/bootstrap.py` 临时改名（或让 `install()` 抛异常），
      确认 `python code/evo_app.py` 照常工作；验证完立刻改回来。
- [ ] 打包版（改了注入、模板或 import 时必做）：`python paimiao_cloud/build_evo.py` 出包，
      跑起来的 exe 里上述界面元素都在。
- [ ] 日志文件（Evo 数据目录下的云同步日志）里没有 `注入失败` 以外的异常刷屏，
      且没有出现"重复插入/重复注册"的迹象。
- [ ] 行为验证：配对 → 同步 → 解除配对 全流程过一遍；关闭开关后确认不再发起上传。

---

## 6. 需要先问人、不要自己决定的事

- 想改的东西**做不到纯注入**，只能靠改上游文件才能实现 —— 停下来，说清"要改哪个文件、为什么、
  有没有替代方案"，由人决定（可能要提 PR 给上游，那是另一回事）。
- 要新增依赖、新增联网域名、新增上传字段、改变上传时机。
- 要动 `code/`、`tools/`、`tests/`、`config/` 里的任何文件。
- 要升级 `CONSENT_VERSION` 或改动用户可见的数据协议正文。
- 需要改动面板 / 官网界面的视觉设计。

---

## 7. 提交前的一句话总结模板

> 这次的改动全部在 `paimiao_cloud/` 内：`<文件>` 通过 `<注入方式>` 实现了 `<效果>`；
> 上游 0 文件改动。验证：源码运行 ✓ / 降级运行 ✓ / 打包运行 ✓ / 全流程 ✓。
