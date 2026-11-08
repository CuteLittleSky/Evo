"""Evo 通知模块（应用内弹窗 + Windows 托盘通知）。

数据来自派喵后端 `/api/evo/notices`，字段全部当纯文本处理，动作/URL 走白名单（防 RCE）。
入口：`cloud.notify.inject.register(app, module)`，由 `cloud.bootstrap.attach()` 调用。
"""
