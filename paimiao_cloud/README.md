# Evo 派喵云同步（零侵入外挂模块）

把 Evo 识别出来的战绩同步到派喵小程序。

**这个文件夹是独立模块：Evo 上游仓库的任何一个文件都没有被修改过。**
（`git diff upstream/main` 永远为空，`git status` 只有 `?? paimiao_cloud/` 一条。）

---

## 一、它长什么样

Evo 顶部导航多出第 4 项 **「派喵云同步」**，点进去是一个独立页面，按用户状态显示三张不同的脸：

| 状态 | 页面上有什么 |
|---|---|
| **还没同意** | 只有七节《数据传输指引》正文 + 一个必填勾选框 + 「同意并继续」。**没有**配对、没有开关、没有任何同步功能 |
| **同意了、还没配对** | 账号说明、手机贴图、「生成配对码」按钮（服务器地址是内置的派喵服务器，不需要填） |
| **已经配对** | 设备名与有效期、自动同步开关、上传情况（已上传 N 局 / 最近一次时间）、「立即同步」、「解除配对」，以及独立卡片的「撤回同意」 |

同步目标固定是派喵的服务器 `https://tools.skycraft.cn/apex/api/battlestats`（`cloud/state.py`
的 `DEFAULT_SERVER`）。调试时可以用环境变量 `EVO_CLOUD_SERVER` 指到本机假接口。

状态存 `DB_PATH` 同级的 `cloud-sync/state.json`（源码运行时由 `source_run.py` 把
`EVO_DATA_DIR` 指到安装版数据文件夹，所以自动跟着走）。原子写，不会写坏。

---

## 二、两种运行形态，两种注入方式

这是全篇最关键的一段：**源码运行和打包运行的注入机制完全不同，不能共用。**

| | 源码运行 | 打包成 exe |
|---|---|---|
| 入口 | `python paimiao_cloud/run_evo.py` | 双击 `Evo.exe` |
| 注入点 | `inject/sitecustomize.py`（解释器自动导入） | `runtime_hook.py`（PyInstaller `runtime_hooks`） |
| 靠什么找到自己 | `PYTHONPATH=` | `sys._MEIPASS/paimiao_cloud` |

为什么不能共用：**实测打包后 `site` 模块根本不初始化（`site=False`），`PYTHONPATH`
也不会进 `sys.path`**，所以 `sitecustomize.py` 这条路对 exe 彻底无效。
这条结论是用一个最小 PyInstaller 探针实际打出来验的，不是查文档猜的。

两条路最后都落到 `cloud/bootstrap.py` 的 `install()`。

### install() 之后发生什么

1. 往 `sys.meta_path` 插一个 `_PanelAppFinder`，专门等 `panel_app` 被 import。
2. `panel_app` 加载完 → `attach(module)`：
   - `app.jinja_loader` 换成 `ChoiceLoader([原loader, cloud/templates])` —— 磁盘上的模板文件一个字节都不动
   - `register_blueprint(cloud)`（`/cloud` 前缀）
   - 把 `sync` 的库路径设成 `app.config["DB"]`（**不能用 `paths.DB_PATH`** —— 面板支持 `--db` 指向别的库）
   - 挂一个 `before_request`，第一个请求到来时才起同步线程，不拖慢启动
3. 兜底：包一层 `Flask.run`。直接 `python code/panel_app.py` 时模块名是 `__main__`，
   meta_path 钩子不触发（实测过），但那条路最后必调 `Flask.run`，且那时 `DB` 已设好。

### 导航第 4 项是怎么来的

`code/panel_app.py:43` 的 `NAV` 是元组常量，`:131` 以 `{"nav": NAV}` 传给模板。
硬改就得动上游文件。所以：**Blueprint 的 `app_context_processor` 会覆盖 app 级同名键**
（实测：后注册的赢），我们在自己的 Blueprint 里注册一个 context processor 返回 4 项，
`base.html` 的 `{% for endpoint, label in nav %}` 就自然多一项，上游一个字不改。

---

## 三、安全边界（都写进代码里了，不是文档承诺）

- **只接受 https，或本机 `http://127.0.0.1` / `localhost`** —— 上传的是玩家数据，不许裸奔
- 设备令牌走 `Authorization: Bearer`，`TIMEOUT=8s`，响应体上限 512 KB
- 所有 POST 必须带 CSRF token（Blueprint 自动继承 `panel_app.py:112-119` 的 `before_request`）→ 无 token 一律 **403**
- 上传顺序：`agreed() and paired() and enabled` 三者同时成立才起线程
- 只读打开数据库（`file:...?mode=ro`），不建表、不写库、不挂 hook
- **唯一的例外是「从云端取回」**（`cloud/pull.py`）：那是刻意设计成要写库的，
  规矩见下面第三节末尾

### 「从云端取回」为什么是唯一会写库的地方

换了电脑、或者重装了 Evo，本地库是空的，但云端还有历史。取回就是把这个补回来。

**它和上传的约束正好相反，所以单独一个文件、单独一套规矩：**

|           | 上传 `sync.py`        | 取回 `pull.py`            |
|-----------|----------------------|--------------------------|
| 打开方式   | `mode=ro`，物理写不坏  | 读写，**必须**（要插行）    |
| 写什么     | 不写                  | **只 INSERT，从不 UPDATE/DELETE** |
| 何时触发   | 定时线程自动           | 只有用户点按钮             |
| 写之前     | —                    | 先备份到 `backups/`，只留最近 5 份 |

六条落地规矩：

1. **判重键和服务器是同一个函数** `sync.client_id_of()`（`sha256(ended_at)`）。
   服务器认成同一局，本地也认成同一局 —— 两边口径不可能分叉。
2. **本地已有的一律跳过**，插入操作天然幂等，拉一百次也只有第一次插得进去。
3. **老库少列也不崩**：`PRAGMA table_info` 过滤掉本地没有的列（和上传侧的
   `_has_columns` 是同一套思路）。
4. **截图路径留空**：`evidence_path` 指向的是另一台电脑上的文件，本机没有。
   面板上点开这局，右侧截图区会是空的 —— 这是诚实的降级。
5. **`rp_source` 写成「云同步」**，一眼能看出这条不是本机识别出来的。
6. **失败不动库**：连不上服务器就抛 `CloudError`，一行都不写；写一半出错就 `rollback()`。

页面上分了「看看会拉回什么」（`GET /cloud/pull/preview`，纯只读预演）
和「从云端取回」（`POST /cloud/pull`）两步 —— 会改库的操作不该点一下就执行。


---

## 四、派喵版差异（6 项，也全是注入）

派喵版和上游 Evo 有 6 处不同，全部由 `cloud/upstream.py` 与 `cloud/notice.py` 在**内存里**换掉，
磁盘上的文件一个字不动：

| # | 改什么 | 怎么做 |
|---|---|---|
| ① | 更新检查改由派喵服务器提供 | 换掉 `update_check.UPDATE_URL` / `DEFAULT_PAGE`，并**原地扩** `TRUSTED_HOSTS` / `DOWNLOAD_HOSTS` |
| ② | 同步目标固定为派喵服务器 | `state.DEFAULT_SERVER`，页面上不再有地址输入框 |
| ③ | 源代码链接改 `CuteLittleSky/Evo` | 换掉 `panel_app.SOURCE_URL` |
| ④ | 去掉意见反馈与 QQ 群 | 模板源码里按 `{# 意见反馈` → 配对括号找到的 `</div>` 整块删掉，并把 `FEEDBACK_URL` / `FEEDBACK_GROUP` 置空 |
| ⑤ | 隐私措辞改写 | 三处字符串替换：`不上传任何数据` → `若不使用派喵云同步功能，将不会上传任何数据`；`只下载官网上一个公开的版本号文件` / `只下载官网上公开的版本号文件` → 去掉"官网上" |
| ⑥ | 加「非官方分支说明」弹窗 + 设置页许可卡片 | `cloud/notice.py`：`inject_dialog` 在 `{% block scripts %}` 前插占位符，`inject_card` 按 block 配对插进 settings 的 content 块尾 |

### ⑥ 的两个坑（都踩过，写在这儿免得再踩）

**弹窗插入为什么是"占位符"而不是直接拼 HTML**：Jinja 会缓存编译好的模板，`preprocess`
只在第一次编译时跑一次。若在那时就判断"用户看过没有"，第一次请求必然插入弹窗并把结果
缓存下来 —— 用户点过「我已知晓」之后照样天天弹，必须重启才消失。所以插进去的是一段
`{% if branch_notice_pending() %}{{ branch_notice_dialog(token) }}{% endif %}`，
每次渲染现算。

**`branch_notice_pending()` 必须是纯函数**：Jinja 在**同一次渲染**里会把那个 `if`
求值两遍（实测）。最初把"问一次就消耗掉"写在判断函数里，第一遍 True、第二遍 False，
`if` 整体判假 —— 弹窗一个字节都进不去页面，而且是**沉默失败**（不报错、不打日志）。
现在记账挪到 `_dialog_for_template()`，那是 `if` 为真之后才调用的，一次渲染最多跑一次。

还有一处：`.dialog` 在上游是 `padding:0`，内边距由 `.dialog form` 提供（`style.css:176`）。
弹窗故意不用 `<form>`（点按钮不该提交），所以 `notice.py` 自己补了一份
`.branch-dialog .dialog-body { padding:24px }`。

模板那两项靠 `jinja2.Environment.preprocess` 钩子（`_install_template_hook`），
它拿到的是模板源码字符串，改完再交给 Jinja 编译 —— 所以不用碰磁盘上的 `.html`。

**白名单必须一起扩。** 只换 `UPDATE_URL` 不改 `DOWNLOAD_HOSTS` 的话，一键更新会被
`safe_download()` 拒掉变成"只能手动去下载页"——不报错，只是按钮没了。

> `--auto-confirm` 之外还有 `confirm_code()`，方便手动验（这部分随早先的假后端脚本一并移除了）。

---

## 五、自己检查

手动冒烟（早先那套脚本化自检 —— `selftest_cloud.py` / `render_check.py` / `notice_check.py` /
`inject_check.py` / `spec_check.py` / `exe_check.py` / `upstream_check.py` / `update_live_check.py`
以及假后端 `fake_server.py` —— 已移除）。想验注入是否生效，最直接的办法是照 §六 起一个带注入的面板，
看导航栏有没有多出「派喵云同步」那一项；再看设置页底部有没有「关于本分支 / 第三方许可」折叠卡片。

---

## 六、接本机假接口联调

需要先自己起一个本机 HTTP 假接口（原 `fake_server.py` 已移除，可按下面的接口契约自行实现或用
任一 mock 服务器）。接口形状与真服务器一致：`/battlestats/pair/start` 发
`Set-Cookie: apexpacks_pair`，`/battlestats/pair/poll` 确认后发
`Set-Cookie: apexpacks_sid`，码是 6 位字母数字（`23456789ABCDEFGHJKMNPQRSTVWXYZ`）。

```bash
# 终端 2：带注入的面板（先用任意假接口或真服务器顶替终端 1）
set EVO_DATA_DIR=%TEMP%\evo-cloud-demo
set EVO_CLOUD_SERVER=http://127.0.0.1:8899
set PYTHONPATH=<仓库>\paimiao_cloud\inject
python tools/demo_data.py
python code/panel_app.py --db %TEMP%\evo-cloud-demo\data\demo.db --port 5077 --no-browser
```

浏览器开 `http://127.0.0.1:5077/cloud/`。地址不用填，`EVO_CLOUD_SERVER` 已经指过去了。

> `run_evo.py` 跑的是完整 `evo_app.py`，**不接受 `--db/--port` 面板参数**（实测报
> `unrecognized arguments`）。只测面板就照上面直接跑 `panel_app.py`。

---

## 七、打包分发

```bash
python paimiao_cloud/build_evo.py
```

产物 `dist/Evo/Evo.exe`，和上游 `tools/build.py` 的结果一样（图标、WebView2、许可证都照做），
区别只在 spec 换成了 `paimiao_cloud/evo.spec`。

`evo.spec` **不复制** `tools/evo.spec`，而是把它 `exec` 进来，只在 `Analysis` 被调用的
那一刻塞两样东西：

```python
runtime_hooks=[paimiao_cloud/runtime_hook.py]   # 打包版的注入时机
datas+=[(paimiao_cloud 目录, "paimiao_cloud")]        # cloud 包本身
hiddenimports=["sqlite3", "hashlib", "urllib.request", ...]
```

`hiddenimports` 那行是**必需的**，不是保险：`cloud/` 作为 datas 打包，PyInstaller 的静态
分析看不到它里面的 `import`。漏掉 `sqlite3` 的后果是——exe 能启动、面板能开，但注入
在 `attach()` 里抛异常被吞掉，导航少一项，而且**看起来什么都不像出错**。这个坑当初是靠打真包发现的。

上游改了 datas / excludes / upx / 图标，这里全自动跟着变，不用同步维护。

### 七点五、自动打包发布（GitHub Actions）

`.github/workflows/release.yml` 负责在 CI 上打包并发到 GitHub Releases，省得每台机器
手动装 onnxruntime / rapidocr 再打包（这些库在 Windows 上能直接 pip 装）。

触发方式：

- 推一个形如 `v0.3.7` 的 tag（**tag 必须和 `code/storage.py` 的 `APP_VERSION` 一致**，workflow 会校验）
- 或在 Actions 页面手动点「Run workflow」（只打包不建 Release）

它做的事：装依赖（`paimiao_cloud/requirements-build.txt`）→ `python paimiao_cloud/build_evo.py`
→ 压成 `Evo-<版本>-win64.zip` 并算 SHA-256 → 建 Release 附 zip + `.sha256`
→ 用 `repository_dispatch`（event_type `evo-release`）通知 ApexPacks 仓库更新
`server/data/evo-latest.json`。

> 注意 `build/` 是 gitignore 的，CI 上 `code/webview2.py` 里那段没缓存时它会去微软
> 下 WebView2 安装程序并核对数字签名，这一步在 windows-latest 上能跑通（用
> `Get-AuthenticodeSignature`）。

需要在 Evo 仓库配两个东西：

| 类型 | 名字 | 用途 |
|---|---|---|
| secret | `APEXPACKS_DISPATCH_TOKEN` | 对 ApexPacks 有 `contents:write` 的 PAT，用来发 repository_dispatch |
| variable | `APEXPACKS_REPO` | ApexPacks 仓库全名，如 `CuteLittleSky/ApexPacks` |

ApexPacks 那边收到 dispatch 后由 `.github/workflows/evo-latest-sync.yml` 写文件：
它会**再校验一遍** `evo-latest.json` 的字段（version 匹配 `x.y.z`、page 是 https、notes ≤10 条每条 ≤200 字、
download.sha256 是 64 位小写十六进制、size 是 1~1GiB 整数），和 `code/update_check.py:82`
的 `fetch_latest` 完全对齐——字段不对 Evo 不会报错，只会让「一键更新」按钮静默消失，
所以必须两头都校验。写好后有变化才提交，再显式 `gh workflow run nuxt-docker-publish.yml`
让线上生效（用默认 GITHUB_TOKEN 推的 commit 不会触发其它 workflow，这是 GitHub 的防递归规则）。

---

## 八、隐私边界（写在这里，也写在程序里）

开启同步后上传：

- **每一局**：对局时间（结束时间，有的话还有开局时间）、模式、地图、传奇、名次、
  击杀、助攻、击倒、伤害、参与度、**小队总击杀**、**救援队友次数**、**复活次数**、
  RP 变化、存活时长、**你自己的玩家名**
- **段位**（2026-10-22 起）：段位名（如「钻石」）、段、段内排位点数、
  以及 Evo 自己换算出的总分。来源有两个 —— 对局结算画面，和**游戏大厅读数**
  （后者才是连续的段位曲线，单独存在 `battlestats_rank_history` 表里）
- **队友**：玩家名、传奇、击杀、助攻、击倒、伤害、存活时长、救援次数

不上传：截图、数据库文件、账号密码、文件路径、硬件信息。

**不使用这个功能就什么都不传。** 设置页那句"不上传任何数据"已按你要求改成
「若不使用派喵云同步功能，将不会上传任何数据」——措辞改了，实际行为没变。

队友名字那条最敏感 —— 它在页面的《数据传输指引》第四节单独说明，并明说"不接受就别开"。
不想传队友，改 `cloud/sync.py` 的 `_mates()` 返回 `{}` 即可。

### 2026-10-22 扩了上传范围，协议版本也跟着改了

`cloud/state.py` 的 `CONSENT_VERSION` 从 `2026-10-21` 改成 `2026-10-22`。
**这是有意的**：老用户已经同意过旧版本，而旧版本没写"上传段位""上传小队总击杀"
这些东西。不升版本号就是拿旧同意去覆盖新范围。升了之后老用户下次打开这一页会被要求重新同意，
在他重新点同意之前**同步线程不会启动**（`agreed()` 检查版本号）。

以后再加字段，改完正文记得改版本号。

---

## 八点五、一局在服务器上的「身份证」：`client_id`

服务器用 `(你的账号, client_id)` 当主键，所以 `client_id` 必须是**一局永久不变的编号**。

现在用的是**这一局的结束时间**：

```python
def client_id_of(match):
    stamp = str(match.get("ended_at") or "").strip()
    return "edo-" + hashlib.sha256(stamp.encode("utf-8")).hexdigest()[:28]
```

`ended_at` 是识别到总结页的那一刻（`code/storage.py:27` 原话「识别到总结页的时间」），
所以它是这一局在时间轴上的绝对位置，而不是某一台电脑的流水号。

### 为什么不用 Evo 自己的 `match_id`（这是换掉它的原因）

`match_id` 是 SQLite 的 `INTEGER PRIMARY KEY AUTOINCREMENT`，**每台电脑各算各的**：

```
笔记本 同一局（本机 match_id=12）  -> evo-12
台式   同一局（本机 match_id=456） -> evo-456
同一条记录？ False          ← 同一局变成云端两条
```

换绑小程序账号更糟：前一个账号的 `evo-1`…`evo-N` 还在服务器上，
第二个人的第一局也叫 `evo-1`，于是只会**更新字段**、不会新增，
战绩列表里混进别人的历史而且行数还变少了。

用结束时间同时解决三件事：

| 场景 | `match_id` 方案 | 结束时间方案 |
|---|---|---|
| 在面板上改击杀/伤害 | 键不变 ✅ | 键不变 ✅ |
| 把数据文件夹拷到另一台电脑 | 变 ❌ 同一局两条 | 不变 ✅ 自动合并 |
| 换绑到另一个小程序账号 | 撞号 ❌ 覆盖 | 不变 ✅ 自动合并 |
| 两台电脑各自打不同的局 | 正常 | 正常（时间本来就不同） |

### 为什么不能用内容哈希（另一个真 bug）

早期版本把 `ended_at|玩家名|模式|地图|传奇|名次|击杀|助攻|伤害|队友数`
拼起来取 sha256。用户在面板上点了「详情与修改」改了击杀，`client_id`
就变成一串新哈希 —— 服务器认为这是新的一局，于是**同一局在库里出现两遍**
（一条 `kills=99`、一条 `kills=0`）。这条是当初实测复现出来的，务必别回退。

**为什么不能带上账号 ID**：它由 OCR 读出，识别不准就会换一个键，
等于每读错一次就多一局。这是明确否决过的方案。

`fingerprint()` 那个内容哈希仍然有用，但只用来判断「要不要重传」，不参与主键。

**残余风险**：两台电脑**同时**录同一场时，各自截到总结页的秒数可能差几秒，
会各存一条。概率低、不影响单机使用，暂时接受。

---

## 九、已知边界

- **`player_name` 可能为空**。它是 `code/storage.py:110` 那次迁移（2026-10-17）才加的列，
  更早的局是 NULL。服务器不能只靠玩家名认账号，要允许为空。
- **`tools/demo_data.py` 造的伪数据不填 `player_name`、也不建队友**。所以联调时看到的
  payload 里 `playerName=null`、`teammates=[]` 是伪数据的问题，不是代码的问题。
- **段位分数线**用的是 `config/ranks.json`（第 30 赛季）。第 31 赛季 EA 说会有大改，
  届时必须重新核对，否则 RP 换算会错。
- **取回的对局没有截图**。`evidence_path` 为空，点开那局右侧截图区是空的。
  要补只能等这台电脑真的再录一次，或者手动从旧机器拷 `evidence/` 目录过来。
- **取回一次最多 200 局**（服务器 `limit` 硬上限）。历史更多时得多点几下，
  每次都从最新的往旧拉，已经有的会自动跳过，所以重复点没有副作用。
- **取回不会把数据变"已核对"**。`needs_review` 原样保留 —— 云端只知道当时识别得准不准。
- **分支说明弹窗一个进程只弹一次**，弹过之后面板内跳转不再出现（这是刻意的：切三个页面
  被拦三回很难受）。想再看内容不必重启，设置页底部那张「关于本分支 / 第三方许可」卡片
  常驻、默认折叠，内容与弹窗一致。要**重新触发弹窗**只能删掉 `cloud-sync/state.json` 里的
  `notice_seen` 字段后重启 Evo —— 没有做界面按钮，因为那个说明本身是"知道一次就够了"的东西。
- 本机没装 cv2 / dxcam / rapidocr，所以跑不了真正的采集循环，只能测面板与注入层。
