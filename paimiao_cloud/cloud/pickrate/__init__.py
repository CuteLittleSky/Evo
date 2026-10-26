"""Evo 派喵云同步 · 传奇选取率榜单（用户 2026-10-25 要求）。

## 它是什么

派喵官网/小程序上那份「传奇选取率」榜，搬进 Evo 面板里看 —— 数据来自**我们自己的**
ApexPacks 服务端，不是上游的匿名统计，也不需要同意任何数据上传。

    Evo 面板「榜单」页
        → GET {pair_server}/evo/pick-rate?tier=all     (Authorization: Bearer <配对令牌>)
        → server/api/evo/pick-rate.get.ts
        → 服务端缓存（5 分钟）→ Apex Legends Status 的选取率表
        → 返回 28 个传奇：pickRate / trend / avgRank / 战绩胜率 / 平均场杀

## 只读

这个包里**没有任何写操作**：不传数据、不上报、不改 `state.json`。
唯一的落盘是 `<数据目录>/cloud-sync/pickrate-<tier>.json` 这份本地缓存，
删掉就没了，下次打开重新取。

## 开关

不需要「同意匿名统计」。能不能看只看一件事：**有没有配对过**（配对时已经同意过
派喵自己的数据传输指引）。所以上游那个「同意匿名信息收集」的界面隐藏掉之后，
这条链路照常可用 —— 它本来就没走上游的 `telemetry.py`。

## 文件

    fetch.py   取数 + 两级缓存 + 失败退避（只用标准库 urllib）
    roles.py   职业中文名 / 颜色的本地兜底（接口一般都会给）
    page.py    Blueprint（/pickrate）+ 导航项 + 渲染期数据
    inject.py  把上面三样挂进上游（幂等，失败静默降级）

模板在 `paimiao_cloud/cloud/templates/pickrate_page.html`
（那个目录已经在 Jinja 的 ChoiceLoader 里，不需要额外注册）。
"""
