// 定时从 Liquipedia 抓数据（2026-11-18 第 3 步）。Worker 的定时任务每 3 分钟叫一次 tick()，每次最多发 1 个请求。
//
// 抓什么：网页上"收录的比赛"（Liquipedia 比赛页面名）的资料和参赛名单 → 名单里的人加上选手库里填了 Liquipedia 页面名的人，
// 抓他们的资料、队伍经历、历届成绩 → 他们现在的队和参赛队伍的战队资料。一轮排成一串步骤（queue），一步一个请求。
// 额度：两次请求至少隔 2.5 分钟、一小时最多 20 次（用户的 Key 一小时 60 次，派圈的定时器最多用 12 次）。
// 一轮抓完歇 24 小时；页面上"现在更新"只是让下一轮马上开始，不会多发请求。
// 失败：一步连着 3 次不成就跳过（旧数据留着，宁可旧、不可编），记在错误里。发请求之前先把"试了几次"写下来：
// 万一这一次 Worker 被掐断（免费版每次运行的计算时间很短），下次也知道试过，不会一直重试同一步、白白用掉额度。
//
// 存在 evo-team 的 lp/ 下（和手动的 manual/ 分开，谁也不改谁的）：
//   lp/tracked.json   {"items": [{"page", "addedBy", "addedAt"}]}：收录的比赛（网页上加、删）
//   lp/state.json     进度：queue、round、recent（最近一小时发请求的时刻）、attempts、errors、lastRequestAt、lastRoundDone、refresh
//   lp/index.json     {"events": {页面名: 比赛资料 + teams/players 数}, "players": {页面名: 选手资料}, "teams": {页面名: 战队资料}}
//                     没找到的写 {"page", "missing": true}
//   lp/squads.json    {页面名: [队伍经历]}
//   lp/event/<页面名>.json    一场比赛的参赛名单
//   lp/results/<页面名>.json  一位选手的历届成绩（页面名用 encodeURIComponent）
//   manual/lp_rejects.json    待确认里点了"不是"的（选手 → Liquipedia 页面名）

import { Refuse, readDoc, updateDoc } from "./store.js";
import { players as manualPlayers, writeLog } from "./data.js";
import { squash } from "./esports.js";
import {
  fetchPlayers, fetchResults, fetchRoster, fetchSquads, fetchTeams, fetchTournaments, pageFromInput, teamPage,
} from "./liquipedia.js";

export const TRACKED = "lp/tracked.json";
export const STATE = "lp/state.json";
export const INDEX = "lp/index.json";
export const SQUADS = "lp/squads.json";
export const REJECTS = "manual/lp_rejects.json";
export const eventKey = (page) => `lp/event/${encodeURIComponent(page)}.json`;
export const resultsKey = (page) => `lp/results/${encodeURIComponent(page)}.json`;

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
export const MIN_GAP = 2.5 * MINUTE;
export const HOURLY_CAP = 20;
export const CYCLE = 24 * HOUR;
export const MAX_ATTEMPTS = 3;
const BATCH = 40;
const RESULT_BATCH = 5;
const MAX_TRACKED = 20;
const KEEP_ERRORS = 20;

const emptyTracked = () => ({ items: [] });
const emptyState = () => ({ queue: [], round: null, recent: [], attempts: {}, errors: [], lastRequestAt: 0, lastRoundDone: 0,
                            refresh: false });
const emptyIndex = () => ({ events: {}, players: {}, teams: {} });
const emptyMap = () => ({});
const emptyRejects = () => ({ items: [] });

export async function tracked(bucket) {
  return (await readDoc(bucket, TRACKED, emptyTracked)).data.items;
}

export async function state(bucket) {
  return (await readDoc(bucket, STATE, emptyState)).data;
}

export async function index(bucket) {
  return (await readDoc(bucket, INDEX, emptyIndex)).data;
}

export async function rejects(bucket) {
  return (await readDoc(bucket, REJECTS, emptyRejects)).data.items;
}

// ---------- 收录的比赛（网页上加、删） ----------

export async function addTracked(bucket, by, input, now) {
  const page = pageFromInput(input);
  if (!page) throw new Refuse("看不懂这个链接：贴 Liquipedia 比赛页的地址（https://liquipedia.net/apexlegends/…），或者页面名");
  await updateDoc(bucket, TRACKED, emptyTracked, (data) => {
    if (data.items.some((x) => x.page === page)) throw new Refuse(`已经收录了 ${page}`);
    if (data.items.length >= MAX_TRACKED) throw new Refuse(`最多收录 ${MAX_TRACKED} 场比赛，先删掉几场不用的`);
    data.items.push({ page, addedBy: by.name, addedAt: now });
  });
  await requestRefresh(bucket);
  await writeLog(bucket, by, now, `收录 Liquipedia 比赛 ${page}`);
  return page;
}

export async function removeTracked(bucket, by, page, now) {
  await updateDoc(bucket, TRACKED, emptyTracked, (data) => {
    if (!data.items.some((x) => x.page === page)) throw new Refuse("没有收录这场比赛（可能刚被别人删了）");
    data.items = data.items.filter((x) => x.page !== page);
  });
  await writeLog(bucket, by, now, `不再收录 Liquipedia 比赛 ${page}（抓过的资料留着）`);
}

// "现在更新"：下一次定时任务就开始新的一轮（正在抓的那一轮照常抓完）
export async function requestRefresh(bucket) {
  await updateDoc(bucket, STATE, emptyState, (s) => {
    s.refresh = true;
  });
}

// ---------- 一轮的步骤 ----------

function chunks(list, size) {
  const out = [];
  for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
  return out;
}

const stepId = (step) => `${step.kind}:${(step.pages ?? [step.page]).join("|")}`.slice(0, 300);

// 开始的几步：比赛资料（一批一问）、每场比赛的名单，然后"排选手"（不发请求，看名单里有谁再排后面的）
function firstSteps(pages) {
  return [...chunks(pages, BATCH).map((p) => ({ kind: "tournaments", pages: p })), ...pages.map((page) => ({ kind: "roster", page })),
          { kind: "plan-players" }];
}

// 名单里的人 + 选手库里填了页面名的人 → 资料、经历、成绩；最后"排战队"
async function planPlayers(bucket, trackedPages) {
  const pages = new Set((await manualPlayers(bucket)).map((p) => p.liquipedia).filter(Boolean));
  for (const page of trackedPages) {
    const event = (await readDoc(bucket, eventKey(page), emptyMap)).data;
    for (const team of event.teams ?? []) for (const p of team.players) pages.add(p.page);
  }
  const list = [...pages];
  return [...chunks(list, BATCH).map((p) => ({ kind: "players", pages: p })), ...chunks(list, BATCH).map((p) => ({ kind: "squads", pages: p })),
          ...chunks(list, RESULT_BATCH).map((p) => ({ kind: "results", pages: p })), { kind: "plan-teams" }];
}

// 参赛的队 + 选手现在的队 → 战队资料
async function planTeams(bucket, idx, trackedPages) {
  const pages = new Set();
  for (const page of trackedPages) {
    const event = (await readDoc(bucket, eventKey(page), emptyMap)).data;
    for (const team of event.teams ?? []) pages.add(teamPage(team.team));
  }
  for (const p of Object.values(idx.players)) if (p.teamPage) pages.add(p.teamPage);
  return chunks([...pages], BATCH).map((p) => ({ kind: "teams", pages: p }));
}

// 发一个请求做一步 → {ok, error, replace（换掉这一步的新步骤）}；数据写进存储桶
async function runStep(bucket, step, deps, now) {
  if (step.kind === "tournaments") {
    const r = await fetchTournaments(step.pages, deps);
    if (!r.ok) return r;
    await updateDoc(bucket, INDEX, emptyIndex, (idx) => {
      for (const page of step.pages) {
        idx.events[page] = r.found[page] ? { ...idx.events[page], ...r.found[page], missing: false, fetchedAt: now }
          : { ...idx.events[page], page, missing: true, fetchedAt: now };
      }
    });
    return { ok: true };
  }
  if (step.kind === "roster") {
    const r = await fetchRoster(step.page, deps);
    if (!r.ok) return r;
    await bucket.put(eventKey(step.page), JSON.stringify({ page: step.page, teams: r.teams, fetchedAt: now }));
    await updateDoc(bucket, INDEX, emptyIndex, (idx) => {
      idx.events[step.page] = { page: step.page, ...idx.events[step.page], rosterTeams: r.teams.length,
                                rosterPlayers: r.teams.reduce((n, t) => n + t.players.length, 0), rosterAt: now };
    });
    return { ok: true };
  }
  if (step.kind === "players" || step.kind === "teams") {
    const r = await (step.kind === "players" ? fetchPlayers : fetchTeams)(step.pages, deps);
    if (!r.ok) return r;
    const field = step.kind === "players" ? "players" : "teams";
    await updateDoc(bucket, INDEX, emptyIndex, (idx) => {
      for (const page of step.pages) {
        idx[field][page] = r.found[page] ? { ...r.found[page], results: idx[field][page]?.results, fetchedAt: now }
          : { page, missing: true, fetchedAt: now };
      }
    });
    return { ok: true };
  }
  if (step.kind === "squads") {
    const r = await fetchSquads(step.pages, deps);
    if (!r.ok) return r;
    await updateDoc(bucket, SQUADS, emptyMap, (all) => Object.assign(all, r.squads));
    return { ok: true };
  }
  if (step.kind === "results") {
    const r = await fetchResults(step.pages, deps);
    if (!r.ok) return r;
    if (r.full && step.pages.length > 1) {  // 给满了：可能截掉了老比赛，拆成两半重查
      const half = Math.ceil(step.pages.length / 2);
      return { ok: true, replace: [{ kind: "results", pages: step.pages.slice(0, half) }, { kind: "results", pages: step.pages.slice(half) }] };
    }
    for (const page of step.pages) {
      await bucket.put(resultsKey(page), JSON.stringify({ page, rows: r.results[page], full: r.full, fetchedAt: now }));
    }
    await updateDoc(bucket, INDEX, emptyIndex, (idx) => {
      for (const page of step.pages) if (idx.players[page]) idx.players[page].results = r.results[page].length;
    });
    return { ok: true };
  }
  return { ok: false, error: `不认识的步骤 ${step.kind}` };
}

function addError(s, now, step, text) {
  s.errors = [{ at: now, step: stepId(step), text }, ...s.errors].slice(0, KEEP_ERRORS);
}

// 定时任务每次叫一次。返回做了什么（测试、日志用）：idle 没到时候 / nothing 没有要抓的 / nokey 没配 Key /
// wait 离上次请求太近或这一小时用完了 / done 做了一步 / failed 这一步没成 / skipped 试了 3 次跳过
export async function tick(env, now, fetchImpl = fetch) {
  const bucket = env.DATA;
  const s = await state(bucket);
  s.recent = s.recent.filter((t) => now - t < HOUR);
  // 写回进度。"现在更新"是页面上另外写进来的：这一次开始了新的一轮就清掉它，别的时候照留（抓的时候又有人点了）
  let tookRefresh = false;
  const save = () => updateDoc(bucket, STATE, emptyState, (fresh) => {
    Object.assign(fresh, s, { refresh: tookRefresh ? false : Boolean(fresh.refresh || s.refresh) });
  });

  if (!s.queue.length) {
    const due = s.refresh || !s.lastRoundDone || now - s.lastRoundDone >= CYCLE;
    if (!due) return "idle";
    const trackedPages = (await tracked(bucket)).map((x) => x.page);
    const manual = (await manualPlayers(bucket)).some((p) => p.liquipedia);
    s.refresh = false;
    tookRefresh = true;
    if (!trackedPages.length && !manual) {
      s.lastRoundDone = now;
      await save();
      return "nothing";
    }
    s.queue = firstSteps(trackedPages);
    s.round = { started: now, done: 0, total: s.queue.length };
    s.attempts = {};
  }
  if (!String(env.LIQUIPEDIA_API_KEY ?? "").trim()) {
    await save();
    return "nokey";
  }

  // 不发请求的"排一排"步骤先做掉
  while (s.queue.length && s.queue[0].kind.startsWith("plan-")) {
    const step = s.queue.shift();
    const trackedPages = (await tracked(bucket)).map((x) => x.page);
    const more = step.kind === "plan-players" ? await planPlayers(bucket, trackedPages) : await planTeams(bucket, await index(bucket), trackedPages);
    s.queue.unshift(...more);
    s.round.total += more.length - 1;
    s.round.done += 1;
  }
  if (!s.queue.length) return finishRound(s, now, save);
  if (now - s.lastRequestAt < MIN_GAP || s.recent.length >= HOURLY_CAP) {
    await save();
    return "wait";
  }

  const step = s.queue[0];
  const id = stepId(step);
  s.attempts[id] = (s.attempts[id] ?? 0) + 1;
  if (s.attempts[id] > MAX_ATTEMPTS) {  // 前几次都没成（或者运行到一半被掐断了）：跳过
    s.queue.shift();
    s.round.done += 1;
    addError(s, now, step, `试了 ${MAX_ATTEMPTS} 次都没成，跳过（上次抓到的照旧留着）`);
    delete s.attempts[id];
    if (!s.queue.length) return finishRound(s, now, save, "skipped");
    await save();
    return "skipped";
  }
  s.lastRequestAt = now;
  s.recent.push(now);
  await save();  // 先记下"试过了"再发请求

  const result = await runStep(bucket, step, { apiKey: env.LIQUIPEDIA_API_KEY, fetchImpl }, now);
  if (!result.ok) {
    addError(s, now, step, result.error);
    await save();
    return "failed";
  }
  s.queue.shift();
  delete s.attempts[id];
  if (result.replace) {
    s.queue.unshift(...result.replace);
    s.round.total += result.replace.length - 1;
  } else {
    s.round.done += 1;
  }
  if (!s.queue.length) return finishRound(s, now, save);
  await save();
  return "done";
}

async function finishRound(s, now, save, outcome = "done") {
  s.lastRoundDone = now;
  if (s.round) s.round.finished = now;
  await save();
  return outcome;
}

// ---------- 待确认：选手库的人和 Liquipedia 对上 ----------

// 比名字时不管大小写、空格、下划线和符号
export function nameKey(text) {
  return squash(text).toLowerCase().replace(/[\s_\-.·'"()（）]/g, "");
}

// 选手库里没填 Liquipedia 页面名的人：名字和抓到的某位选手的 ID 一样（点过"不是"的不算）→ 建议
// 填了页面名、Liquipedia 上却没有这个页面的 → 也列出来
export function pending(manual, idx, rejected) {
  const byKey = new Map();
  for (const p of Object.values(idx.players)) {
    if (p.missing) continue;
    const key = nameKey(p.id);
    if (!byKey.has(key)) byKey.set(key, []);
    byKey.get(key).push(p);
  }
  const no = new Set(rejected.map((x) => `${x.player}|${x.page}`));
  const suggestions = [];
  const missing = [];
  for (const m of manual) {
    if (m.liquipedia) {
      if (idx.players[m.liquipedia]?.missing) missing.push(m);
      continue;
    }
    const candidates = (byKey.get(nameKey(m.name)) ?? []).filter((p) => !no.has(`${m.id}|${p.page}`));
    if (candidates.length) suggestions.push({ player: m, candidates });
  }
  return { suggestions, missing };
}

// 点"是他"：把页面名填进选手库（手动的那一份，记谁点的）
export async function confirmMatch(bucket, by, playerId, page, now) {
  const idx = await index(bucket);
  if (!idx.players[page] || idx.players[page].missing) throw new Refuse("Liquipedia 上没有这个选手（可能刚更新过），请刷新页面再看");
  let name = "";
  await updateDoc(bucket, "manual/players.json", () => ({ version: 1, items: [] }), (list) => {
    const p = list.items.find((x) => x.id === playerId);
    if (!p) throw new Refuse("选手库里没有这个人了（可能刚被删了）");
    if (p.liquipedia) throw new Refuse(`「${p.name}」已经填了 Liquipedia 页面名 ${p.liquipedia}`);
    p.liquipedia = page;
    p.updated = now;
    p.updatedBy = by.name;
    p.rev = (p.rev ?? 0) + 1;
    name = p.name;
  });
  await writeLog(bucket, by, now, `确认选手 ${name} 就是 Liquipedia 上的 ${page}`);
}

// 点"不是"：记下来，以后不再建议这一对
export async function rejectMatch(bucket, by, playerId, page, now) {
  const manual = (await manualPlayers(bucket)).find((x) => x.id === playerId);
  if (!manual) throw new Refuse("选手库里没有这个人了（可能刚被删了）");
  await updateDoc(bucket, REJECTS, emptyRejects, (data) => {
    if (!data.items.some((x) => x.player === playerId && x.page === page)) data.items.push({ player: playerId, page, by: by.name, at: now });
  });
  await writeLog(bucket, by, now, `选手 ${manual.name} 不是 Liquipedia 上的 ${page}`);
}

// 页面上"这一轮"的进度、这一小时用了几次
export function progress(s, now) {
  const recent = (s.recent ?? []).filter((t) => now - t < HOUR).length;
  const running = s.queue?.length > 0;
  const left = running ? s.queue.filter((x) => !x.kind.startsWith("plan-")).length : 0;
  const next = !running && s.lastRoundDone ? (s.refresh ? now : s.lastRoundDone + CYCLE) : null;
  return { recent, running, done: s.round?.done ?? 0, total: s.round?.total ?? 0, left, hoursLeft: Math.ceil((left * 3) / 60 * 10) / 10,
           next, lastRoundDone: s.lastRoundDone, errors: s.errors ?? [], refresh: Boolean(s.refresh) };
}

