# 参与开发 Evo

Evo 是什么、给玩家怎么用，见 [README](README.md)。这一份写给一起开发的人：怎么合作、要守的规矩、怎么装环境、怎么测试。

## 怎么合作

- **需求和决定都由 AugustHuangMX（项目拥有者）定**，所有改动经他同意才合并
- 派给你的活：写在 GitHub Issue 里，指派给你
- 你自己的想法：**先开一条 Issue**，写想做什么、为什么、大概怎么做，他回复同意了再动手（免得做完了不合适，白做）
- 动手：从最新的 `main` 开一个分支。名字按用途写前缀：`feat/` 新功能、`fix/` 修问题、`refactor/` 整理代码、`docs/` 文档，比如 `fix/lobby-rp`
- 做完提 PR（Pull Request），描述里写清：改了什么、为什么、怎么验证的（跑了哪几组测试，结果怎样）；关联 Issue（写 `Closes #12`）
- 一个 PR 只做一件事，尽量小，方便看
- `main` 有保护：不能直接推；PR 要 AugustHuangMX 批准才能合并；批准以后再推新提交，要重新批准
- 提交信息用中文：第一行简要说明，空一行，下面写改了什么、为什么、怎么验证的
- 你的代码按 AGPL-3.0 合进来，版权归你自己，合并时保留你的提交（署名）

## 项目原则（不能违背）

1. **只看不动**：只做被动截屏（DXGI 桌面复制，`dxcam`）。绝不注入游戏、不读写游戏内存、不 hook 渲染、不在游戏画面上画浮层、不模拟键盘鼠标、不注册全局键盘钩子。Apex 用 EA Javelin 反作弊，非常敏感。唯一的例外：只读 Apex 自己的视频设置文件 `videoconfig.txt`，看分辨率和是不是全屏（`code/game_aspect.py`）
2. **轻量**：后台内存 < 100 MB，识别时峰值 < 300 MB，空闲时 CPU 接近 0%，不影响游戏帧数。不用 Electron；OCR 只用 CPU、单线程、低优先级，模型用到才加载
3. **代码全部自己写**：可以参考别的项目的思路，但不复制别人的代码，即使许可证允许
4. **数据可信优先**：读得没把握时不悄悄写进错误数据，而是留截图、标"待确认"让玩家改；读不出来的项留空，不当作 0
5. **配置驱动**：识别规则（区域、模板、阈值）写在 `config/` 里，不写死在代码里
6. **加新依赖前先在 Issue 里说**：为什么要加，对内存、安装包大小有什么影响

## 装开发环境

要 Windows 10 / 11 和 Python 3.13。

```powershell
git clone https://github.com/AugustHuangMX/Evo.git
cd Evo
python -m venv .venv
.venv\Scripts\activate
pip install dxcam==0.3.0 mss==10.2.0 opencv-python-headless==5.0.0.93 numpy==2.5.4 rapidocr==3.10.0 onnxruntime==1.31.0 flask==3.1.3 pywebview==6.2.1 pystray==0.19.5 pillow==12.3.0
```

- 不要装 `opencv-python`，会和 `opencv-python-headless` 冲突
- OCR 模型（PP-OCRv6）随 `rapidocr` 一起装好，不用另外下载
- 打包（PyInstaller）、发版由 AugustHuangMX 做，你不用装

运行（命令都在项目文件夹里、带上文件夹名）：

- 整个 Evo（托盘 + 窗口）：`python code/evo_app.py`。用源码运行时，数据库、截图、日志写在项目文件夹的 `data/`、`captures/`、`logs/`（都不进 git）
- 只看页面、用演示数据：`python tools/demo_data.py`，然后 `python code/panel_app.py --db data/demo.db`
- `code/` 里的模块互相 import 时直接写模块名（`import storage`）；`tools/` 里的脚本要先 `import code_path`

## 测试

照 [docs/测试SOP.md](docs/测试SOP.md) 做：按改动范围跑，不多跑也不少跑。

- 单独跑一组：`python tests/test_xxx.py`；全部：`python tests/run_all.py`（约 10 分钟）
- 测试截图只在 AugustHuangMX 的电脑上，所以在你那里很多组会显示"⏭ 跳过"。**跳过不算通过**，PR 里要写出来；改了模型（识别、`config/`、记录程序）或 `storage.py` 的 PR，由他在自己电脑上跑全部
- 测试只 import 项目代码，项目代码不能 import 测试（否则会被打进安装包）

## 不在 GitHub 上的东西

下面这些只在 AugustHuangMX 的电脑上，要用到时找他要，由他决定给不给：

- 测试截图 `captures/`（截图上有别的玩家的名字）、战绩数据库
- 微信小程序和云同步的代码（`code/cloud_sync.py` 等，见 `.gitignore`）：GitHub 上的 Evo 没有它们也能运行，设置页不显示那一栏
- 官网 `site/`、各种密钥

**不要提交**：别的玩家的名字和 ID、截图、数据库文件、任何密钥。

## 找代码、读文档

- [docs/代码地图.md](docs/代码地图.md)：每个文件一行，写它是干什么的，按分工分组。它是 `python tools/code_map.py` 生成的，不要手改：加、删、改名文件以后（`code/`、`tools/` 的新文件先在脚本的 `SECTIONS` 里分好组），重新运行一次，再跑 `python tests/test_code_map.py`
- 改识别逻辑：[docs/模型流程.drawio](docs/模型流程.drawio)（实际怎么跑）、[docs/对局全流程图.md](docs/对局全流程图.md)（规则为什么这样定）、[docs/识别经验.md](docs/识别经验.md)（实测结论、游戏画面特征）
- 改页面：[docs/面板.md](docs/面板.md)；托盘、进程、数据库、打包、更新：[docs/底座.md](docs/底座.md)
- 待办：[claude/todo.md](claude/todo.md)
- [CLAUDE.md](CLAUDE.md) 是给 AI（Claude Code）读的项目说明。你也用 Claude Code 的话它会自动生效，但里面"关于我（开发者）"说的是 AugustHuangMX，不是你
