"""大厅模式识别（2026-10-24 用户要求）：在准备界面认出「这一局打的是什么模式、是不是排位」。

上游现在的做法（`code/match_start.py`）是**等开局画面**才读模式：左上角横幅里
「排位赛 - 奥林匹斯」这样一行，横杠前面就是模式。问题是开局画面只闪几秒，
闪的时候正好在看手机／被击杀回放挡住，这一局的模式就永远读不到了。

这个模块提前一步：**大厅点了准备（正在匹配）时就读左下角那张模式卡片**。
只做被动截屏后的识别，不碰游戏、不读内存，和上游的其它识别一样。

四个文件：
    rules.py        识别规则（坐标、缩放、字样、颜色）—— 全部是数据，方便改
    recognizer.py   从一张截图里读「模式色条 + 匹配状态」
    screen.py       继承上游 ScreenDetector，顺手读卡片
    match_mode.py   把读到的模式补给这一局，最终写进 matches.mode
    map_lookup.py   查地图轮换接口（某个时刻是哪张图）
    map_guess.py    没读到地图时按开局时刻推一张，顺便补 started_at
    inject.py       用注入把上面这些接进上游（screens.py / live_collect.py）

面板上**没有单独的页面**（2026-10-24 用户决定去掉 /cloud/mode）：
模式已经自动写进 matches.mode，在战绩页就能看到。

为什么要单独一套坐标、不复用 config/screens.json：
    增量注入的约束（AGENT_RULES.md）不允许改 config/，所以坐标写在本文件夹里。
    上游改了界面时，这里要跟着改 —— 只改 rules.py 一处。
"""
from __future__ import annotations

# 规则版本：改 rules.py 里的坐标/字样时一并改它，日志里能看出是第几版规则在跑
VERSION = "2026-10-24e"
