# 给 Evo 增加「OBS 虚拟摄像头」采集源 · 方案

状态：**2026-11-07 已实现源码版原型（新增 camera 来源 + 配置指引），上游 0 改动；待打包形态与真机验收。**
日期：2026-11-07 ｜ 相关：`paimiao_cloud/cloud/obs/`、`paimiao_cloud/cloud/capture/`、`code/capture.py`

---

## 一、先回答"能不能检测到"：能，而且能直接抓帧

本机实测（OBS 32.2.2 正在运行、虚拟摄像头已开启）：

| 检查项 | 结果 |
|---|---|
| DirectShow 设备枚举 | 有 `OBS Virtual Camera`（第 2 个视频设备：0 Integrated Camera / 1 USB3 Video / 2 OBS Virtual Camera） |
| ffmpeg 抓 1 帧 | `1920×1080` / `rgb24`，1.5 MB；内容就是**实时 Apex 画面**（不是黑帧、不是占位） |
| 项目自带 OpenCV 走 DirectShow | `cv2.VideoCapture(2, CAP_DSHOW)` 0.41 秒打开，读到 `1080×1920×3` 的 BGR 帧，backend=`DSHOW` |
| 依赖打包时被删的 29MB ffmpeg DLL？ | 不需要。DirectShow 后端在 `cv2.pyd` 里，本机 backend 明确是 DSHOW |

采集到的实时画面见 `captures/obs_vcam_probe/vcam_frame.png`。

**顺带查出一个既有 bug**：`paimiao_cloud/cloud/capture/devices.py` 是一段**从没被任何地方 import 的半成品**（枚举采集卡用），它读设备名有两处错：
1. `VARIANT` 里 BSTR 指针的偏移写成了 16，实际应是 8（x64）；
2. `IPropertyBag::Read` 的属性名写成了数字 `"14"`，实际应传 `"FriendlyName"` / `"DevicePath"`。

我按这两处改了之后（仅临时 monkeypatch 验证，没动源文件），枚举恢复正常：`0 Integrated Camera / 1 USB3 Video / 2 OBS Virtual Camera`，且 **2 号 == OpenCV 的 2 号设备**。也就是说：修好这两处，就能**按名字可靠地找到 OBS 的序号**，再用 OpenCV 打开它。

---

## 二、为什么现在走「投影窗口」而不是虚拟摄像头（以及用户的想法为何成立）

现状（`obs/projector.py` 顶部注释）：
- 现在主机模式是：OBS 把画面**全屏投影**到一块屏幕上，Evo 用 dxcam 截那块屏。
- 代价：**必须有一块屏专门放投影（前台可见）**；PC 玩家也是把游戏全屏放前台。

当初不用虚拟摄像头，列了 4 条理由，其中第 1 条是「虚拟摄像头会**独占采集卡**，OBS 自己也开不了同一个采集源」——**那是针对「Evo 直接打开采集卡」**。

而用户这次想的正好相反：**OBS 占着采集卡，Evo 读 OBS 的虚拟摄像头输出**。这样 OBS 依然正常用采集卡，Evo 只是当第二个观众——所以这条反对理由不成立，思路是通的。

> 说明：OBS 本身早就有「虚拟摄像头」功能，不是要给它写插件；要做的是**让 Evo 增加一个「读 OBS 虚拟摄像头」的采集来源**。

---

## 三、方案设计（全部在 `paimiao_cloud/` 注入层，上游 0 字节改动）

来源从两种变三种：`screen`（整屏）/ `projector`（OBS 投影）/ **`camera`（OBS 虚拟摄像头，新）**。

### 改动清单

| 文件 | 改什么 |
|---|---|
| `cloud/obs/settings.py` | `SOURCES` 加 `"camera"`；新增设备选择键 `obs_camera_device`（默认自动找名字含 `OBS Virtual Camera`，找不到选第一个可用视频设备） |
| `cloud/capture/devices.py` | 修上面两个 bug（VARIANT 偏移、FriendlyName 属性名）；加 `find_obs()` 帮助函数按名字返回序号 |
| **`cloud/capture/frame.py`（新）** | `DirectShowCamera`：用 `cv2.VideoCapture(index, CAP_DSHOW)`。接口对齐 dxcam：`width` / `height` / `grab(new_frame_only=False)` / `release()` / `start()` / `stop()`；`grab` 返回 BGR numpy，拿不到帧返回 `None`（上游 `if frame is not None:` 已能跳过该帧） |
| `cloud/obs/capture.py`（注入点） | 现在包的是 `dxcam.create`；改成**同时包 `capture.open_camera`**：`source=="camera"` 时直接返回 `(DirectShowCamera, notes)`，不碰 dxcam；否则原样转发（projector 仍由 `dxcam.create` 那个包装生效）。注入失败静默降级 |
| `cloud/obs/trigger.py` | `projector_running()` 泛化为 `source_active()`：projector→`find_projector()`；camera→见下方决策 A；其他→原进程判断 |
| `cloud/obs/page.py` | 设置页「画面来源」下拉加第三项「OBS 虚拟摄像头」；选中后显示：检测到的设备名 / 能否打开 / 分辨率 / 状态（复用现有 `/obs/status` 自动刷新机制，扩展返回字段）；多设备时给个设备下拉 |
| `cloud/obs/tray.py` | 主机模式下托盘提示「等待投影窗口」→ 按来源改成「等待虚拟摄像头画面」 |
| 记录子进程日志 | 写清「截法：directshow / 设备：OBS Virtual Camera / 1920×1080」，方便排查 |

### 关键实现点
- 上游建相机的真正位置是 `live_collect.py:669` 的 `capture.open_camera(log)`（不是直接调 dxcam），所以**包 `capture.open_camera` 是最干净的上游接管点**。
- 接口与 dxcam 相机一致，`live_collect` 里 `camera.width/height/grab/release` 一处都不用改。
- 分辨率/宽高比：OBS 虚拟摄像头通常输出 1920×1080（16:9），`layout.py` 会按比例适配；如果 OBS 画布不是 16:9，需要 `set_game_aspect` 或提示用户。
- 不新增依赖：`cv2` 记录子进程本来就在用；每帧 1920×1080×3 约 6 MB，占用可忽略。

---

## 四、需要你拍板的几件事

### A. 主机（电脑上没有 `r5apex.exe`）时，用什么判断"开始/停止记录"？
**难点**：托盘进程**不能**去开虚拟摄像头来探测——虚拟摄像头一个进程占着时，会和记录子进程抢同一个设备。所以不能拿"能不能打开 vcam"当探针。三个选项：

| 选项 | 做法 | 优点 | 代价 |
|---|---|---|---|
| **1（推荐）** | 来源=camera 且 OBS 进程在 → 常开记录子进程；加「连续 N 分钟没认出任何游戏画面就自动停」兜底 | 最简单、最稳、不碰设备 | OBS 开着但没玩时，记录子进程常驻约 200MB（偏离"空闲<100MB"） |
| 2 | 接 **OBS WebSocket**（OBS 28+ 自带，默认 4455）查 `GetVirtualCamStatus`，虚拟摄像头开/关即开/停记录 | 最准、最省资源 | 要在 OBS 里开 WebSocket、读配置里的密码，自己写一个零依赖的 websocket 客户端（代码较多） |
| 3 | 先只支持 PC（`r5apex.exe` 触发），主机仍用投影 | 改动最小、零风险 | 主机玩家享受不到，等于没解决"采集卡不上前台"那一半 |

我建议：**先按选项 1 做源码版原型**，跑通后再考虑要不要升级成选项 2。

### B. PC 场景「不前台化」是否真成立？（需要你真机试一次）
走虚拟摄像头时，OBS 得持续捕获游戏。Apex 被切到后台 / 窗口化时，OBS 的 Game Capture 还出不出画面，取决于游戏是否继续渲染。请在真机上把 Apex 窗口化并切后台，看 **OBS 虚拟摄像头预览里的画面还在不在动**。
- 若还在动 → PC 的「不前台化」成立；
- 若变黑/静止 → PC 只能用「无边框窗口、保持可见」，那"不前台化"要打折扣。

### C. 打包后 DirectShow 还能不能抓？（必须验）
本机源码形态已验证 DSHOW 成功。但 `release.py` 打包时会删掉 `opencv_videoio_ffmpeg*.dll`（29MB）；DirectShow 后端不依赖它，理论上没问题，但**必须拿打包后的 `Evo.exe` 再抓一次虚拟摄像头确认**。不行就二选一：保留该 DLL / 换实现。需要你同意"先做实验版本，打包后再验"。

### D. 多个程序同时用虚拟摄像头会不会打架？
OBS 虚拟摄像头被别的程序（Zoom / Teams / 浏览器 / Discord）同时占用时能不能共存，要在目标机器上确认。如果只能单消费者，Evo 记录时别的会议软件就用不了，设置页要给提示。

### E. 文案 / 范围
- 设置页这一项叫什么？（建议：「OBS 虚拟摄像头（主机 / 不希望游戏上前台）」）
- 要不要顺便支持「直接选采集卡（不经过 OBS）」？`devices.py` 已经铺好路，但那是另一条功能，本次建议**先不做**。

---

## 五、落地步骤（建议顺序）

1. 修 `devices.py` 枚举 bug，加 `find_obs()`；写 `frame.py` 的 `DirectShowCamera`。
2. 改注入点 `obs/capture.py`（包 `capture.open_camera`）、`settings.py` 加 `camera` 源。
3. 设置页加第三项 + 设备/状态显示；源码运行形态跑通：能选、能抓帧、能识别总结页。
4. 用真实链路（OBS 采集卡→vcam 主机 / OBS 抓游戏→vcam PC）各实测一局。
5. 打包，用打包版 `Evo.exe` 再验 DSHOW 抓帧；跑 `tests/run_all.py` 与 `captures/mode_probe/` 下的注入验证脚本。
6. 按决策 A 收敛触发逻辑、托盘/状态文案，补一条进 `claude/todo.md` 与 `claude/progress.md`。

## 六、风险与降级
- 任何注入失败 → 静默降级回整屏（沿用现有模式），Evo 不崩。
- 设备打不开 / 被占用 / 中途消失 → 按设置暂停或退回整屏（复用 `pause_when_closed` 思路）。
- 原则 1 不变：读虚拟摄像头仍是**只读取视频流**，不注入游戏、不模拟输入。
- 不碰上游文件；全部改动在 `paimiao_cloud/`。

## 七、本次不做
- 不直接读采集卡（只读 OBS 虚拟摄像头）。
- 不改 OBS 设置、不要求用户改 OBS 输出分辨率。
- 不做 PC 后台渲染的保证（取决于游戏本身，需真机 B 项确认）。

## 八、如果以后要接 OBS WebSocket（让触发更精确）

现在触发是「OBS 进程在就常开记录」（选项1）。缺点是 OBS 开着没玩时记录子进程常驻、虚拟摄像头没启动时读不到画面。接 WebSocket 就能按「虚拟摄像头开/关」精确触发。要做的事：

1. **OBS 端**：OBS 28+ 自带 obs-websocket。工具 → WebSocket Server Settings → 启用（默认端口 4455，可设密码）。
2. **连接参数**：端口和密码在 `%APPDATA%\obs-studio\plugin_config\obs-websocket\config.json`（明文，可读；也可让用户手填）。地址固定 `127.0.0.1`。
3. **客户端**：Python 标准库没有 websocket，两条路——
   - 自己写（约 150–200 行）：socket → HTTP Upgrade 握手（base64 随机 key + 校验 Sec-WebSocket-Accept）→ 帧解析（掩码/分片）→ UTF-8 JSON。零新依赖。
   - 或加 websocket-client / websockets 库（违反"能不加就不加"）。
   建议自己写最小实现（只连本机、只收发小 JSON，不搞 TLS/代理）。
4. **认证**：obs-websocket 5.x 先收 `Hello`（含 authentication.challenge+salt），再发 `Identify`（把 `base64(sha256(密码+salt))` 再和 challenge 做 sha256 后 base64），收到 `Identified` 才算连上。
5. **请求**：`GetVirtualCamStatus` → 读 `outputActive` bool；`VirtualcamStateChanged` 事件做实时触发。**Evo 只读状态，不替用户 Start/Stop 虚拟摄像头。**
6. **接入点**：`cloud/obs/trigger.py` 的 camera 分支把 `obs_running()` 换成 `websocket_vcam_active()`（带缓存 + 断线自动重连 + 超时回退到 OBS 进程判断）。
7. **降级**：连不上 / 没开 websocket / 密码变了 → 退回「OBS 进程判断」，不影响记录。
8. **安全**：只连 `127.0.0.1`、不打印密码；读 config 只在本机。
9. **代价**：多一个本地 socket 线程 + 重连逻辑，约 200–300 行 + 单测。

结论：现在先不接，等选项1跑一段看够不够用；不够再上。接入点已经留好（camera 分支一行替换）。
