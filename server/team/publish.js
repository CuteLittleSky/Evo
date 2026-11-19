// 发布：把选手库和赛程生成 esports.json，写进放安装包的那个公开存储桶 evotracker（https://dl.evotracker.cn/），
// Evo 的赛事页读它（code/esports_data.py）。照 tools/esports.py 的做法（2026-11-17 第 2 步）：
// - 先发测试文件 esports_test.json（玩家的 Evo 不读它），核对没问题再发正式的 esports.json
// - 平台开关（某个平台出问题时改成"去 X 看"）照线上现在的原样带着，只有专门的开关才改它
//   （出事时不发版就能关掉一个平台，靠的只有它）
// - 写完马上读回来，和要写的一字不差才算发好
// 这个 Worker 能写整个公开存储桶（R2 的绑定不能只给一个文件），所以只写下面 TARGETS 里的两个文件名，
// 和队徽（logos.js，只写 esports/logos/<内容哈希>.png|webp），别的一律不碰。

import { Refuse } from "./store.js";
import { PLATFORMS, RECENT_DAYS, STATUS_WORDS, beijingText, build, clean, parseIso, platformName, status } from "./esports.js";
import { events, players, published, recentLog, recordPublish, writeLog } from "./data.js";
import { teamLogos } from "./logos.js";

const DAY = 24 * 3600 * 1000;

export const TARGETS = {
  test: { file: "esports_test.json", name: "测试文件" },
  official: { file: "esports.json", name: "正式文件" },
};
export const PUBLIC_URL = "https://dl.evotracker.cn";

// 线上现在的文件：{raw: 原样的内容, data: Evo 读到的样子}；没有、格式不对 data 是 null
export async function liveFile(publicBucket, target) {
  const object = await publicBucket.get(TARGETS[target].file);
  if (!object) return { raw: null, data: null };
  try {
    const raw = JSON.parse(await object.text());
    return { raw, data: clean(raw) };
  } catch {
    return { raw: null, data: null };
  }
}

// 发布页要的东西：按现在的选手库、赛程生成的（含检查结果）和线上的两个文件
export async function preview(env, now) {
  const [test, official] = await Promise.all([liveFile(env.PUBLIC, "test"), liveFile(env.PUBLIC, "official")]);
  const [playerItems, eventItems, logos] = await Promise.all([players(env.DATA), events(env.DATA), teamLogos(env.DATA)]);
  return { live: { test, official }, built: { test: build(playerItems, eventItems, test.data?.platforms, now, logos),
                                               official: build(playerItems, eventItems, official.data?.platforms, now, logos) } };
}

async function write(publicBucket, target, data) {
  const key = TARGETS[target].file;
  const body = JSON.stringify(data, null, 1);
  await publicBucket.put(key, body, { httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-cache" } });
  const back = await publicBucket.get(key);
  if (!back || (await back.text()) !== body) throw new Refuse(`${key} 写进去以后读回来不一样，请再发一次；还不行告诉 Claude`);
  return body.length;
}

// 几个数：几场赛事、几支队、几位选手
export function counts(data) {
  return { events: data.events.length, teams: new Set(data.events.flatMap((e) => e.teams)).size, players: data.players.length };
}

export async function publish(env, by, target, now) {
  if (!Object.hasOwn(TARGETS, target)) throw new Refuse("不认识要发到哪里");
  const current = await liveFile(env.PUBLIC, target);
  const result = build(await players(env.DATA), await events(env.DATA), current.data?.platforms, now, await teamLogos(env.DATA));
  if (result.errors.length) throw new Refuse(`没有发布，有 ${result.errors.length} 处要改：\n${result.errors.join("\n")}`);
  const bytes = await write(env.PUBLIC, target, result.data);
  const c = counts(result.data);
  await recordPublish(env.DATA, target, { at: now, by: by.name, ...c, bytes });
  await writeLog(env.DATA, by, now, `发布${TARGETS[target].name}：${c.events} 场赛事、${c.teams} 支队、${c.players} 位选手`);
  return c;
}

// ---------- 有改动还没发布（2026-11-19 用户同意：页面顶上提醒）----------
// 按现在的选手库、赛程生成一份，和线上的正式文件比（不管 updated、平台开关）。线上那份先照 build 的规矩
// 去掉结束超过 7 天的赛事和用不到的人、队徽：只是日子过去了、旧赛事该下架了，不算"有改动"。
function comparable(data, now) {
  const evs = (data.events ?? []).filter((e) => !(parseIso(e.end) !== null && now - parseIso(e.end) > RECENT_DAYS * DAY));
  const used = new Set(evs.flatMap((e) => e.teams));
  return JSON.stringify({ players: (data.players ?? []).filter((p) => used.has(p.team)), events: evs,
                          teams: Object.fromEntries(Object.entries(data.teams ?? {}).filter(([t]) => used.has(t))) });
}

// 没有要发布的 → null；有 → {last: 上次发布的记录, who: 之后改过的人, errors: 现在还有几处要改}
export async function unpublished(env, now) {
  const [live, playerItems, eventItems, logos, last, log] = await Promise.all([liveFile(env.PUBLIC, "official"), players(env.DATA),
    events(env.DATA), teamLogos(env.DATA), published(env.DATA), recentLog(env.DATA)]);
  const built = build(playerItems, eventItems, live.data?.platforms, now, logos);
  if (live.raw ? comparable(built.data, now) === comparable(live.raw, now) : !built.data.events.length) return null;
  const since = last.official?.at ?? 0;
  const who = [...new Set(log.filter((x) => x.at > since && x.by !== "Liquipedia 同步" && !x.text.startsWith("发布")).map((x) => x.by))];
  return { last: last.official ?? null, who, errors: built.errors.length };
}

// 平台开关：只改线上文件里的 platforms 和 updated，别的原样不动（同 tools/esports.py --off / --on）
export async function setPlatform(env, by, target, key, on, now) {
  if (!Object.hasOwn(TARGETS, target)) throw new Refuse("不认识要改哪个文件");
  if (!Object.hasOwn(PLATFORMS, key)) throw new Refuse("不认识这个平台");
  const current = await liveFile(env.PUBLIC, target);
  if (!current.data) throw new Refuse(`线上还没有${TARGETS[target].name}：先发布一次赛事数据`);
  const data = { ...current.raw, platforms: { ...current.data.platforms, [key]: on },
                 updated: new Date(Math.floor(now / 1000) * 1000 + 8 * 3600 * 1000).toISOString().slice(0, 19) + "+08:00" };
  await write(env.PUBLIC, target, data);
  await writeLog(env.DATA, by, now, `${on ? "打开" : "关掉"}平台 ${PLATFORMS[key].name}（${TARGETS[target].name}）`
    + (on ? "" : "：Evo 不在窗口里播它，改成“去这个平台看”"));
}

// 一份文件写成几行字（发布页的预览、线上现在是什么）
export function describe(data, now) {
  if (!data) return null;
  const c = counts(data);
  const byPlatform = {};
  for (const p of data.players) byPlatform[p.platform] = (byPlatform[p.platform] ?? 0) + 1;
  return {
    logos: Object.keys(data.teams ?? {}).length,
    summary: `${c.events} 场赛事、${c.teams} 支队、${c.players} 位选手`
      + (data.players.length ? `（${Object.entries(byPlatform).map(([k, n]) => `${platformName(k)} ${n}`).join("、")}）` : ""),
    updated: data.updated ? beijingText(parseIso(data.updated)) : "",
    platformsOff: Object.entries(data.platforms).filter(([, on]) => !on).map(([k]) => PLATFORMS[k].name),
    events: data.events.map((e) => ({
      status: STATUS_WORDS[status(e, now)],
      when: `${beijingText(parseIso(e.start)).slice(5)}–${beijingText(parseIso(e.end)).slice(11)}`,
      text: `${e.type} ${e.name}${e.stage ? ` · ${e.stage}` : ""}`,
      teams: e.teams.length,
      players: data.players.filter((p) => e.teams.includes(p.team)).length,
    })),
  };
}
