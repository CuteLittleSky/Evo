// 战队：赛区、战队页的数、整队删掉、一次删几位选手（2026-11-21 赛管毛毛要的"分赛区的页面，方便查找"、"老选手老队伍可以批量删掉了"）。
//
// 赛区照 ALGS 四个大区（用户定的："照 ALGS 四个大区，但是国内队是亚太南"）：亚太北、亚太南、EMEA、美洲。
// 手动标的存在 manual/regions.json：{"items": {队名: 赛区}}（不和队徽放一个文件：换队徽时那一条整条重写）；战队改名、并队时跟着搬（data.js）。
// 没手动标过的队按 Liquipedia 猜（guessRegions）：队里每位选手看他在 Liquipedia 上现在那支战队的赛区，没有战队的看第一个国籍，
// 多数说了算；都认不出就是"没标赛区"。台湾、香港、澳门猜成亚太北（Claude 定的，ALGS 的分法；不对就在战队页改）。

import { Refuse, readDoc, updateDoc } from "./store.js";
import { PLAYERS, events, players, writeLog } from "./data.js";
import { eventLabel, parseIso, squash, status } from "./esports.js";
import { index } from "./lpsync.js";

export const REGIONS = { apac_n: "亚太北", apac_s: "亚太南", emea: "EMEA", americas: "美洲" };
export const NONE = "none";  // 页面上"没标赛区"那一栏
export const REGION_FILE = "manual/regions.json";

const emptyRegions = () => ({ items: {} });
const emptyList = () => ({ version: 1, items: [] });

// Liquipedia 战队页上的 region → 大区
const LP_REGION = {
  China: "apac_s", "Southeast Asia": "apac_s", Oceania: "apac_s", "South Asia": "apac_s", India: "apac_s",
  Japan: "apac_n", Korea: "apac_n", "South Korea": "apac_n", Taiwan: "apac_n", "Hong Kong": "apac_n", "East Asia": "apac_n",
  Europe: "emea", CIS: "emea", "Middle East": "emea", Africa: "emea", EMEA: "emea",
  "North America": "americas", "South America": "americas", "Latin America": "americas", Americas: "americas",
};
// 国籍（Liquipedia 选手页的第一个国籍）→ 大区
const COUNTRY_REGION = Object.fromEntries([
  ["apac_s", ["China", "Thailand", "Vietnam", "Philippines", "Indonesia", "Malaysia", "Singapore", "Cambodia", "Myanmar", "Laos", "Brunei", "India",
    "Pakistan", "Bangladesh", "Sri Lanka", "Nepal", "Australia", "New Zealand", "Cook Islands", "Fiji", "Papua New Guinea", "Samoa", "Tonga"]],
  ["apac_n", ["Japan", "South Korea", "Korea", "Taiwan", "Hong Kong", "Macau", "Mongolia"]],
  ["americas", ["United States", "Canada", "Mexico", "Brazil", "Argentina", "Chile", "Colombia", "Peru", "Uruguay", "Paraguay", "Bolivia",
    "Ecuador", "Venezuela", "Costa Rica", "Panama", "Guatemala", "Honduras", "El Salvador", "Nicaragua", "Dominican Republic", "Puerto Rico",
    "Cuba", "Jamaica", "Trinidad and Tobago"]],
  ["emea", ["United Kingdom", "Ireland", "France", "Germany", "Netherlands", "Belgium", "Luxembourg", "Switzerland", "Austria", "Italy", "Spain",
    "Portugal", "Poland", "Czechia", "Czech Republic", "Slovakia", "Hungary", "Slovenia", "Croatia", "Serbia", "Bosnia and Herzegovina", "Montenegro",
    "North Macedonia", "Albania", "Greece", "Bulgaria", "Romania", "Moldova", "Ukraine", "Belarus", "Russia", "Lithuania", "Latvia", "Estonia",
    "Finland", "Sweden", "Norway", "Denmark", "Iceland", "Turkey", "Kazakhstan", "Uzbekistan", "Georgia", "Armenia", "Azerbaijan", "Israel",
    "Saudi Arabia", "United Arab Emirates", "Qatar", "Kuwait", "Bahrain", "Oman", "Jordan", "Lebanon", "Egypt", "Morocco", "Algeria", "Tunisia",
    "South Africa", "Nigeria", "Kenya", "Malta", "Cyprus"]],
].flatMap(([region, countries]) => countries.map((c) => [c, region])));

export async function manualRegions(bucket) {
  return (await readDoc(bucket, REGION_FILE, emptyRegions)).data.items;
}

// 一位选手按 Liquipedia 猜的大区（认不出是 null）
function playerRegion(p, idx) {
  const lp = p.liquipedia ? idx.players?.[p.liquipedia] : null;
  if (!lp) return null;
  const team = lp.teamPage ? idx.teams?.[lp.teamPage] : null;
  return LP_REGION[team?.region] ?? COUNTRY_REGION[(lp.nats ?? [])[0]] ?? null;
}

// 每支队按 Liquipedia 猜的大区：{队名: 大区}（认不出的队不在里面）。票数一样多时取队里第一个认出来的
export function guessRegions(items, idx) {
  const votes = new Map();
  for (const p of items) {
    const region = playerRegion(p, idx);
    if (!region) continue;
    if (!votes.has(p.team)) votes.set(p.team, new Map());
    const v = votes.get(p.team);
    v.set(region, (v.get(region) ?? 0) + 1);
  }
  const out = {};
  for (const [team, v] of votes) out[team] = [...v.entries()].sort((a, b) => b[1] - a[1])[0][0];
  return out;
}

// 每支队的大区：{队名: {region: 大区或 NONE, guessed: 是不是猜的}}
export async function teamRegions(bucket, items = null) {
  const [list, manual, idx] = await Promise.all([items ?? players(bucket), manualRegions(bucket), index(bucket)]);
  const guesses = guessRegions(list, idx);
  const out = {};
  for (const p of list) {
    if (out[p.team]) continue;
    const set = Object.hasOwn(REGIONS, manual[p.team]) ? manual[p.team] : null;
    out[p.team] = { region: set ?? guesses[p.team] ?? NONE, guessed: !set && Boolean(guesses[p.team]) };
  }
  return out;
}

// 战队页的每一行：{team, count, region, guessed, last: 最近一场赛事 {label, end} 或 null, busy: 进行中、即将开始的赛事里有它}
export async function teamOverview(bucket, now) {
  const [list, eventItems] = await Promise.all([players(bucket), events(bucket)]);
  const regions = await teamRegions(bucket, list);
  const counts = new Map();
  for (const p of list) counts.set(p.team, (counts.get(p.team) ?? 0) + 1);
  return [...counts].map(([team, count]) => {
    const mine = eventItems.filter((e) => e.teams.includes(team));
    const latest = mine.sort((a, b) => parseIso(b.end) - parseIso(a.end))[0];
    return { team, count, ...regions[team], last: latest ? { label: eventLabel(latest), end: latest.end } : null,
             busy: mine.some((e) => status(e, now) !== "ended") };
  });
}

// 标赛区：form 里每支队一组 tn_i（队名）、tr_i（选的）、tw_i（打开时的）；只存改过的。选"没标"= 去掉手动标的，回到按 Liquipedia 猜
export async function saveRegions(bucket, by, form, now) {
  const picks = [];
  for (let i = 0; Object.hasOwn(form, `tn_${i}`); i++) {
    const team = squash(form[`tn_${i}`]);
    const region = form[`tr_${i}`] ?? "";
    if (!team || region === form[`tw_${i}`]) continue;
    if (region !== "" && !Object.hasOwn(REGIONS, region)) throw new Refuse(`「${team}」的赛区不认识`);
    picks.push([team, region]);
  }
  if (!picks.length) return 0;
  await updateDoc(bucket, REGION_FILE, emptyRegions, (doc) => {
    for (const [team, region] of picks) {
      if (region) doc.items[team] = region;
      else delete doc.items[team];
    }
  });
  await writeLog(bucket, by, now, `标赛区（${picks.length} 支队）：${picks.map(([team, region]) => `${team} → ${region ? REGIONS[region] : "没标（按 Liquipedia 猜）"}`).join("、")}`);
  return picks.length;
}

// 整队删掉（连同队里的选手、队徽和赛区）。进行中、即将开始的赛事里有的队不让删（发布时会挡住）；结束了的赛事里写着也能删
export async function deleteTeams(bucket, by, names, now) {
  const wanted = [...new Set(names.map(squash).filter(Boolean))];
  if (!wanted.length) throw new Refuse("没有勾选要删的队");
  const busy = (await events(bucket)).filter((e) => status(e, now) !== "ended" && e.teams.some((t) => wanted.includes(t)));
  if (busy.length) {
    const teams = [...new Set(busy.flatMap((e) => e.teams.filter((t) => wanted.includes(t))))];
    throw new Refuse(`「${teams.join("」「")}」在进行中、即将开始的赛事里（${busy.map(eventLabel).join("、")}）：先从赛程里去掉，再删`);
  }
  const gone = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const out = list.items.filter((p) => wanted.includes(p.team));
    list.items = list.items.filter((p) => !wanted.includes(p.team));
    return out;
  });
  for (const key of ["manual/teams.json", REGION_FILE]) {  // 队徽（logos.js）、赛区
    await updateDoc(bucket, key, emptyRegions, (doc) => { for (const t of wanted) delete doc.items[t]; });
  }
  const per = wanted.map((t) => `${t}（${gone.filter((p) => p.team === t).length} 人）`);
  await writeLog(bucket, by, now, `删掉战队（${wanted.length} 支，连同 ${gone.length} 位选手）：${per.join("、")}`);
  return { teams: wanted.length, players: gone.length };
}

// 一次删几位选手（批量修改页勾选的）
export async function deletePlayers(bucket, by, ids, now) {
  const wanted = new Set(ids);
  if (!wanted.size) throw new Refuse("没有勾选要删的选手");
  const gone = await updateDoc(bucket, PLAYERS, emptyList, (list) => {
    const out = list.items.filter((p) => wanted.has(p.id));
    list.items = list.items.filter((p) => !wanted.has(p.id));
    return out;
  });
  if (!gone.length) throw new Refuse("勾选的选手已经不在了（可能刚被别人删了）");
  await writeLog(bucket, by, now, `删掉选手（${gone.length} 位）：${gone.map((p) => `${p.name}（${p.team}）`).join("、")}`);
  return gone.length;
}

