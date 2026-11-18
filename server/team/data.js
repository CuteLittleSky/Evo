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
import { PLATFORMS, cleanEventFields, cleanPlayerFields, squash } from "./esports.js";
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
  return `${p.name}（${p.team}，${PLATFORMS[p.platform]?.name ?? p.platform} ${p.room}）`;
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
    if (other.platform === fields.platform && other.room === fields.room) {
      throw new Refuse(`${PLATFORMS[fields.platform].name} 房间 ${fields.room} 已经是「${other.name}」的了`);
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
    const before = { ...item, platform: PLATFORMS[item.platform]?.name };
    Object.assign(item, fields);
    const lines = changes(before, { ...item, platform: PLATFORMS[item.platform]?.name }, PLAYER_LABELS);
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

// 战队改名：选手库和赛程里一起改
export async function renameTeam(bucket, by, from, to, now) {
  const oldName = squash(from);
  const newName = squash(to);
  if (!newName) throw new Refuse("新队名不能空着");
  if ([...newName].length > 40) throw new Refuse("队名最多 40 个字");
  if (oldName === newName) return;
  const count = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    if (list.items.some((p) => p.team === newName)) throw new Refuse(`已经有一支叫「${newName}」的队了`);
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
      const key = `${p.platform}-${p.room}`;
      if (rooms.has(key)) errors.push(`${PLATFORMS[p.platform].name} 房间 ${p.room} 同时是「${rooms.get(key)}」和「${p.name}」的`);
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

function eventText(e) {
  return `「${e.name}」（${e.type}，${e.start.slice(0, 16).replace("T", " ")}）`;
}

const EVENT_LABELS = { name: "名称", type: "类型", stage: "阶段", start: "开始", end: "结束", teams: "队伍", main: "主舞台" };
const mainText = (main) => (main?.length ? `${main.length} 路` : "没有");

async function knownTeams(bucket) {
  return [...teamsOf(await players(bucket)).keys()];
}

export async function addEvent(bucket, by, form, now) {
  const fields = cleanEventFields(form, await knownTeams(bucket));
  const item = await updateDoc(bucket, EVENTS, emptyList, (list) => {
    if (list.items.length >= MAX_ITEMS) throw new Refuse(`赛事最多 ${MAX_ITEMS} 场`);
    const record = { id: randomHex(4), ...fields, created: now, createdBy: by.name, updated: now, updatedBy: by.name, rev: 1 };
    list.items.push(record);
    return record;
  });
  await writeLog(bucket, by, now, `添加赛事${eventText(item)}，${item.teams.length} 支队${item.main.length ? `，主舞台 ${item.main.length} 路` : ""}`);
  return item;
}

export async function saveEvent(bucket, by, id, form, now) {
  const known = await knownTeams(bucket);
  const diff = await updateDoc(bucket, EVENTS, emptyList, (list) => {
    const item = findItem(list, id, "赛事");
    checkRev(item, form.rev, `「${item.name}」`);
    // 选手库里已经没有的队（被删了、改名了），原来就在这场里的也算认识：不然一保存就报错
    const fields = cleanEventFields(form, [...known, ...item.teams]);
    if (!Array.isArray(form.main)) fields.main = item.main ?? [];  // 表单里没有主舞台那几格（老页面）：照旧
    const when = (iso) => iso.slice(0, 16).replace("T", " ");
    const before = { ...item, teams: `${item.teams.length} 支队`, start: when(item.start), end: when(item.end), main: mainText(item.main) };
    const sameTeams = item.teams.join("\n") === fields.teams.join("\n");
    const sameMain = JSON.stringify(item.main ?? []) === JSON.stringify(fields.main);
    Object.assign(item, fields);
    const after = { ...item, teams: sameTeams ? before.teams : `${item.teams.length} 支队（换过）`,
                    start: when(item.start), end: when(item.end), main: sameMain ? before.main : `${mainText(item.main)}（换过）` };
    const lines = changes(before, after, EVENT_LABELS);
    if (lines.length) stamp(item, by, now);
    return { name: item.name, lines };
  });
  if (diff.lines.length) await writeLog(bucket, by, now, `修改赛事「${diff.name}」：${diff.lines.join("，")}`);
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
  return result;
}
