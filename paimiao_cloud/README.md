# Evo 派喵云同步版

**Evo 的非官方修改分支**，由派喵小程序作者维护。

- 上游：[AugustHuangMX/Evo](https://github.com/AugustHuangMX/Evo)
- 本分支：[CuteLittleSky/Evo](https://github.com/CuteLittleSky/Evo)
- 依据 **MIT License** 分发，保留原版权声明：`Copyright (c) 2026 AugustHuangMX`（[LICENSE](https://github.com/CuteLittleSky/Evo/blob/main/LICENSE)）

与上游无隶属、无合作、无背书关系。本分支特有的问题请提到
[Issues](https://github.com/CuteLittleSky/Evo/issues)，不要提给上游。

---

## 本分支做了什么

1. **新增「派喵云同步」**（可选）：顶部导航第 4 项，把 Evo 识别出来的战绩同步到派喵小程序。
   电脑端出 6 位码，在派喵小程序「游戏战绩」里输入确认即可配对。
2. **更新检查改由派喵服务器下发**，安装包从 Cloudflare R2 下载。
3. **源代码链接、意见反馈入口**改成本分支的。
4. **启动时弹一次非官方分支说明**，设置页底部常驻「关于本分支 / 第三方许可」卡片。

---

## 数据协议

使用云同步前必须先同意《派喵云同步 · 数据传输指引》（软件内「派喵云同步」页可随时查看）。
指引正文改了会要求重新同意，重新同意之前不会上传任何数据。

**会上传**：对局时间、模式、地图、传奇、名次、击杀、助攻、击倒、伤害、参与度、
小队总击杀、救援与复活次数、RP 变化、存活时长、你的玩家名；段位名与段内排位点数；
队友的玩家名、传奇、击杀、助攻、击倒、伤害、存活时长、救援次数。

**绝不上传**：截图、数据库文件、账号密码、文件路径、硬件信息。

数据按你的微信 openid 归属，只有你自己的派喵小程序会话能读到。

**不用这个功能就什么都不传。** 没有同意、没有配对、或关掉自动同步，都不会启动上传。

---

## 许可证

本分支依据 MIT License 分发。上游版权声明：`Copyright (c) 2026 AugustHuangMX`。
完整许可证见 [LICENSE](https://github.com/CuteLittleSky/Evo/blob/main/LICENSE)，
程序内「设置 → 关于本分支 / 第三方许可」也可查看。
