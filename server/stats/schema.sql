-- Evo 匿名统计的数据库（Cloudflare D1，名字 evo-stats）。worker.js 往里写，tools/stats_report.py 读。
-- 只有用户同意后发来的数据；没有 IP 地址、游戏名、ID。id 是 Evo 里随机生成的匿名编号。
-- 建表：wrangler d1 execute evo-stats --remote --file schema.sql（已经有的表不动）。
-- 给已有的表加列不能写在这里（CREATE TABLE IF NOT EXISTS 不会加列）：另写一个只运行一次的文件（ALTER TABLE … ADD COLUMN …），运行完就删掉。2026-10-24 加队友数据的 8 列时用的 add_mate_stats.sql 可以在 git 历史里参考

-- 日活：每个编号每天一行（那天 Evo 开着、联网发过一次）
CREATE TABLE IF NOT EXISTS pings (
  id TEXT NOT NULL,
  day TEXT NOT NULL,  -- 用户电脑上的日期
  version TEXT,  -- Evo 版本号
  screen TEXT,  -- 屏幕分辨率，如 2560x1440
  game_res TEXT,  -- Apex 视频设置里的分辨率
  game_mode TEXT,  -- fullscreen / borderless / windowed
  aspect TEXT,  -- Evo 设置页选的游戏画面比例：auto / 16:9 / 16:10 / 4:3
  received_at TEXT NOT NULL,  -- 服务器收到的时间（UTC）
  PRIMARY KEY (id, day)
);

-- 每局一行。k 是 Evo 算出来的对局编号（同一局再发就覆盖）；没读出来的项为空
CREATE TABLE IF NOT EXISTS matches (
  id TEXT NOT NULL,
  k TEXT NOT NULL,
  day TEXT,  -- 这一局结束的日期（用户电脑上的）
  version TEXT,  -- 记这一局的 Evo 版本号
  mode TEXT,  -- ranked / pubs / other
  map TEXT,
  legend TEXT,  -- 自己的传奇（中文名）
  mate1 TEXT,  -- 两名队友的传奇
  mate2 TEXT,
  placement INTEGER,
  kills INTEGER,
  assists INTEGER,
  knockdowns INTEGER,
  damage INTEGER,
  rp_delta INTEGER,
  tier TEXT,  -- 段位，只有大段，如 钻石
  rp_source TEXT,  -- RP 从哪个画面来：经验值页 / 对局中 / 大厅校准
  review INTEGER,  -- 1 = 这一局在 Evo 里标了待确认
  edited TEXT,  -- 用户在战绩页改过哪几项（列名，逗号分隔）
  received_at TEXT NOT NULL,
  -- 两名队友这一局的数据（总结页上读的；0.3.8 之后的 Evo 才发，2026-10-24 加的列）
  mate1_kills INTEGER,
  mate1_assists INTEGER,
  mate1_knockdowns INTEGER,
  mate1_damage INTEGER,
  mate2_kills INTEGER,
  mate2_assists INTEGER,
  mate2_knockdowns INTEGER,
  mate2_damage INTEGER,
  -- 自己每把武器拿在手上几秒，JSON 文字，如 {"car":265,"nemesis":574}（只算这一局第一次拿到武器 60 秒以后的；
  -- 0.4.1 之后的 Evo 才发，2026-10-28 加的列）；没记下武器是空的
  weapons TEXT,
  PRIMARY KEY (id, k)
);

-- 每局对局中 Evo 自己的占用（2026-11-15 用户决定，这以后的 Evo 才发）：每局从认出自己的界面到结束，Evo 全部进程
-- 平均占一个核的百分之几、内存（专用工作集）的平均和最高。k 是 Evo 算出来的编号（同一局再发就覆盖）
CREATE TABLE IF NOT EXISTS usage (
  id TEXT NOT NULL,
  k TEXT NOT NULL,
  day TEXT,  -- 用户电脑上的日期
  version TEXT,  -- 记这一局的 Evo 版本号
  minutes REAL,  -- 这一段多少分钟
  cpu_core REAL,  -- Evo 平均占一个核的百分之几（和电脑有几个核无关）
  memory INTEGER,  -- 内存平均（MB）
  memory_peak INTEGER,  -- 内存最高（MB）
  received_at TEXT NOT NULL,
  PRIMARY KEY (id, k)
);

CREATE INDEX IF NOT EXISTS pings_day ON pings (day);
CREATE INDEX IF NOT EXISTS matches_day ON matches (day);
CREATE INDEX IF NOT EXISTS matches_received ON matches (received_at);  -- 汇总前看有没有新数据（只读一行）
CREATE INDEX IF NOT EXISTS usage_day ON usage (day);  -- 后台「数据」页按天算占用（server/team/statsroll.js，2026-11-19 加的）

-- 全体玩家的汇总（Evo 数据页"全体玩家"下载的就是 v1 那一行的 body；worker.js 每天出一次）。
-- 另外几行也放在这里：maps_v1 地图轮换，v1_days 汇总增量算到哪了，v1_dirty /v1/delete 删过数据、要重建的日子（见 worker.js）
CREATE TABLE IF NOT EXISTS summary (
  name TEXT PRIMARY KEY,  -- 汇总的版本（v1）；格式改了换个名字，老版本的 Evo 照样读老的
  body TEXT NOT NULL,  -- JSON，格式见 worker.js 的"全体玩家的汇总"
  made_at TEXT NOT NULL,  -- 算的时间（UTC）
  data_at TEXT  -- v1：算的时候读到的最新一局的收到时间
);

-- 全体玩家汇总的中间结果（2026-11-19 改成按天增量算，见 worker.js 的"全体玩家的汇总"）。都由 worker.js 从 matches 算出来。
-- 要从头重建：把 summary 表里 v1_days 那一行删掉，下一个小时的定时任务清空这两张表、从头建（只清空这两张表不行）
-- 每名玩家每天一行：那天他每一局里汇总用得到的几项（JSON {对局编号 k: [...]}，见 worker.js 的 snapshot）
CREATE TABLE IF NOT EXISTS summary_pairs (
  day TEXT NOT NULL,  -- 对局的日期（没有日期的局是 ''）
  id TEXT NOT NULL,
  games TEXT NOT NULL,
  PRIMARY KEY (day, id)
);
-- 每天一行：那天全体的部分和（JSON，见 worker.js 的 pairParts）
CREATE TABLE IF NOT EXISTS summary_days (
  day TEXT PRIMARY KEY,
  body TEXT NOT NULL
);
