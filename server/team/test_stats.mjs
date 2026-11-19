// 后台「数据」页的测试（2026-11-19）。由 test.mjs 运行。
// 用 Node 自带的 SQLite 当 D1（建表用统计服务器的 server/stats/schema.sql），走一遍：第一次从最早的一天补算、
// 每天的数和直接查的一样、新玩家只算第一次出现、电脑时钟错了的老日子不算、离上次不到 3 小时什么都不查、
// 之后只算最近几天 + 又收到对局的日子（重发的旧局）、没绑数据库时不出错；页面（权限、几个数、图、表、范围、没算过）、首页、改名。

import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { hash, newToken, update } from "./auth.js";
import { DAILY, EVERY, rollup } from "./statsroll.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;

// D1 的样子：prepare().bind().all() / first()、batch()。queries 记下每一句按哪一天查的（看是不是只查了该查的日子）
class FakeD1 {
  constructor() {
    this.db = new DatabaseSync(":memory:");
    this.db.exec(readFileSync(new URL("../stats/schema.sql", import.meta.url), "utf-8"));
    this.queries = [];
  }
  prepare(sql) {
    const d1 = this;
    const make = (args) => ({
      bind: (...more) => make(more),
      async all() {
        d1.queries.push({ sql, args });
        return { results: d1.db.prepare(sql).all(...args).map((r) => ({ ...r })) };
      },
      async first() { return (await this.all()).results[0] ?? null; },
    });
    return make([]);
  }
  async batch(statements) { return Promise.all(statements.map((s) => s.all())); }
  run(sql, ...args) { this.db.prepare(sql).run(...args); }
  days() { return this.queries.filter((q) => /WHERE day = \?1/.test(q.sql) && /FROM pings WHERE/.test(q.sql) && !/NOT EXISTS/.test(q.sql)
    && /COUNT\(\*\) AS n FROM pings WHERE/.test(q.sql)).map((q) => q.args[0]); }
}

const stats = new FakeD1();
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket(), STATS: stats };
let now = Date.UTC(2026, 10, 19, 4, 0);  // 北京时间 11-19 12:00
const browser = () => makeBrowser(env, () => now);
const doc = () => env.DATA.json(DAILY, '{"days":{}}');

// ---------- 造数据：10-25 起每天几个人开着 Evo、打几局 ----------
const iso = (ms) => new Date(ms).toISOString();
const dayOf = (ms) => iso(ms).slice(0, 10);
const start = Date.UTC(2026, 9, 25);
let k = 0;
for (let t = start; t <= now; t += DAY) {
  const day = dayOf(t);
  const n = 3 + Math.floor((t - start) / DAY / 5);  // 人慢慢变多
  for (let i = 0; i < n; i++) {
    const id = `id${i}`;
    stats.run("INSERT INTO pings (id, day, version, received_at) VALUES (?, ?, ?, ?)", id, day, i % 2 ? "0.6.1" : "0.6.0", iso(t + HOUR));
    if (i % 2 === 0) {
      for (let g = 0; g < 2; g++) {
        stats.run(`INSERT INTO matches (id, k, day, version, mode, review, edited, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
          id, `k${++k}`, day, "0.6.0", g ? "ranked" : "pubs", g, i === 0 && g === 0 ? "kills" : null, iso(t + 2 * HOUR));
        stats.run("INSERT INTO usage (id, k, day, version, minutes, cpu_core, memory, memory_peak, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
          id, `u${k}`, day, "0.6.0", 20, 15, 200, 280, iso(t + 2 * HOUR));
      }
    }
  }
}
stats.run("INSERT INTO pings (id, day, version, received_at) VALUES ('wrongclock', '1999-01-01', '0.6.0', ?)", iso(now));  // 电脑时钟错了

// ---------- 没绑数据库 ----------
check("没绑统计数据库 → 什么都不做", await rollup({ DATA: new FakeBucket() }, now) === "no-db");

// ---------- 第一次：从最早的一天补算 ----------
let r = await rollup(env, now);
check("第一次：补算 10 天 + 最近 4 天，电脑时钟错了的 1999 年不算", r === "14 天" && doc().first === "2026-10-20" && doc().backfill === "2026-10-30");
let guard = 0;
while (doc().backfill && guard++ < 10) await rollup(env, now);  // 补算没完：每次定时任务都接着补，不等 3 小时
const d = doc();
check("补完了：从最早一天到今天每天都有", d.backfill === null && d.days["2026-10-25"] && d.days["2026-11-18"] && d.days["2026-11-19"]
  && Object.keys(d.days).length >= 26);
const direct = (sql, day) => stats.db.prepare(sql).get(day);
const nov10 = d.days["2026-11-10"];
check("一天的数和直接查的一样（日活、对局、人数、排位、匹配、待确认、改过）",
  nov10.dau === direct("SELECT COUNT(*) AS n FROM pings WHERE day = ?", "2026-11-10").n
  && nov10.games === direct("SELECT COUNT(*) AS n FROM matches WHERE day = ?", "2026-11-10").n
  && nov10.players === direct("SELECT COUNT(DISTINCT id) AS n FROM matches WHERE day = ?", "2026-11-10").n
  && nov10.ranked === nov10.games / 2 && nov10.pubs === nov10.games / 2 && nov10.review === nov10.games / 2 && nov10.edited === 1);
check("版本、占用按版本分开", nov10.versions["0.6.0"] + nov10.versions["0.6.1"] === nov10.dau && nov10.usage["0.6.0"].n === nov10.games
  && nov10.usage["0.6.0"].cpu === 15 * nov10.games && nov10.usage["0.6.0"].peak === 280);
const freshTotal = Object.values(d.days).reduce((s, x) => s + x.fresh, 0);
check("新玩家：每个人只在第一次出现那天算一次", d.days["2026-10-25"].fresh === 3 && d.days["2026-10-26"].fresh === 0
  && freshTotal === direct("SELECT COUNT(DISTINCT id) AS n FROM pings WHERE day >= ?", "2026-10-20").n);
check("只存汇总的数，不存任何一个匿名编号", !JSON.stringify(d).includes("id0") && !JSON.stringify(d).includes("wrongclock"));

// ---------- 之后：不到 3 小时不查；之后只算最近几天 + 又收到对局的日子 ----------
stats.queries = [];
now += HOUR;
check("离上次不到 3 小时 → 什么都不查", await rollup(env, now) === "wait" && stats.queries.length === 0);
now += EVERY;
stats.run(`INSERT INTO matches (id, k, day, version, mode, review, received_at) VALUES ('id0', 'resent', '2026-10-30', '0.6.1', 'ranked', 0, ?)`, iso(now));
const before = d.days["2026-10-30"].games;
r = await rollup(env, now);
const asked = [...new Set(stats.days())].sort();
check("之后只算最近 4 天 + 又收到对局的那一天（玩家修正后重发的旧局）", r === "5 天"
  && JSON.stringify(asked) === JSON.stringify(["2026-10-30", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]));
check("重发的旧局算进去了", doc().days["2026-10-30"].games === before + 1);
check("找重发的局用收到时间（有索引，只读新收到的行）", stats.queries.some((q) => /received_at > \?1/.test(q.sql)));

// ---------- 页面 ----------
async function join(name, role) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish: role === "admin", created: now,
                        createdBy: "命令行", expires: now + 7 * DAY });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}
const viewer = await join("看看", "viewer");
let page = await viewer.get("/stats");
check("只读的也能看数据（有后台权限的都能看）", page.status === 200 && page.text.includes("<h1>数据</h1>"));
check("几个数：昨天日活、近 7 天平均、昨天的对局、开过匿名统计的玩家", page.text.includes("昨天日活") && page.text.includes("近 7 天平均日活")
  && page.text.includes("昨天的对局") && page.text.includes(`>${freshTotal}</p>`));
check("几张图（电脑、手机各一张）、悬停的数、看数字的表", (page.text.match(/<svg class="chart"/g) ?? []).length === 8
  && page.text.includes('data-tip="2026-11-19（今天，还没算完）') && page.text.includes("<table class=\"nums\">") && page.text.includes("看数字"));
check("写着口径：只算开了匿名统计的玩家、今天还没算完", page.text.includes("开了匿名统计的玩家") && page.text.includes("今天还没算完"));
check("在用的版本、Evo 自己的占用", page.text.includes("在用的版本") && page.text.includes("0.6.1") && page.text.includes("Evo 自己的占用")
  && page.text.includes("15.0%"));
check("页面上没有任何一个匿名编号", !page.text.includes("id0") && !page.text.includes("wrongclock"));
check("默认近 30 天", page.text.includes('href="/stats" aria-current="page">近 30 天') && !page.text.includes('data-tip="2026-10-20'));
page = await viewer.get("/stats?range=all");
check("全部：从最早一天起", page.text.includes('aria-current="page">全部') && page.text.includes('data-tip="2026-10-20'));
page = await viewer.get("/stats?range=abc");
check("范围写错了 → 近 30 天", page.status === 200 && page.text.includes('href="/stats" aria-current="page">近 30 天'));
page = await viewer.get("/");
check("首页：数据那一格写着昨天日活；导航里有数据；网站叫 Evo 后台", page.text.includes(`昨天日活 ${doc().days["2026-11-18"].dau}`)
  && page.text.includes('href="/stats"') && page.text.includes("· Evo 后台</title>") && page.text.includes("<span>后台</span>"));
const empty = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
await empty.DATA.put("members.json", env.DATA.objects.get("members.json").body);
page = await makeBrowser(empty, () => now).go("GET", "/stats");
check("没登录 → 登录页（写着 Evo 后台）", page.status === 401 && page.text.includes("<h1>Evo 后台</h1>"));
