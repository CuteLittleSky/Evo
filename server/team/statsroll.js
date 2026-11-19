// 后台「数据」页的数（2026-11-19 用户要的：后台显示日活趋势、对局趋势）。来源是匿名统计的数据库 evo-stats
// （Cloudflare D1，统计服务器 server/stats/ 往里写；这里绑定成 STATS，只读：代码里只有 SELECT）。
//
// 额度：D1 免费版整个账号一天只能读 500 万行，2026-11-08 超过一次（超了当天匿名统计、全体玩家、地图轮换全断）。
// 所以页面打开时一行都不读数据库：定时任务每 3 小时增量算一次，存进 evo-team 的 stats/daily.json，页面只读这个文件。
// 增量：每次只算"变了的日子"——最近几天（今天的数还在涨、玩家电脑的日期和 UTC 差几个小时）+ 上次以后又收到对局的日子
// （玩家修正后会重发旧局，用 matches.received_at 的索引找，只读新收到的那几行）。每一天的数都按那一天的行查
// （pings、matches 有 day 的索引；usage 的 day 索引是 11-19 加的），读的行数≈那几天的数据量，不随总量变大。
// 第一次（还没算过）从最早的一天起补算，每次最多补 BACKFILL 天，几次定时任务就补完。
// 只存汇总的数，不存任何一个匿名编号（隐私说明第四节：不公开任何一个编号的数据）。
//
// stats/daily.json：{"version": 1, "days": {日期: 那一天的数}, "first": 最早的日期, "backfill": 补算到哪天了（补完是 null）,
//   "watermark": 上次看到的最新一局的收到时间, "computedAt": 毫秒}
// 那一天的数：{dau, fresh（第一次出现的编号）, versions: {版本: 人数}, games, players（打了对局的人数）, ranked, pubs,
//   review（待确认的局）, edited（玩家改过的局）, usage: {版本: {n, cpu, mem, peak}}（cpu、mem 是合计，页面上再除）}

import { readDoc, updateDoc } from "./store.js";

export const DAILY = "stats/daily.json";
const HOUR = 3600 * 1000;
const DAY = 24 * HOUR;
export const EVERY = 3 * HOUR;
export const BACKFILL = 10;   // 补算时一次最多几天
export const MAX_DAYS = 30;   // 一次最多算几天（补算 + 变了的），剩下的下次再算
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
export const START = "2026-10-20";  // 匿名统计 0.3.7 起才有（2026-10-22 上线）；电脑时钟错了的更早的日子不算

const emptyDaily = () => ({ version: 1, days: {}, first: null, backfill: null, watermark: "", computedAt: 0 });

export async function daily(bucket) {
  return (await readDoc(bucket, DAILY, emptyDaily)).data;
}

export const isoDay = (ms) => new Date(ms).toISOString().slice(0, 10);
export const nextDay = (day) => isoDay(Date.parse(`${day}T00:00:00Z`) + DAY);

// 最近几天：玩家电脑上的日期比 UTC 早的晚的都有（中国是 UTC+8），今天的数还在涨
export function recentDays(now) {
  return [-2, -1, 0, 1].map((n) => isoDay(now + n * DAY));
}

// 一天的数：一次批量查询（一个来回）。每一句都只读这一天的行
export async function computeDay(db, day) {
  const [dau, fresh, versions, games, usage] = await db.batch([
    db.prepare("SELECT COUNT(*) AS n FROM pings WHERE day = ?1").bind(day),
    db.prepare("SELECT COUNT(*) AS n FROM pings p WHERE p.day = ?1 AND NOT EXISTS (SELECT 1 FROM pings q WHERE q.id = p.id AND q.day < ?1)").bind(day),
    db.prepare("SELECT version, COUNT(*) AS n FROM pings WHERE day = ?1 GROUP BY version").bind(day),
    db.prepare(`SELECT COUNT(*) AS games, COUNT(DISTINCT id) AS players, SUM(mode = 'ranked') AS ranked, SUM(mode = 'pubs') AS pubs,
      SUM(review = 1) AS review, SUM(edited IS NOT NULL AND edited != '') AS edited FROM matches WHERE day = ?1`).bind(day),
    db.prepare(`SELECT version, COUNT(*) AS n, SUM(cpu_core) AS cpu, SUM(memory) AS mem, MAX(memory_peak) AS peak
      FROM usage WHERE day = ?1 AND cpu_core IS NOT NULL GROUP BY version`).bind(day),
  ]);
  const g = games.results[0] ?? {};
  return {
    dau: dau.results[0]?.n ?? 0,
    fresh: fresh.results[0]?.n ?? 0,
    versions: Object.fromEntries(versions.results.map((r) => [r.version || "?", r.n])),
    games: g.games ?? 0, players: g.players ?? 0, ranked: g.ranked ?? 0, pubs: g.pubs ?? 0, review: g.review ?? 0, edited: g.edited ?? 0,
    usage: Object.fromEntries(usage.results.map((r) => [r.version || "?", { n: r.n, cpu: r.cpu ?? 0, mem: r.mem ?? 0, peak: r.peak ?? 0 }])),
  };
}

// 定时任务叫它（worker.js 的 scheduled，每 3 分钟一次；离上次不到 EVERY 就什么都不做）。force：测试、手动重算用
export async function rollup(env, now, { force = false } = {}) {
  const db = env.STATS;
  if (!db) return "no-db";
  const current = await daily(env.DATA);
  // 补算没完的时候每次定时任务（3 分钟）都接着补；补完了 3 小时一次
  if (!force && current.first && !current.backfill && now - current.computedAt < EVERY) return "wait";
  const head = await db.batch([
    db.prepare("SELECT MIN(day) AS d FROM pings"),
    db.prepare("SELECT MAX(received_at) AS r FROM matches"),
  ]);
  const earliest = head[0].results[0]?.d ?? null;
  const first = earliest && earliest < START ? START : earliest;
  const watermark = head[1].results[0]?.r ?? "";
  if (!first || !DAY_RE.test(first)) {
    await updateDoc(env.DATA, DAILY, emptyDaily, (d) => { d.computedAt = now; });
    return "empty";
  }
  // 上次以后又收到对局的日子（玩家修正后重发的旧局）：received_at 有索引，只读新收到的行
  const changed = current.watermark
    ? (await db.prepare("SELECT DISTINCT day FROM matches WHERE received_at > ?1").bind(current.watermark).all()).results.map((r) => r.day)
    : [];
  const recent = recentDays(now);
  // 补算：从最早的一天起，补到最近几天之前
  let backfill = current.first ? current.backfill : first;
  const todo = [];
  while (backfill && backfill < recent[0] && todo.length < BACKFILL) {
    todo.push(backfill);
    backfill = nextDay(backfill);
  }
  if (backfill && backfill >= recent[0]) backfill = null;
  for (const day of [...recent, ...changed]) if (DAY_RE.test(day ?? "") && day >= first && !todo.includes(day)) todo.push(day);
  const days = todo.slice(0, MAX_DAYS);
  const results = {};
  for (const day of days) results[day] = await computeDay(db, day);
  const left = todo.slice(MAX_DAYS).filter((d) => !recent.includes(d));
  await updateDoc(env.DATA, DAILY, emptyDaily, (d) => {
    Object.assign(d.days, results);
    d.first = first;
    d.backfill = backfill;
    if (!left.length) d.watermark = watermark;  // 变了的日子没算完：水位不往前挪，下次还查得出来
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
