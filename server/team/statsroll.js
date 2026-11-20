// 后台「数据」页的数（2026-11-19 用户要的：后台显示日活趋势、对局趋势）。来源是匿名统计的数据库 evo-stats
// （Cloudflare D1，统计服务器 server/stats/ 往里写；这里绑定成 STATS，只读：代码里只有 SELECT）。
//
// 额度：D1 免费版整个账号一天只能读 500 万行，2026-11-08 超过一次（超了当天匿名统计、全体玩家、地图轮换全断）。
// 所以页面打开时一行都不读数据库：定时任务每 3 小时增量算一次，存进 evo-team 的 stats/daily.json，页面只读这个文件。
// 增量：每次只算"变了的日子"——最近 3 天（今天的数还在涨、玩家电脑的日期和 UTC 差几个小时）每次都算；上次以后又收到对局的
// 更早的日子（玩家修正后会重发旧局，用 matches.received_at 的索引找，只读新收到的那几行）先记下来，每个 UTC 日子只补算一次
// （2026-11-20 改的：原来每 3 小时都把这些日子整天重算一遍，加上找它们的那句没用上索引、每次扫整张表，一天读约 230 万行，
// 改完约 30 万行）。每一天的数都按那一天的行查（pings、matches 有 day 的索引；usage 的 day 索引是 11-19 加的），
// 读的行数≈那几天的数据量，不随总量变大。只有日活、没有对局的旧日子（没联网时攒着、过几天补发的日活）不会再算：差几个人，
// 不值得每次多读一天。
// 第一次（还没算过）、VERSION 改了时从最早的一天起补算，每次最多补 BACKFILL 天，几次定时任务就补完（补完以前照样显示原来的数）。
// 段位分布不在这里算：照抄统计服务器"全体玩家的汇总"里的那份（summary 表 v1 那一行的 tiers，只读一行；见 server/stats/worker.js）。
// 只存汇总的数，不存任何一个匿名编号（隐私说明第四节：不公开任何一个编号的数据）。
//
// stats/daily.json：{"version": VERSION, "days": {日期: 那一天的数}, "first": 最早的日期, "backfill": 补算到哪天了（补完是 null）,
//   "watermark": 上次看到的最新一局的收到时间, "pending": [又收到对局、还没补算的旧日子], "pendingDay": 上次补算旧日子的 UTC 日期,
//   "tiers": 段位分布（{madeAt, season, seasonFrom, ranges: {all|7d|season: {players, counts: {段位: 人数}}}}，没有是 null）,
//   "computedAt": 毫秒}
// 那一天的数：{dau, fresh（第一次出现的编号）, versions: {版本: 人数}, games, players（打了对局的人数）, ranked, pubs,
//   review（待确认的局）, edited（玩家改过的局）, tierRead: {版本: [排位局, 其中读到了段位的]}（11-20 加的）,
//   usage: {版本: {n, cpu, mem, peak}}（cpu、mem 是合计，页面上再除）}

import { readDoc, updateDoc } from "./store.js";

export const DAILY = "stats/daily.json";
export const VERSION = 2;  // 那一天的数加了项、算法改了就加 1：部署以后从最早的一天重新补算（2：加了 tierRead）
const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
export const EVERY = 3 * HOUR;
export const BACKFILL = 10;   // 补算时一次最多几天
export const MAX_DAYS = 30;   // 一次最多算几天（补算 + 变了的），剩下的下次再算
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
export const START = "2026-10-20";  // 匿名统计 0.3.7 起才有（2026-10-22 上线）；电脑时钟错了的更早的日子不算

const emptyDaily = () => ({ version: VERSION, days: {}, first: null, backfill: null, watermark: "", pending: [], pendingDay: "", tiers: null,
                            computedAt: 0 });

export async function daily(bucket) {
  return (await readDoc(bucket, DAILY, emptyDaily)).data;
}

export const isoDay = (ms) => new Date(ms).toISOString().slice(0, 10);
export const nextDay = (day) => isoDay(Date.parse(`${day}T00:00:00Z`) + DAY);

// 最近 3 天：玩家电脑上的日期比 UTC 早的晚的都有（中国是 UTC+8，美洲是 UTC-5～-8），今天的数还在涨。
// 一天 D 一直算到 UTC 的 D+2 零点，那时候哪个时区的 D 都过完了
export function recentDays(now) {
  return [-1, 0, 1].map((n) => isoDay(now + n * DAY));
}

// 一天的数：一次批量查询（一个来回）。每一句都只读这一天的行；对局只扫一遍（按编号、版本分组拿回来，在这里加：
// 打了对局的人数 = 有几个编号，编号用完就扔，不存）。日活 = 各版本人数加起来（pings 每个编号每天只有一行）
export async function computeDay(db, day) {
  const [fresh, versions, games, usage] = await db.batch([
    db.prepare("SELECT COUNT(*) AS n FROM pings p WHERE p.day = ?1 AND NOT EXISTS (SELECT 1 FROM pings q WHERE q.id = p.id AND q.day < ?1)").bind(day),
    db.prepare("SELECT version, COUNT(*) AS n FROM pings WHERE day = ?1 GROUP BY version").bind(day),
    db.prepare(`SELECT id, version, COUNT(*) AS games, SUM(mode = 'ranked') AS ranked, SUM(mode = 'pubs') AS pubs, SUM(review = 1) AS review,
      SUM(edited IS NOT NULL AND edited != '') AS edited, SUM(mode = 'ranked' AND tier IS NOT NULL AND tier != '') AS tier
      FROM matches WHERE day = ?1 GROUP BY id, version`).bind(day),
    db.prepare(`SELECT version, COUNT(*) AS n, SUM(cpu_core) AS cpu, SUM(memory) AS mem, MAX(memory_peak) AS peak
      FROM usage WHERE day = ?1 AND cpu_core IS NOT NULL GROUP BY version`).bind(day),
  ]);
  const out = { dau: 0, fresh: fresh.results[0]?.n ?? 0, versions: {}, games: 0, players: 0, ranked: 0, pubs: 0, review: 0, edited: 0, tierRead: {} };
  for (const r of versions.results) {
    out.versions[r.version || "?"] = r.n;
    out.dau += r.n;
  }
  const ids = new Set();
  for (const r of games.results) {
    ids.add(r.id);
    for (const key of ["games", "ranked", "pubs", "review", "edited"]) out[key] += r[key] ?? 0;
    if (r.ranked) {
      const t = (out.tierRead[r.version || "?"] ??= [0, 0]);
      t[0] += r.ranked;
      t[1] += r.tier ?? 0;
    }
  }
  out.players = ids.size;
  out.usage = Object.fromEntries(usage.results.map((r) => [r.version || "?", { n: r.n, cpu: r.cpu ?? 0, mem: r.mem ?? 0, peak: r.peak ?? 0 }]));
  return out;
}

// 段位分布：统计服务器的汇总里那份（每个编号最近一局排位的段位，同 Evo 数据页"全体玩家"）。json_extract 在数据库那边取，
// 不把整份汇总（十几万字）拿回来。读不到、格式不对 → null（页面上照旧显示上一次的）
function tiersFrom(row) {
  if (!row?.tiers) return null;
  try {
    const ranges = JSON.parse(row.tiers);
    const ok = (r) => r && Number.isFinite(r.players) && r.counts && typeof r.counts === "object";
    if (!ok(ranges.season) || !ok(ranges.all)) return null;
    return { madeAt: row.made_at, season: row.season ?? null, seasonFrom: row.season_from ?? null, ranges };
  } catch {
    return null;
  }
}

// 定时任务叫它（worker.js 的 scheduled，每 3 分钟一次；离上次不到 EVERY 就什么都不做）。force：测试、手动重算用
export async function rollup(env, now, { force = false } = {}) {
  const db = env.STATS;
  if (!db) return "no-db";
  const current = await daily(env.DATA);
  const same = current.version === VERSION;  // 版本不对（算法改了）：从最早的一天重新补算
  // 补算没完的时候每次定时任务（3 分钟）都接着补；补完了 3 小时一次
  if (!force && same && current.first && !current.backfill && now - current.computedAt < EVERY) return "wait";
  const head = await db.batch([
    db.prepare("SELECT MIN(day) AS d FROM pings"),
    db.prepare("SELECT MAX(received_at) AS r FROM matches"),
    db.prepare(`SELECT made_at, json_extract(body, '$.tiers') AS tiers, json_extract(body, '$.season') AS season,
      json_extract(body, '$.season_from') AS season_from FROM summary WHERE name = 'v1'`),
  ]);
  const earliest = head[0].results[0]?.d ?? null;
  const first = earliest && earliest < START ? START : earliest;
  const watermark = head[1].results[0]?.r ?? "";
  const tiers = tiersFrom(head[2].results[0]);
  if (!first || !DAY_RE.test(first)) {
    await updateDoc(env.DATA, DAILY, emptyDaily, (d) => { d.computedAt = now; });
    return "empty";
  }
  // 上次以后又收到对局的日子（玩家修正后重发的旧局）：只读新收到的行。INDEXED BY 一定要写：不写时 SQLite 为了 DISTINCT day
  // 挑了 day 的索引、一行一行看收到时间，等于每次扫整张表（2026-11-20 查出来的，一次约 11 万行）
  const changed = current.watermark && same
    ? (await db.prepare("SELECT DISTINCT day FROM matches INDEXED BY matches_received WHERE received_at > ?1").bind(current.watermark).all())
      .results.map((r) => r.day)
    : [];
  const recent = recentDays(now);
  // 补算：从最早的一天起，补到最近几天之前
  let backfill = current.first && same ? current.backfill : first;
  const todo = [];
  while (backfill && backfill < recent[0] && todo.length < BACKFILL) {
    todo.push(backfill);
    backfill = nextDay(backfill);
  }
  if (backfill && backfill >= recent[0]) backfill = null;
  for (const day of recent) if (day >= first) todo.push(day);
  // 又收到对局的旧日子：先记着，每个 UTC 日子补算一次（补算还没走到的日子不用记，走到了自然会算）
  const pending = new Set(same ? current.pending ?? [] : []);
  for (const day of changed) if (DAY_RE.test(day ?? "") && day >= first && !recent.includes(day)) pending.add(day);
  for (const day of [...pending]) if (todo.includes(day) || (backfill && day >= backfill)) pending.delete(day);
  const utcToday = isoDay(now);
  const catchUp = current.pendingDay !== utcToday;
  if (catchUp) todo.push(...[...pending].sort());
  const days = todo.slice(0, MAX_DAYS);
  const results = {};
  for (const day of days) results[day] = await computeDay(db, day);
  for (const day of days) pending.delete(day);
  await updateDoc(env.DATA, DAILY, emptyDaily, (d) => {
    Object.assign(d.days, results);
    d.version = VERSION;
    d.first = first;
    d.backfill = backfill;
    d.watermark = watermark;  // 又收到对局的日子都记进 pending 了，水位可以往前挪
    d.pending = [...pending].sort();
    if (catchUp && !pending.size) d.pendingDay = utcToday;  // 没补完（一次最多 MAX_DAYS 天）：下次接着补
    if (tiers) d.tiers = tiers;
    d.computedAt = now;
  });
  return `${days.length} 天`;
}

// ---------- 页面用：一段日子的数 ----------

// 从 from 到 to（含）每一天一项，没有数据的天是 null（页面上画成断开，不当成 0）
export function series(doc, from, to) {
  const out = [];
  for (let day = from; day <= to; day = nextDay(day)) out.push({ day, ...(doc.days[day] ?? { missing: true }) });
  return out;
}

// 一段日子里各版本的占用（加权平均）：{版本: {n, cpu, mem, peak}}
export function usageByVersion(rows) {
  const out = {};
  for (const r of rows) {
    for (const [v, u] of Object.entries(r.usage ?? {})) {
      const o = (out[v] ??= { n: 0, cpu: 0, mem: 0, peak: 0 });
      o.n += u.n;
      o.cpu += u.cpu;
      o.mem += u.mem;
      o.peak = Math.max(o.peak, u.peak);
    }
  }
  return out;
}
