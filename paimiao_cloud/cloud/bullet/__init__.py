"""子弹时间（混合模式里的一种玩法）：纯注入补丁。每个文件只管一件事。

    rules.py    判据和坐标（纯数据，调坐标只改这一个文件）
    detect.py   在总结页上找「降级近战攻击」、认子弹时间；包住 SummaryWatcher.update / summary_to_match
    page.py     战绩页：模式一栏显示「子弹时间」，加这一项筛选
    inject.py   等 live_collect 导入的钩子（和 lobby_mode/inject.py 同一套写法）
"""
