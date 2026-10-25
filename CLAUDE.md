# Evo（项目说明）

Evo：一个轻量、开源的 Apex Legends 战绩记录器，面向 PC 玩家（Steam / EA App）。通过屏幕截图 + OCR 自动记录每局的名次、击杀、伤害和排位分（RP）变化。绑定微信小程序以后，战绩同步到云端（微信云开发），手机上也能看，可以随时解绑。

命名：项目名 Evo（取自游戏里随伤害升级的进化护甲）。名称中不用 "Apex"、"Legends" 等 EA 商标，只在简介中写 "for Apex Legends"。仓库名和 Python 包名用 `evo-tracker`。

## 先读哪些文件

| 什么时候 | 读 |
|---|---|
| 每次开工 | [claude/todo.md](claude/todo.md)（所有待办只放在那里）、[claude/progress.md](claude/progress.md)（已完成的里程碑和版本记录） |
| **要改上游的任何东西之前** | [paimiao_cloud/AGENT_RULES.md](paimiao_cloud/AGENT_RULES.md)：注入规范的正式版（能改哪里、怎么注入、自检清单）。本文件只写要点，冲突时以它为准 |
| 改模型（识别逻辑、`config/`） | [docs/模型流程.drawio](docs/模型流程.drawio)（实际怎么跑）、[docs/对局全流程图.md](docs/对局全流程图.md)（规则为什么这样定）、[docs/识别经验.md](docs/识别经验.md)（实测结论、游戏画面特征） |
| 改页面 | [docs/面板.md](docs/面板.md) |
| 打包、安装、发布、改托盘或窗口 | [docs/底座.md](docs/底座.md) |
| 做小程序、云同步（`miniprogram/`、Evo 上传战绩） | [docs/小程序.md](docs/小程序.md) |
| 给新版本做宣传图（新功能配图） | [release/做图流程.md](release/做图流程.md)：每次都照它做（`python tools/feature_cards.py`），风格不要自己改 |

- 做完一项：从 todo.md 删掉，在 progress.md 记一笔；不要把待办写进本文件
- 改了模型：流程图、对局全流程图.md 要跟着改，三者（代码、流程图、记录）以最新讨论为准

## 用词和分工

| 名称 | 指什么 |
|---|---|
| 上游 | 原版 Evo：`code/`、`tools/`、`tests/`、`config/`、`panel/`、`webview/` 这些**只读**的代码。由原作者维护，我们不直接改 |
| 注入 | 在运行时（内存里）把上游的行为改成我们要的样子，磁盘上的上游文件一个字节都不动。全部在 `paimiao_cloud/` 里 |
| 模型 | 整套识别逻辑：画面判断、读数、校验、各来源合并、规则配置（`config/`）、测试集。最重要的部分 |
| 引擎 | OCR 本身（RapidOCR + PP-OCRv6 模型文件），模型用到的一个工具 |
| 页面 | Evo 窗口里的面板（`panel/`、`panel_app.py`） |
| 底座 | 托盘、进程、数据库、文件位置、检查更新、打包安装。用户不太关心细节，但**改动也要先问**：问的时候写短一些（改什么、为什么、对用户有什么影响、我的建议），原理不展开 |
| 网站 | 官网和下载（`site/`，只在本机，不进 git） |
| 小程序 | 微信小程序（`miniprogram/app/`）和它的云端（微信云开发的云函数、云数据库，`miniprogram/cloud/`），Evo 里上传战绩的那部分也算 |

## 关于我（开发者）

- 我是编程新手：懂一点 Python。
- 遇到需要我做决定的地方，先问我，不要自己假设
- 我用 **DSH（DeepSeek Harness）** 干活，不是 Claude Code，也不是 Cursor。所以：
  - 项目说明就是这个 `CLAUDE.md`（DSH 会读它），不要再找 `AGENTS.md`、`.cursor/rules`、`.claude/` 之类的东西
  - 干活前先看当前有哪些 skill 可用；叫我用某个 skill 而它没装，就直说没装，别假装用了
  - 需要显示第几行时用 DSH 的格式：文件链接后面接 `:24` 或 `:24–30`（如 `code/live_collect.py:249`），不要写 `L249` / `#L249`

## 项目原则（不可违背）

1. **只看不动**：只做被动截屏（DXGI 桌面复制，如 dxcam）。绝不注入游戏、不读写游戏内存、不 hook 渲染、不在游戏画面上绘制浮层、不模拟任何键盘鼠标输入、不注册全局键盘钩子。Apex PC 版用 EA Javelin 反作弊，比之前的 EAC 更敏感。唯一的例外（2026-10-20 用户决定）：读 Apex 自己的视频设置文件 `videoconfig.txt` 里的分辨率和是不是全屏，判断游戏画面是 16:9、16:10 还是 4:3（`game_aspect.py`；只读不改，设置页可以改成手动选）
2. **轻量**：
   - 后台运行内存 < 100 MB，识别时峰值 < 300 MB，空闲时 CPU 接近 0%
   - 不影响游戏帧数（用 CapFrameX / PresentMon 实测平均帧数和 1% low）
   - 不使用 Electron。界面 = 托盘图标 + 按需打开的 Evo 窗口（Windows 自带的 WebView2 显示本地网页面板），窗口在单独的进程里，关掉就把内存还给系统
   - OCR 只用 CPU、限制单线程、低优先级，模型按需加载；交火时不用引擎
3. **开源**：AGPL-3.0 许可证（2026-10-24 用户决定，之前是 MIT：防止加速器等公司把代码放进不公开源码的产品，不管放在客户端还是服务器上；代码和提示音都用它，传奇头像等素材另算）。可以借鉴其他项目（如 MaaAssistantArknights）的**设计思路**，但不复制别人的代码：即使许可证允许（MAA 也是 AGPL-3.0），Evo 的代码也要全部是自己的，以后才能单独授权给别人。
4. **数据可信优先**：识别置信度低时，不悄悄写入错误数据，而是保留截图并标记"待确认"，由用户在面板中修正；读不出来的项留空，不当作 0。
5. **配置驱动**：识别规则（区域、模板、阈值）写在配置文件里（`config/`），而不是写死在代码中，以便适应赛季更新和社区贡献。
6. **不提供竞技优势**：不做查看其他玩家段位、实时战术提示等功能。只记录用户自己和队友的数据：队友只记对局中左下角和赛后总结页上本来就显示的名字、ID、传奇和这一局的数据；对手的名字、ID 不记，任何人的段位都不记（自己的除外）。
7. **改上游只走注入**（2026-10-24 用户决定，排在所有下面的做法前面）：
   - **上游文件只读**。`code/`、`tools/`、`tests/`、`config/`、`panel/`、`webview/`、`docs/`、`release/`、`assets/`、`build/`、`data/`、`logs/`、`.github/`、根目录的文档和配置，一个字节都不改（也不许用脚本、格式化工具、`sed`、Git 操作间接改）。
   - 所有对上游的改动，一律在 `paimiao_cloud/` 里用**运行时注入**实现：改模块属性、包函数/方法、注册 Flask Blueprint、给 Jinja 模板套 preprocess。细节和踩过的坑全在 [paimiao_cloud/AGENT_RULES.md](paimiao_cloud/AGENT_RULES.md)。
   - 不自作主张把上游文件整份复制进 `paimiao_cloud/` 顶替（上游一升级就成了新旧混合体）。
   - 注入失败必须**静默降级**：删掉 `paimiao_cloud/` 后上游照常跑；有它但注入失败，也只写日志，不能让 Evo 起不来或白屏。
   - 每次都自检：`git status --short` 里不能出现 `paimiao_cloud/` 以外的路径。出现了就全部还原。
   - 遇到"做不到纯注入、必须改上游文件"的情况：**停下来问我**，别自己动手。
8. **网页要审过再用**：改面板、官网这类界面，永远使用 ui-ux-pro-max 这个 skill。当前 DSH 会话里没有它时直说没装，用现有组件的样式和写法，别自己另起一套视觉风格 —— 风格统一比好看重要。

## 技术栈

- Python 3.13（Windows）：新版 numpy 要求 3.12+，onnxruntime 暂不支持 3.15
- 文件布局：程序代码在 `code/`（互相 import 时直接写模块名，如 `import storage`）；只在开发时用的脚本（打包、安装、发布、伪数据、探测工具、`evo.spec`）在 `tools/`（先 `import code_path` 才能 import 程序代码）；测试在 `tests/`；注入层在 `paimiao_cloud/`（见下面的「注入层」）；根目录只放文档和文件夹；素材放 `assets/`。运行命令都带上文件夹，如 `python code/evo_app.py`、`python tools/build.py`
- 截屏 `dxcam`（备选 `mss`）；图像 `opencv-python-headless` + `numpy`（不要装 `opencv-python`，会冲突）；引擎 RapidOCR + onnxruntime（模型内置、单线程）；存储 SQLite；面板 Flask + 纯 HTML/CSS；窗口 `pywebview` + WebView2；托盘 `pystray`；打包 PyInstaller（一个文件夹）。细节见 docs/底座.md
- 上游 0.4.0 的多语言识别：`code/game_lang.py`，语言规则在 `config/lang/<语言>.json`（简体是基准，其他语言只写差异），参照图在 `config/templates/<语言>/`
- 上游的「微信小程序云同步」方案：上游靠 `code/cloud_sync.py` 等把战绩同步到微信小程序（方案见 docs/小程序.md）。**派喵分支用自己的派喵云替代**，这些上游文件只在本机、不入 git
- 数据位置：程序文件夹旁边的 `Evo Tracker Data`（设置页能改，指路文件 `data_location.txt`）。按 `docs/底座.md` 安装的话程序在 `E:\Evo Tracker`；**没装安装版时**，用源码运行 `python paimiao_cloud/run_evo.py` 或 `python code/evo_app.py` 战绩就记在仓库的 `data/` 里（`data/evo.db`、`data/cloud-sync/cloud.log`）。源码版和安装版不能同时开，否则两边各记一份；Evo 开着时不能重新安装
- 下载和一键更新（上游 2026-10-24 决定）：**一键更新只下载变了的文件**（增量更新，0.3.7 → 0.3.8 是 11 MB，完整包 112 MB），以后每次发布都这样；备用线路用国内的腾讯云 COS，一键更新按顺序换线路。细节见 docs/底座.md

## 注入层（`paimiao_cloud/`）

上游一行不改，所有改动都在这里。**改之前先读 [paimiao_cloud/AGENT_RULES.md](paimiao_cloud/AGENT_RULES.md)**，它写了每一种注入方式的正确写法和踩过的坑。日常能用到的只有这些：

| 想做什么 | 从哪进 |
|---|---|
| 源码运行（带注入） | `python paimiao_cloud/run_evo.py` |
| 打包（带注入） | `python paimiao_cloud/build_evo.py` |
| 改上游某个常量、包某个函数 | `cloud/upstream.py`；新功能照 `cloud/lobby_mode/`、`cloud/obs/`、`cloud/wildcard/` 的样子各开一个文件夹，各自的 `inject.py` 负责把钩子挂上去 |
| 加页面 | 自己开 Blueprint（`cloud/panel.py`），前缀不要和上游撞 |
| 改上游模板的内容 | 在 `app.jinja_env` 上套 `preprocess`（`cloud/upstream.py`、`cloud/notice.py`） |
| 日志 | `cloud/state.py` 的 `log()`，写进 Evo 数据目录下的云同步日志 |

- 注入都是**幂等**的：重复执行不会套两层、不会重复注册路由，用模块上的标记位判断
- 上游改了名字/结构，我们那一项要**降级**（静默不生效 + 记日志），不许崩
- 改完注入，源码运行和打包运行**两种形态都要验一遍**

## 模型（怎么识别、记录）

1. 托盘程序每 5 秒看一次进程列表，有 Apex（`r5apex.exe` / `r5apex_dx12.exe`）就启动记录子进程（`live_collect.collect()`）
2. 每次只截一张全屏，先判断是哪个画面（`screens.py`，先比外观、像了才用引擎读关键字）：总结页、经验值页、开局画面、大厅、淘汰画面、冠军横幅、返回大厅按钮。都不是时，看左下角认不认得出自己的传奇（`in_match.py`）
3. 对局中（每 0.3 秒一张）只比外观：认自己和队友的传奇、倒地、成盒观战、右下角手上的武器（`weapons.py`）；右上角那一小块存在内存里，一局结束时才读
4. 一局结束时读对局中最后的右上角；结算画面上读名次、击杀、伤害、RP 等；回到大厅读段位和分数
5. 各来源的读数都记进 `readings` 表，按优先级合并（`post_match.settle`），对不上标待确认；每次回大厅用总分校准各局 RP（`calibration.py`）
6. 大厅模式（`paimiao_cloud/cloud/lobby_mode/`，注入实现）：大厅读到模式后**只要没回大厅就一直有效**，打完点"继续匹配"的第 2、3 局沿用；回大厅读不到就作废；开局横幅读到的永远优先

## 数据（`code/storage.py`，各列的说明在里面）

- `matches`：每局一行（名次、击杀/助攻/击倒、老板分、伤害、RP、段位、待确认……）
- `teammates`：每局最多两名队友（名字和传奇以对局中为准，ID 和数据来自总结页）
- `readings`：每局 × 每项 × 每个来源一行，不只记最后采用的那个
- `lobby_readings`：每次回大厅读到的段位和总分
- `weapon_holds`：每局 × 每一段拿在手上的武器（从这局第一次拿到武器算起的秒数）
- `player_names`：玩家名 → 账号（按名字区分账号，改名后合并）
- 库结构归上游：`SCHEMA_VERSION` + `MIGRATIONS`，只加列、加表。**要改库结构得先问我**（上游文件不能改），能靠注入解决的优先
- 看库的时候优先用只读方式（`sqlite3` 的只读连接 / 先复制一份再看），别在正在运行的 Evo 上写

RP：游戏里没有单一的"总 RP"，只有"段位 + 段内进度"。**总分以大厅为准**（打完一局通常不回大厅，所以每次回大厅都要抓住，用来校准）。每局 RP 的来源：经验值页明细 → 对局中右上角最后的 RP（只限自己最后被淘汰；先成盒、拿冠军时不是最终值）→ 前后两次大厅总分之差（只缺一局时补上）。

## 测试

- 运行：`python tests/run_all.py`（约 1 分钟，23 组；没有截图的组会跳过）。改了模型、记录程序或 `config/` 之后都要跑，改了注入之后**至少要跑一遍**（注入失败会让整组挂掉），新赛季更新后也要跑一遍
- 做法：`tests/fake_game.py` 用"假摄像头"把真实截图按顺序喂给记录程序，数据写进临时文件夹（不碰正式数据），时钟是假的。正确答案写在 `tests/` 里的 CSV 和测试文件里
- 测试只 import 项目代码，项目代码不能 import 测试（否则 PyInstaller 会把测试打进安装包）
- 注入层的专项验证写在 `captures/mode_probe/`（不进 git），用假 dxcam + 假引擎 + 假 PostMatch 按主循环的真实顺序喂帧，比如大厅模式那条路：`python captures/mode_probe/test_continue_match.py`
- 看日志：`data/cloud-sync/cloud.log`（源码运行时）；找 `[大厅模式]` 之类的标签就能定位是哪一项注入在说话

## 协作约定

- 每完成一个能运行的小功能就提交一次，提交信息用中文简要说明
- **提交前必做**：`git status --short` 里只允许出现 `paimiao_cloud/`（以及你自己新加的注入验证脚本）的路径，出现上游路径就还原
- 改动前先说明计划，改动后说明如何验证（跑了什么、看到什么）
- 新增依赖前先说明理由和对内存 / 体积的影响
- 不要删除或覆盖我的截图样本
- 不提交到仓库：`captures/`（测试截图、注入验证脚本）、`data/`、数据库文件、`site/`；小程序的 AppSecret、云开发的密钥（也不写进 Evo）；别的玩家的名字和 ID（队友的测试答案只在本机：`tests/replay_20261016_teammates.csv`、`tests/in_match_teammates.json`）
- **微信小程序的内容永远不上传 GitHub**（上游 2026-10-24 决定；派喵分支遵守同一规则）：`miniprogram/`、`docs/小程序.md`，以及 Evo 里绑定、上传战绩的 `code/cloud_sync.py`、`code/panel_cloud.py`、`code/qr.py`、`panel/templates/_cloud.html`、`tests/test_cloud_sync.py`、`tools/cloud_ping.py`（都写在 `.gitignore` 里，只在本机；之前的提交在本机分支 `local/miniprogram-history`，不推送）。GitHub 上的 Evo 没有这些文件也要能运行（找不到就不显示那一栏、不同步）；小程序上线前也不打进安装包（`tools/evo.spec` 的 `WITH_CLOUD`）。别的文档里写到小程序照常上传。推送前先查 `git log origin/main..main -- <这些路径>` 为空
- **合并上游一律「无历史合并」**（2026-10-25 用户决定）：不要用 `git merge upstream/main`——那会把上游的原始提交（现在 45 条，以后会更多）全部变成我们历史的祖先。改用 `git merge --squash upstream/main`（或软重置后重做一次提交），上游的改动只变成我们这边**一条**新提交，历史里始终只有我们自己的提交。GitHub 上的 "N commits behind" 就是这个原因造成的计数，**不是内容缺失**，下次无历史合并后自动归零。squash 不等于违约：AGPL-3.0 不要求保留 git 提交历史，要求的是保留版权声明、修改说明和源码可获取（LICENSE 全文、README 里的 `Copyright (c) 2026 AugustHuangMX` 与上游链接、`paimiao_cloud/README.md` 的改动清单都在）。**追溯线索写进提交信息**：合并提交里注明上游范围，如 `合并上游 0.4.0（upstream/main e61a85a..457a159，45 个提交）`。
- **合并上游时不要把这些东西带回来**（2026-10-25 用户确认）：

  | 上游有、派喵版**已经删掉或改过**的 | 应该怎么做 |
  |---|---|
  | `code/update_patch.py`（增量补丁包）、`tools/cos.py`（腾讯云 COS 备用线路） | 不要再存在。一键更新只下完整安装包、单线路（源码里的默认是 `code/update_check.py` 的 `dl.evotracker.cn`，注入层实际把它换成 `tools.skycraft.cn`） |
  | `updater.py` / `update_check.py` / `_update.html` / `base.html` 里的补丁包、`mirrors`、`LATEST_URLS`、`COS_BASE` 分支 | 不要恢复。注入层 `paimiao_cloud/cloud/upstream.py` 的 `UPDATE_BASE` 指向 `tools.skycraft.cn`，别被上游的 `dl.evotracker.cn` 覆盖 |
  | `code/cloud_sync.py` 等小程序云同步文件 | 只在 `.gitignore`，不进 git |
  | 匿名统计（`telemetry.py`）、"全体玩家"数据页（`global_stats.py`）、开屏非官方分支弹窗 | 由注入层关掉（`cloud/upstream.py`、`cloud/global_stats/`、`cloud/notice.py`）；上游升级后要重新验证模板锚点，见下一条 |
  | 总结页截图存 JPG | 派喵版存 WEBP（`code/screenshots.py` 的 `to_webp()` / `WEBP_RATIO`） |

  合并完必查：`git diff --stat HEAD~1` 里不该出现 `tools/cos.py`、`code/update_patch.py` 重新新增；`rg -n "COS_BASE|MIRROR_HOSTS|update_patch" code/ tools/` 应为空；`paimiao_cloud/cloud/upstream.py` 的 `UPDATE_BASE` 仍是 `tools.skycraft.cn`；然后跑 `tests/run_all.py` 和 `captures/mode_probe/test_inject.py`。
- **模板锚点是上游升级最容易悄悄失效的地方**：注入层靠字符串锚点隐藏/改写上游模板（如 `paimiao_cloud/cloud/upstream.py:161` 的 `_SETTINGS_BLOCK_END = "</section>"`）。已经踩过一次：0.4.0 改掉 settings.html 匿名统计区块的结尾结构后，旧的 `"{% endif %}\n    </section>"` 锚点找不到、不报错、区块照常显示。上游升级后逐个验证 `cloud/upstream.py`、`cloud/notice.py`、`cloud/global_stats/` 里的锚点都在，方法：调用注入的 `_patch_template()` 后检查目标文字已消失（如 `'匿名统计' in out` 应为 False）。
