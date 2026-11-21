// 选手库、赛程：网页上手动维护的那一份（2026-11-17 第 2 步）。
//
// 存在存储桶 evo-team 里：
//   manual/players.json  {"version": 1, "items": [{"id", "name", "team", "platform", "room", "liquipedia",
//                                                  "created", "createdBy", "updated", "updatedBy", "rev"}]}
//   manual/events.json   {"version": 1, "items": [{"id", "name", "type", "stage", "start", "end", "teams",
//                                                  "created", "createdBy", "updated", "updatedBy", "rev"}]}
//   log.json             {"items": [{"at", "by", "text"}]}：最近的改动（新的在前，留 300 条）
//   published.json       {"test": {...}, "official": {...}}：上次发布是谁、什么时候、发了多少
// 放在 manual/ 下：以后 Liquipedia（还有 ALS、overstat）抓来的放在各自的文件夹，和手动的分开存、谁也不改谁的，
// 页面上显示的是合并以后的结果（用户 11-17 同意的做法，见 claude/todo.md）。现在只有手动这一份。
// id 是网页自己用的随机编号（换平台、换房间也不变）；发布到 esports.json 时选手的 id 才是"平台-房间号"。
// rev：每改一次加 1。表单里带着打开时的 rev，保存时对不上（别人刚改过）就不保存，提示看一眼再改。

import { Refuse, randomHex, readDoc, updateDoc } from "./store.js";
import { NO_STREAM, PLATFORMS, cleanEventFields, cleanPlayerFields, eventLabel, normalRoom, platformName, squash } from "./esports.js";
import { pageFromInput } from "./liquipedia.js";

export const PLAYERS = "manual/players.json";
export const EVENTS = "manual/events.json";
export const LOG = "log.json";
export const PUBLISHED = "published.json";
const LOG_KEEP = 300;
const MAX_ITEMS = 1500;

const emptyList = () => ({ version: 1, items: [] });
const emptyLog = () => ({ items: [] });
const emptyPublished = () => ({});

// 读出来的 Liquipedia 页面名一律是页面名：2026-11-18 以前存的有整个网址（那时候还不认网址），读的时候转一下，下次保存就存成页面名
export async function players(bucket) {
  return (await readDoc(bucket, PLAYERS, emptyList)).data.items.map((p) => (/^https?:\/\//i.test(p.liquipedia ?? "")
    ? { ...p, liquipedia: pageFromInput(p.liquipedia) ?? p.liquipedia } : p));
}

export async function events(bucket) {
  return (await readDoc(bucket, EVENTS, emptyList)).data.items;
}

export async function recentLog(bucket, count = LOG_KEEP) {
  return (await readDoc(bucket, LOG, emptyLog)).data.items.slice(0, count);
}

export async function published(bucket) {
  return (await readDoc(bucket, PUBLISHED, emptyPublished)).data;
}

export async function writeLog(bucket, by, now, lines) {
  const items = (Array.isArray(lines) ? lines : [lines]).filter(Boolean);
  if (!items.length) return;
  await updateDoc(bucket, LOG, emptyLog, (log) => {
    log.items = [...items.reverse().map((text) => ({ at: now, by: by.name, text })), ...log.items].slice(0, LOG_KEEP);
  });
}

export async function recordPublish(bucket, target, record) {
  await updateDoc(bucket, PUBLISHED, emptyPublished, (data) => {
    data[target] = record;
  });
}

// 战队名单（按选手库里第一次出现的顺序）和每队几个人
export function teamsOf(items) {
  const teams = new Map();
  for (const p of items) teams.set(p.team, (teams.get(p.team) ?? 0) + 1);
  return teams;
}

function playerText(p) {
  return `${p.name}（${p.team}，${p.platform === NO_STREAM || !p.platform ? platformName(p.platform) : `${platformName(p.platform)} ${p.room}`}）`;
}

function stamp(item, by, now) {
  item.updated = now;
  item.updatedBy = by.name;
  item.rev = (item.rev ?? 0) + 1;
}

function checkRev(item, rev, what) {
  if (rev !== undefined && rev !== "" && Number(rev) !== item.rev) {
    throw new Refuse(`${item.updatedBy} 刚改过${what}（你打开以后），没有保存：请看一眼现在的样子再改`);
  }
}

function findItem(list, id, what) {
  const item = list.items.find((x) => x.id === id);
  if (!item) throw new Refuse(`没有这个${what}（可能刚被别人删了）`);
  return item;
}

// 一个选手只能在一支队、同一个平台房间不能写两个人（保存时就查）
function assertUniquePlayer(items, fields, selfId) {
  for (const other of items) {
    if (other.id === selfId) continue;
    if (other.name === fields.name) throw new Refuse(`已经有一个叫「${fields.name}」的选手了（在「${other.team}」）`);
    if (fields.platform && fields.platform !== NO_STREAM && other.platform === fields.platform && other.room === fields.room) {
      throw new Refuse(`${platformName(fields.platform)} 房间 ${fields.room} 已经是「${other.name}」的了`);
    }
  }
}

function changes(before, after, labels) {
  return Object.entries(labels).filter(([key]) => String(before[key] ?? "") !== String(after[key] ?? ""))
    .map(([key, label]) => `${label} ${before[key] || "（空）"} → ${after[key] || "（空）"}`);
}

const PLAYER_LABELS = { name: "名字", team: "战队", platform: "平台", room: "房间号", liquipedia: "Liquipedia" };

// ---------- 选手 ----------

export async function addPlayer(bucket, by, form, now) {
  const fields = cleanPlayerFields(form);
  const item = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    if (list.items.length >= MAX_ITEMS) throw new Refuse(`选手最多 ${MAX_ITEMS} 个`);
    assertUniquePlayer(list.items, fields);
    const record = { id: randomHex(4), ...fields, created: now, createdBy: by.name, updated: now, updatedBy: by.name, rev: 1 };
    list.items.push(record);
    return record;
  });
  await writeLog(bucket, by, now, `添加选手 ${playerText(item)}`);
  return item;
}

export async function savePlayer(bucket, by, id, form, now) {
  const fields = cleanPlayerFields(form);
  const diff = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const item = findItem(list, id, "选手");
    checkRev(item, form.rev, `「${item.name}」`);
    assertUniquePlayer(list.items, fields, id);
    const before = { ...item, platform: platformName(item.platform) };
    Object.assign(item, fields);
    const lines = changes(before, { ...item, platform: platformName(item.platform) }, PLAYER_LABELS);
    if (lines.length) stamp(item, by, now);
    return { name: item.name, lines };
  });
  if (diff.lines.length) await writeLog(bucket, by, now, `修改选手 ${diff.name}：${diff.lines.join("，")}`);
}

export async function deletePlayer(bucket, by, id, now) {
  const gone = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const item = findItem(list, id, "选手");
    list.items = list.items.filter((x) => x !== item);
    return item;
  });
  await writeLog(bucket, by, now, `删除选手 ${playerText(gone)}`);
}

function teamName(text, what = "队名") {
  const name = squash(text);
  if (!name) throw new Refuse(`${what}不能空着`);
  if ([...name].length > 40) throw new Refuse(`${what}最多 40 个字`);
  return name;
}

// 选手换队（2026-11-19 用户要的：原来只能删了再加，HAB / HABNW 就是这么来的）：只改他的战队，赛程不动。
// 换到一个选手库里还没有的队名，就是新建一支队
export async function movePlayer(bucket, by, id, team, rev, now) {
  const to = teamName(team, "要换去的队");
  const diff = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const item = findItem(list, id, "选手");
    checkRev(item, rev, `「${item.name}」`);
    if (item.team === to) throw new Refuse(`「${item.name}」已经在「${to}」了`);
    const from = item.team;
    item.team = to;
    stamp(item, by, now);
    return { name: item.name, from, to, emptied: !list.items.some((p) => p.team === from) };
  });
  await writeLog(bucket, by, now, `选手 ${diff.name} 换队：${diff.from} → ${diff.to}${diff.emptied ? `（「${diff.from}」没人了）` : ""}`);
  return diff;
}

// 一张表批量填直播间（2026-11-19 用户要的：原来一次只能改一个人，2ephykr 11-18 花了 54 分钟填了 20 人）。
// form 里每个人三格：p_<编号> 平台、r_<编号> 房间号、v_<编号> 打开页面时的 rev。只存改过的；有一个人不对整批都不存，
// 一条一条写清楚是谁；别人刚改过的那个人也不存（rev 对不上）。平台空着（"直播间待填"）只能是原来就待填、房间号也空着的
export async function saveRooms(bucket, by, form, now) {
  const result = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const errors = [];
    const changed = [];
    for (const item of list.items) {
      if (!Object.hasOwn(form, `p_${item.id}`)) continue;
      const platform = squash(form[`p_${item.id}`]);
      const room = squash(form[`r_${item.id}`]);
      if (!platform) {
        if (room) errors.push(`「${item.name}」：填了房间号，先选平台`);
        else if (item.platform) errors.push(`「${item.name}」：选一个平台（不开直播的选"不直播"）`);
        continue;
      }
      let fields;
      try {
        fields = cleanPlayerFields({ ...item, platform, room });
      } catch (error) {
        if (!(error instanceof Refuse)) throw error;
        errors.push(`「${item.name}」：${error.message}`);
        continue;
      }
      if (fields.platform === item.platform && fields.room === item.room) continue;
      const rev = form[`v_${item.id}`];
      if (rev !== undefined && rev !== "" && Number(rev) !== item.rev) {
        errors.push(`「${item.name}」：${item.updatedBy} 刚改过（你打开以后），看一眼现在的样子再填`);
        continue;
      }
      changed.push({ item, fields });
    }
    const after = new Map(list.items.map((p) => [p.id, p]));
    for (const { item, fields } of changed) after.set(item.id, { ...item, platform: fields.platform, room: fields.room });
    const rooms = new Map();
    for (const p of after.values()) {
      if (p.platform === NO_STREAM || !p.platform) continue;
      const key = `${p.platform}-${p.room}`;
      if (rooms.has(key)) errors.push(`${platformName(p.platform)} 房间 ${p.room} 同时填给了「${rooms.get(key)}」和「${p.name}」`);
      rooms.set(key, p.name);
    }
    if (errors.length) throw new Refuse(`没有保存，有 ${errors.length} 处要改：\n${errors.join("\n")}`);
    for (const { item, fields } of changed) {
      item.platform = fields.platform;
      item.room = fields.room;
      stamp(item, by, now);
    }
    return changed.map(({ item }) => `${item.name} ${item.platform === NO_STREAM ? platformName(item.platform) : `${platformName(item.platform)} ${item.room}`}`);
  });
  if (result.length) await writeLog(bucket, by, now, `批量填直播间（${result.length} 位）：${result.join("、")}`);
  return result.length;
}

// 两支队并成一支（2026-11-19 用户要的）：from 的选手都挪到 to，赛程里的 from 换成 to（同一场里两支都有的只留一个），
// to 没有队徽时用 from 的
export async function mergeTeam(bucket, by, from, to, now) {
  const oldName = teamName(from);
  const newName = teamName(to, "要并进去的队");
  if (oldName === newName) throw new Refuse("选另一支队");
  const count = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    if (!list.items.some((p) => p.team === newName)) throw new Refuse(`选手库里没有叫「${newName}」的队（只是想改名的话用"改队名"）`);
    const members = list.items.filter((p) => p.team === oldName);
    if (!members.length) throw new Refuse(`没有叫「${oldName}」的队（可能刚被别人改了）`);
    for (const p of members) {
      p.team = newName;
      stamp(p, by, now);
    }
    return members.length;
  });
  const changed = await updateDoc(bucket, EVENTS, emptyList, (list) => {
    let n = 0;
    for (const e of list.items) {
      if (!e.teams.includes(oldName)) continue;
      e.teams = [...new Set(e.teams.map((t) => (t === oldName ? newName : t)))];
      stamp(e, by, now);
      n++;
    }
    return n;
  });
  const logo = await updateDoc(bucket, "manual/teams.json", () => ({ items: {} }), (data) => {  // 队徽（logos.js）
    const moved = Boolean(data.items[oldName] && !data.items[newName]);
    if (moved) data.items[newName] = data.items[oldName];
    delete data.items[oldName];
    return moved;
  });
  await writeLog(bucket, by, now, `战队「${oldName}」并进「${newName}」（${count} 位选手${changed ? `，赛程里 ${changed} 场跟着改了` : ""}`
    + `${logo ? `，队徽用「${oldName}」的` : ""}）`);
}

// 战队改名：选手库和赛程里一起改
export async function renameTeam(bucket, by, from, to, now) {
  const oldName = squash(from);
  const newName = teamName(to, "新队名");
  if (oldName === newName) return;
  const count = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    if (list.items.some((p) => p.team === newName)) throw new Refuse(`已经有一支叫「${newName}」的队了（想把两支队并成一支，用"并到另一支队"）`);
    const members = list.items.filter((p) => p.team === oldName);
    if (!members.length) throw new Refuse(`没有叫「${oldName}」的队（可能刚被别人改了）`);
    for (const p of members) {
      p.team = newName;
      stamp(p, by, now);
    }
    return members.length;
  });
  await updateDoc(bucket, EVENTS, emptyList, (list) => {
    for (const e of list.items) {
      if (e.teams.includes(oldName)) {
        e.teams = e.teams.map((t) => (t === oldName ? newName : t));
        stamp(e, by, now);
      }
    }
  });
  await updateDoc(bucket, "manual/teams.json", () => ({ items: {} }), (data) => {  // 队徽跟着搬（logos.js）
    if (data.items[oldName]) {
      data.items[newName] = data.items[oldName];
      delete data.items[oldName];
    }
  });
  await writeLog(bucket, by, now, `战队改名 ${oldName} → ${newName}（${count} 位选手，赛程里也跟着改了）`);
}

// 从 Excel 粘贴的一批：名字已经有的更新，没有的新加。有一行不对就整批不存
export async function importPlayerRows(bucket, by, rows, now) {
  const result = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const errors = [];
    const seen = new Map();
    for (const row of rows) {
      if (seen.has(row.name)) errors.push(`第 ${row.line} 行：选手「${row.name}」在第 ${seen.get(row.name)} 行已经写过`);
      seen.set(row.name, row.line);
    }
    // 先算出导入以后的样子，再查房间号有没有撞
    const after = list.items.map((p) => ({ ...p }));
    let added = 0;
    let changed = 0;
    for (const row of rows) {
      const { line, ...fields } = row;
      const existing = after.find((p) => p.name === fields.name);
      if (existing) {
        if (Object.keys(PLAYER_LABELS).some((k) => String(existing[k] ?? "") !== String(fields[k] ?? ""))) {
          Object.assign(existing, fields);
          stamp(existing, by, now);
          changed++;
        }
      } else {
        after.push({ id: randomHex(4), ...fields, created: now, createdBy: by.name, updated: now, updatedBy: by.name, rev: 1, line });
        added++;
      }
    }
    const rooms = new Map();
    for (const p of after) {
      if (p.platform === NO_STREAM || !p.platform) continue;  // 不直播的、待填的没有房间
      const key = `${p.platform}-${p.room}`;
      if (rooms.has(key)) errors.push(`${platformName(p.platform)} 房间 ${p.room} 同时是「${rooms.get(key)}」和「${p.name}」的`);
      rooms.set(key, p.name);
    }
    if (after.length > MAX_ITEMS) errors.push(`选手最多 ${MAX_ITEMS} 个`);
    if (errors.length) throw new Refuse(`没有导入，有 ${errors.length} 处要改：\n${errors.join("\n")}`);
    list.items = after.map(({ line, ...p }) => p);
    return { added, changed };
  });
  await writeLog(bucket, by, now, `从表格导入选手：新加 ${result.added} 位，更新 ${result.changed} 位`);
  return result;
}

// ---------- 赛事 ----------

// 改动记录里的赛事带上阶段（A 组、B 组同名时分得清；2026-11-19 用户同意）
function eventText(e) {
  return `${eventLabel(e)}（${e.type}，${e.start.slice(0, 16).replace("T", " ")}）`;
}

const EVENT_LABELS = { name: "名称", type: "类型", stage: "阶段", start: "开始", end: "结束" };
const streamText = (m) => `${m.name}（${platformName(m.platform)} ${m.room}）`;

// 队伍、主舞台改了什么：加了谁、去掉了谁（主舞台还有同一个直播间改了名字），只换了顺序的写"顺序换了"
function listChange(label, before, after, key, text) {
  const had = new Map(before.map((x) => [key(x), x]));
  const has = new Map(after.map((x) => [key(x), x]));
  const parts = [];
  const added = after.filter((x) => !had.has(key(x)));
  const removed = before.filter((x) => !has.has(key(x)));
  if (added.length) parts.push(`加了 ${added.map(text).join("、")}`);
  if (removed.length) parts.push(`去掉了 ${removed.map(text).join("、")}`);
  const renamed = after.filter((x) => had.has(key(x)) && had.get(key(x)).name !== undefined && had.get(key(x)).name !== x.name);
  if (renamed.length) parts.push(renamed.map((x) => `「${had.get(key(x)).name}」改名「${x.name}」`).join("、"));
  if (!parts.length && before.map(key).join("\n") !== after.map(key).join("\n")) parts.push("顺序换了");
  return parts.length ? `${label}${parts.join("，")}` : "";
}

async function knownTeams(bucket) {
  return [...teamsOf(await players(bucket)).keys()];
}

// ---------- 主舞台记下的直播间（2026-11-21 赛管毛毛要的"直播间记忆，保存主播直播间号，在发布赛事时直接选择"）----------
// manual/main_rooms.json {"items": {"平台-房间号": {platform, room, name, at}}}：填过的主舞台直播间，名字用最近一次填的。
// 还没有这个文件时，从赛程里已有的主舞台算一份（第一次用就有）。主舞台名字那一格的下拉 = 这些 + 选手库里开直播的人（mainChoices）；
// 只写了名字、平台和房间号空着的行，保存时照名字补上（fillMain；网页上 /app.js 选了就先填好）
export const MAIN_ROOMS = "manual/main_rooms.json";
const roomKey = (platform, room) => `${platform}-${normalRoom(platform, room)}`;

function seedRooms(eventItems) {
  const items = {};
  for (const e of [...eventItems].sort((a, b) => (a.updated ?? 0) - (b.updated ?? 0))) {
    for (const m of e.main ?? []) {
      items[roomKey(m.platform, m.room)] = { platform: m.platform, room: normalRoom(m.platform, m.room), name: m.name, at: e.updated ?? 0 };
    }
  }
  return { items };
}

// 记下的直播间：[{key, platform, room, name, at}]，最近用过的在前
export async function savedRooms(bucket) {
  const doc = (await readDoc(bucket, MAIN_ROOMS, () => null)).data ?? seedRooms(await events(bucket));
  return Object.entries(doc.items ?? {}).map(([key, r]) => ({ key, ...r })).sort((a, b) => (b.at ?? 0) - (a.at ?? 0) || (a.name < b.name ? -1 : 1));
}

async function rememberMain(bucket, main, now) {
  if (!main?.length) return;
  const seed = seedRooms(await events(bucket));
  await updateDoc(bucket, MAIN_ROOMS, () => seed, (doc) => {
    for (const m of main) doc.items[roomKey(m.platform, m.room)] = { platform: m.platform, room: normalRoom(m.platform, m.room), name: m.name, at: now };
  });
}

// 忘掉几个记下的直播间（keys 是"平台-房间号"）。赛程里已经填着的不动
export async function forgetRooms(bucket, by, keys, now) {
  const seed = seedRooms(await events(bucket));
  const gone = await updateDoc(bucket, MAIN_ROOMS, () => seed, (doc) => {
    const out = [];
    for (const key of keys) {
      if (!Object.hasOwn(doc.items, key)) continue;
      out.push(doc.items[key]);
      delete doc.items[key];
    }
    return out;
  });
  if (!gone.length) throw new Refuse("没有勾选要忘掉的直播间");
  await writeLog(bucket, by, now, `忘掉记下的直播间：${gone.map(streamText).join("、")}`);
  return gone.length;
}

// 主舞台名字那一格能选的：记下的直播间在前，选手库里开直播的人在后（同一个直播间只列一次，用记下的名字）
export function mainChoices(rooms, playerItems) {
  const out = rooms.map((r) => ({ name: r.name, platform: r.platform, room: r.room, from: "saved" }));
  const seen = new Set(rooms.map((r) => r.key));
  for (const p of playerItems) {
    if (!p.platform || p.platform === NO_STREAM || !p.room || seen.has(roomKey(p.platform, p.room))) continue;
    seen.add(roomKey(p.platform, p.room));
    out.push({ name: p.name, platform: p.platform, room: normalRoom(p.platform, p.room), from: "player", team: p.team });
  }
  return out;
}

// 只写了名字、平台和房间号都空着的行：照名字从能选的里补上；认不出的名字说清楚
export function fillMain(rows, choices) {
  if (!Array.isArray(rows)) return rows;
  return rows.map((row) => {
    const name = squash(row.name);
    if (!name || squash(row.platform) || squash(row.room)) return row;
    const found = choices.find((c) => c.name === name) ?? choices.find((c) => c.name.toLowerCase() === name.toLowerCase());
    if (!found) throw new Refuse(`主舞台「${name}」：记下的直播间和选手库里都没有这个名字。新的直播间要把平台和房间号也填上`);
    return { platform: found.platform, room: found.room, name };
  });
}

async function withMain(bucket, form) {
  if (!Array.isArray(form.main) || !form.main.some((r) => squash(r.name) && !squash(r.platform) && !squash(r.room))) return form;
  return { ...form, main: fillMain(form.main, mainChoices(await savedRooms(bucket), await players(bucket))) };
}

export async function addEvent(bucket, by, form, now) {
  form = await withMain(bucket, form);
  const fields = cleanEventFields(form, await knownTeams(bucket));
  const item = await updateDoc(bucket, EVENTS, emptyList, (list) => {
    if (list.items.length >= MAX_ITEMS) throw new Refuse(`赛事最多 ${MAX_ITEMS} 场`);
    const record = { id: randomHex(4), ...fields, created: now, createdBy: by.name, updated: now, updatedBy: by.name, rev: 1 };
    list.items.push(record);
    return record;
  });
  await writeLog(bucket, by, now, `添加赛事${eventText(item)}，${item.teams.length} 支队${item.main.length ? `，主舞台 ${item.main.length} 路` : ""}`);
  await rememberMain(bucket, item.main, now);
  return item;
}

export async function saveEvent(bucket, by, id, form, now) {
  form = await withMain(bucket, form);
  const known = await knownTeams(bucket);
  const diff = await updateDoc(bucket, EVENTS, emptyList, (list) => {
    const item = findItem(list, id, "赛事");
    checkRev(item, form.rev, `「${item.name}」`);
    // 选手库里已经没有的队（被删了、改名了），原来就在这场里的也算认识：不然一保存就报错
    const fields = cleanEventFields(form, [...known, ...item.teams]);
    if (!Array.isArray(form.main)) fields.main = item.main ?? [];  // 表单里没有主舞台那几格（老页面）：照旧
    const when = (iso) => iso.slice(0, 16).replace("T", " ");
    const before = { ...item, start: when(item.start), end: when(item.end) };
    const oldTeams = [...item.teams];
    const oldMain = [...(item.main ?? [])];
    Object.assign(item, fields);
    const lines = [...changes(before, { ...item, start: when(item.start), end: when(item.end) }, EVENT_LABELS),
      listChange("队伍：", oldTeams, item.teams, (t) => t, (t) => t),
      listChange("主舞台：", oldMain, item.main, (m) => `${m.platform}-${m.room}`, streamText)].filter(Boolean);
    if (item.teams.join("\n") !== oldTeams.join("\n") && lines.some((l) => l.startsWith("队伍：加了") || l.startsWith("队伍：去掉了"))) {
      lines[lines.findIndex((l) => l.startsWith("队伍："))] += `（现在 ${item.teams.length} 支）`;
    }
    // 11-19 到 11-21 有过"观战"（后来并进主舞台）：存着的那一项保存时顺手去掉
    if (item.observers) delete item.observers;
    if (lines.length) stamp(item, by, now);
    return { label: eventLabel(item), lines, main: item.main ?? [] };
  });
  if (diff.lines.length) {
    await writeLog(bucket, by, now, `修改赛事${diff.label}：${diff.lines.join("；")}`);
    await rememberMain(bucket, diff.main, now);
  }
}

export async function deleteEvent(bucket, by, id, now) {
  const gone = await updateDoc(bucket, EVENTS, emptyList, (list) => {
    const item = findItem(list, id, "赛事");
    list.items = list.items.filter((x) => x !== item);
    return item;
  });
  await writeLog(bucket, by, now, `删除赛事${eventText(gone)}`);
}

// 粘贴的一批赛事：名称和开始时间都一样的算同一场（更新），别的新加。有一行不对就整批不存
export async function importEventRows(bucket, by, rows, now) {
  const result = await updateDoc(bucket, EVENTS, emptyList, (list) => {
    let added = 0;
    let changed = 0;
    for (const { line, ...fields } of rows) {
      const existing = list.items.find((e) => e.name === fields.name && e.start === fields.start);
      if (fields.main === undefined) fields.main = existing?.main ?? [];  // 粘贴的表没有「主舞台」那一列：照旧
      if (existing) {
        if (JSON.stringify([existing.type, existing.stage, existing.end, existing.teams, existing.main ?? []])
            !== JSON.stringify([fields.type, fields.stage, fields.end, fields.teams, fields.main])) {
          Object.assign(existing, fields);
          stamp(existing, by, now);
          changed++;
        }
      } else {
        list.items.push({ id: randomHex(4), ...fields, created: now, createdBy: by.name, updated: now, updatedBy: by.name, rev: 1 });
        added++;
      }
    }
    if (list.items.length > MAX_ITEMS) throw new Refuse(`赛事最多 ${MAX_ITEMS} 场`);
    return { added, changed };
  });
  await writeLog(bucket, by, now, `从表格导入赛程：新加 ${result.added} 场，更新 ${result.changed} 场`);
  await rememberMain(bucket, rows.flatMap((r) => r.main ?? []), now);
  return result;
}
