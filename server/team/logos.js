// 战队队徽（2026-11-18；用户定的：先用派圈助手那一批，没有的队再手动上传）。
//
// - 队标库：派圈那一批 851 张传在不公开的 evo-team 存储桶 logo-library/ 下（tools/team_logos.py 传的），
//   logo-library/index.json 是名单（Liquipedia 页面名、队名、文件名、宽高）。网页上按队名先猜一张，也能搜着挑
// - 选手库的每支队挑一张（或者上传一张）→ 复制到公开存储桶（dl.evotracker.cn）的 esports/logos/<文件名>，
//   文件名是图片内容 sha256 的前 16 位（换了图名字就变，所以可以一直缓存：Cache-Control immutable）
// - 哪支队用哪张记在 manual/teams.json：{"items": {队名: {"logo", "source", "by", "at"}}}；战队改名时跟着搬（data.js）
// - 发布时 esports.json 多一项 "teams": {队名: {"logo": 文件名}}，只放这次用到、有队徽的队（esports.js 的 build）
// 和 Evo 对好的规矩（「赛事页和看直播」窗口 11-18）：PNG 或 WebP，最长边不超过 256、不超过 60 KB，不要求正方形；
// Evo 里放在深色的小方块里显示，所以要用适合深色背景的那一版。不收 SVG（里面能带脚本）。

import { Refuse, readDoc, updateDoc } from "./store.js";
import { players, teamsOf, writeLog } from "./data.js";
import { squash } from "./esports.js";
import { nameKey } from "./lpsync.js";

export const LIBRARY_INDEX = "logo-library/index.json";
export const libraryKey = (file) => `logo-library/${file}`;
export const TEAMS = "manual/teams.json";
export const PUBLIC_PREFIX = "esports/logos/";
export const PUBLIC_LOGO_URL = "https://dl.evotracker.cn/esports/logos/";
export const LOGO_FILE = /^[a-z0-9]{8,40}\.(png|webp)$/;
export const MAX_BYTES = 60 * 1024;
export const MAX_SIDE = 256;

const emptyLibrary = () => ({ version: 1, items: [] });
const emptyTeams = () => ({ items: {} });

export async function library(bucket) {
  return (await readDoc(bucket, LIBRARY_INDEX, emptyLibrary)).data.items;
}

export async function teamLogos(bucket) {
  return (await readDoc(bucket, TEAMS, emptyTeams)).data.items;
}

// 图片的种类和宽高：只认 PNG 和 WebP（看文件开头的几个字节，不看文件名），别的都是 null
export function imageInfo(bytes) {
  const b = new Uint8Array(bytes);
  const u32be = (i) => ((b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]) >>> 0;
  const text = (i, n) => String.fromCharCode(...b.slice(i, i + n));
  if (b.length > 24 && b[0] === 0x89 && text(1, 3) === "PNG" && text(12, 4) === "IHDR") {
    return { type: "png", w: u32be(16), h: u32be(20) };
  }
  if (b.length > 30 && text(0, 4) === "RIFF" && text(8, 4) === "WEBP") {
    const chunk = text(12, 4);
    if (chunk === "VP8X") return { type: "webp", w: 1 + (b[24] | (b[25] << 8) | (b[26] << 16)), h: 1 + (b[27] | (b[28] << 8) | (b[29] << 16)) };
    if (chunk === "VP8L" && b[20] === 0x2f) {
      const bits = b[21] | (b[22] << 8) | (b[23] << 16) | (b[24] << 24);
      return { type: "webp", w: 1 + (bits & 0x3fff), h: 1 + ((bits >>> 14) & 0x3fff) };
    }
    if (chunk === "VP8 " && b[23] === 0x9d && b[24] === 0x01 && b[25] === 0x2a) {
      return { type: "webp", w: (b[26] | (b[27] << 8)) & 0x3fff, h: (b[28] | (b[29] << 8)) & 0x3fff };
    }
  }
  return null;
}

export async function sha16(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].slice(0, 8).map((x) => x.toString(16).padStart(2, "0")).join("");
}

function checkImage(bytes) {
  if (bytes.byteLength > MAX_BYTES) throw new Refuse(`图片太大了（${Math.ceil(bytes.byteLength / 1024)} KB，最多 60 KB）：换一张小一点的`);
  const info = imageInfo(bytes);
  if (!info) throw new Refuse("只收 PNG、WebP 图片（JPG 在网页上会自动转好；不收 SVG）");
  if (!info.w || !info.h || Math.max(info.w, info.h) > MAX_SIDE) throw new Refuse(`图片最长边要在 ${MAX_SIDE} 像素以内（这张是 ${info.w}×${info.h}）`);
  return info;
}

async function knownTeam(bucket, team) {
  const name = squash(team);
  if (!teamsOf(await players(bucket)).has(name)) throw new Refuse(`选手库里没有叫「${name}」的队`);
  return name;
}

// 放进公开存储桶：文件名就是内容哈希，已经有了就不再写
async function putPublic(env, file, bytes, type) {
  const key = PUBLIC_PREFIX + file;
  if (!LOGO_FILE.test(file)) throw new Refuse("文件名不对");
  if (await env.PUBLIC.head(key)) return;
  await env.PUBLIC.put(key, bytes, { httpMetadata: { contentType: `image/${type}`, cacheControl: "public, max-age=31536000, immutable" } });
  const back = await env.PUBLIC.head(key);
  if (!back || back.size !== bytes.byteLength) throw new Refuse("队徽放上去以后核对不上，请再试一次");
}

async function record(bucket, by, team, logo, source, now) {
  await updateDoc(bucket, TEAMS, emptyTeams, (data) => {
    data.items[team] = { logo, source, by: by.name, at: now };
  });
  await writeLog(bucket, by, now, `给「${team}」换了队徽（${source}）`);
}

// 从队标库里挑一张
export async function pickLogo(env, by, team, file, now) {
  const name = await knownTeam(env.DATA, team);
  const item = (await library(env.DATA)).find((x) => x.file === file);
  if (!item) throw new Refuse("队标库里没有这张图");
  const object = await env.DATA.get(libraryKey(file));
  if (!object) throw new Refuse("队标库里的这张图找不到了，请告诉管理员");
  const bytes = await object.arrayBuffer();
  const info = checkImage(bytes);
  await putPublic(env, file, bytes, info.type);
  await record(env.DATA, by, name, file, `派圈的队标库：${item.name}`, now);
}

// 自己上传一张（网页上已经缩好；这里再核对）
export async function uploadLogo(env, by, team, bytes, now) {
  const name = await knownTeam(env.DATA, team);
  if (!bytes?.byteLength) throw new Refuse("没有选图片");
  const info = checkImage(bytes);
  const file = `${await sha16(bytes)}.${info.type}`;
  await putPublic(env, file, bytes, info.type);
  await record(env.DATA, by, name, file, "上传的", now);
  return file;
}

export async function removeLogo(bucket, by, team, now) {
  const name = squash(team);
  await updateDoc(bucket, TEAMS, emptyTeams, (data) => {
    if (!data.items[name]) throw new Refuse("这支队没有队徽");
    delete data.items[name];
  });
  await writeLog(bucket, by, now, `去掉了「${name}」的队徽`);
}

// 按队名猜：去掉大小写、空格、符号后一样的最先，再是一边包含另一边的
export function suggest(team, items, limit = 6) {
  const key = nameKey(team);
  if (!key) return [];
  const exact = items.filter((x) => nameKey(x.name) === key || nameKey(x.page) === key);
  const near = items.filter((x) => !exact.includes(x) && key.length >= 2
    && (nameKey(x.name).includes(key) || (nameKey(x.name).length >= 3 && key.includes(nameKey(x.name)))));
  return [...exact, ...near].slice(0, limit);
}

// ---------- 一键配队徽（2026-11-19 用户同意：40 支队只有 7 支有队徽，每支都要点进去挑）----------

// 还没有队徽的队，各猜一张：名字一样（不管大小写、空格、符号）的算"准"，页面上先勾上；只是有点像的不勾
export function autoMatches(teamNames, logos, items) {
  const out = { matches: [], none: [], done: [] };
  for (const team of teamNames) {
    if (logos[team]?.logo) {
      out.done.push(team);
      continue;
    }
    const [best] = suggest(team, items, 1);
    if (!best) out.none.push(team);
    else out.matches.push({ team, item: best, exact: nameKey(best.name) === nameKey(team) || nameKey(best.page) === nameKey(team) });
  }
  return out;
}

// 一次配好勾上的几支：picks 是 [{team, file}]。队名、图都先核对，有一个不对整批不配；
// 打开页面以后别人已经给它配了的跳过（不盖掉）。R2 的读写算"内部请求"（免费版一次 1000 个），一支队 4 个，40 支也够
export async function pickMany(env, by, picks, now) {
  const [items, roster, current] = await Promise.all([library(env.DATA), players(env.DATA), teamLogos(env.DATA)]);
  const known = teamsOf(roster);
  const errors = [];
  const todo = [];
  for (const pick of picks) {
    const team = squash(pick.team);
    const item = items.find((x) => x.file === pick.file);
    if (!known.has(team)) errors.push(`选手库里没有叫「${team}」的队（可能刚改了名）`);
    else if (!item) errors.push(`「${team}」：队标库里没有这张图`);
    else if (!current[team]?.logo && !todo.some((x) => x.team === team)) todo.push({ team, item });
  }
  if (errors.length) throw new Refuse(`没有配，有 ${errors.length} 处不对：\n${errors.join("\n")}`);
  for (const { item } of todo) {
    const object = await env.DATA.get(libraryKey(item.file));
    if (!object) throw new Refuse(`队标库里的「${item.name}」找不到了，请告诉管理员`);
    const bytes = await object.arrayBuffer();
    await putPublic(env, item.file, bytes, checkImage(bytes).type);
  }
  const done = await updateDoc(env.DATA, TEAMS, emptyTeams, (data) => {
    const set = [];
    for (const { team, item } of todo) {
      if (data.items[team]?.logo) continue;
      data.items[team] = { logo: item.file, source: `派圈的队标库：${item.name}`, by: by.name, at: now };
      set.push(team);
    }
    return set;
  });
  if (done.length) await writeLog(env.DATA, by, now, `一键配队徽（${done.length} 支，派圈的队标库）：${done.join("、")}`);
  return { done: done.length, skipped: picks.length - done.length };
}

export function search(q, items, limit = 60) {
  const key = nameKey(q);
  return key ? items.filter((x) => nameKey(x.name).includes(key) || nameKey(x.page).includes(key)).slice(0, limit) : [];
}
