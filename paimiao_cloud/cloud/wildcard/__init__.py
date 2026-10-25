"""外卡模式：纯注入补丁。每个文件只管一件事。

    rules.py    两行标签的坐标（纯数据，调坐标只改这一个文件）
    detect.py   在总结页上读标签、认外卡；包住 SummaryWatcher.update / _record / summary_to_match
    page.py     战绩页：模式一栏显示「外卡」，两行标签换成游戏里的叫法，加「外卡」筛选
    inject.py   等 live_collect 导入的钩子（和 lobby_mode/inject.py 同一套写法）
"""
