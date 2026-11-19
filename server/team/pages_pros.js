// 资料：搜职业选手和战队、选手页、战队页（2026-11-18 第 4 步）。数据是定时从 Liquipedia 抓的（lpsync.js），
// 有手动修正的项换成修正的值、标出来（overrides.js）。只读的人能看；编辑能改、撤掉修正。外框在 pages.js。

import { can } from "./auth.js";
import { beijingText, squash } from "./esports.js";
import { FIELDS } from "./overrides.js";
import { STATUSES } from "./manualpros.js";
import { SITE } from "./liquipedia.js";
import { nameKey } from "./lpsync.js";
import { ago, escape, layout } from "./pages.js";

const lpUrl = (page) => SITE + String(page).split("/").map(encodeURIComponent).join("/");
export const proHref = (page) => `/pros/${encodeURIComponent(page)}`;
export const teamHref = (page) => `/teams/${encodeURIComponent(page)}`;
const TIERS = { 1: "S 级", 2: "A 级", 3: "B 级", 4: "C 级", 5: "D 级" };
const STATUS = { active: "现役", inactive: "不活跃", retired: "已退役", banned: "禁赛", passed: "已故" };

function notes({ error = "", ok = "" }) {
  return (error ? `<div class="note note-error" role="alert">${escape(error)}</div>` : "")
    + (ok ? `<p class="note note-ok" role="status">${escape(ok)}</p>` : "");
}

function money(n) {
  return n ? `$${Math.round(n).toLocaleString("en-US")}` : "";
}

// 生日 → 几岁（北京时间的今天）
function age(birth, now) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(birth ?? "");
  if (!m) return "";
  const today = new Date(now + 8 * 3600 * 1000);
  let years = today.getUTCFullYear() - Number(m[1]);
  if (today.getUTCMonth() + 1 < Number(m[2]) || (today.getUTCMonth() + 1 === Number(m[2]) && today.getUTCDate() < Number(m[3]))) years--;
  return years > 0 && years < 100 ? `${years} 岁` : "";
}

function place(text) {
  const t = String(text ?? "").replace(/\s/g, "");
  return /^\d+(-\d+)?$/.test(t) ? `第 ${t.replace("-", "–")} 名` : t;
}

const fixedBadge = (p, field) => (p.fixed?.[field] ? '<span class="badge badge-upcoming">手动修正</span>' : "");

function teamLink(p, knownTeams) {
  if (!p.team) return "";
  const page = p.fixed?.team ? null : p.teamPage;
  return page && knownTeams.has(page) ? `<a href="${teamHref(page)}">${escape(p.team)}</a>` : escape(p.team);
}

function playerRow(p, linked, knownTeams) {
  return `<li class="row"><div class="row-main">
    <div class="row-name"><a href="${proHref(p.page)}">${escape(p.id)}</a>${Object.keys(p.fixed ?? {}).length ? '<span class="badge badge-upcoming">有修正</span>' : ""}
      ${p.manual ? '<span class="badge">手动建的</span>' : ""}
      ${linked.length ? `<span class="badge">选手库：${escape(linked.map((m) => m.name).join("、"))}</span>` : ""}</div>
    <div class="row-meta">${p.name ? `<span>${escape(p.name)}</span>` : ""}${p.team ? `<span>${teamLink(p, knownTeams)}</span>` : '<span>现在没有队</span>'}
      ${p.nats?.length ? `<span>${escape(p.nats.join("、"))}</span>` : ""}${p.results ? `<span>${p.results} 场成绩</span>` : ""}</div></div></li>`;
}

// ---------- 搜索、名单 ----------

// players：合并了修正的选手（不含 missing）；teams：战队资料；linkedBy：页面名 → 选手库里连着的人
export function prosPage(me, { q, players, teams, linkedBy, now, ok = "" }) {
  const query = squash(q);
  const key = nameKey(query);
  const knownTeams = new Set(teams.map((t) => t.page));
  const hit = (...texts) => !key || texts.some((t) => nameKey(t).includes(key));
  const foundPlayers = players.filter((p) => hit(p.id, p.name, p.localName, p.team, p.page))
    .sort((a, b) => (a.team || "～").localeCompare(b.team || "～") || a.id.localeCompare(b.id));
  const members = (page) => players.filter((p) => p.teamPage === page && !p.fixed?.team).length;
  const foundTeams = teams.filter((t) => hit(t.name, t.page)).sort((a, b) => members(b.page) - members(a.page) || a.name.localeCompare(b.name));
  const teamRows = foundTeams.map((t) => `<li class="row"><div class="row-main">
    <div class="row-name"><a href="${teamHref(t.page)}">${escape(t.name)}</a>${t.status === "disbanded" ? '<span class="badge">已解散</span>' : ""}</div>
    <div class="row-meta">${t.region ? `<span>${escape(t.region)}</span>` : ""}<span>收录了 ${members(t.page)} 位现役选手</span></div></div></li>`).join("");
  return layout({ title: "资料", me, current: "pros", body: `
<div class="stack"><h1>资料</h1>
  <p class="muted">职业选手和战队的资料、经历、成绩，定时从 Liquipedia 抓（数据来自 <a href="https://liquipedia.net/apexlegends/" target="_blank" rel="noopener noreferrer">Liquipedia</a>，CC BY-SA 3.0）。
    Liquipedia 还没更新的，可以在选手页上手动修正。</p>
  <p class="links"><a href="/liquipedia">收录的比赛、抓取进度、待确认 ›</a>${can(me, "edit") ? '<a href="/pros/new">Liquipedia 上没有的选手：手动建一份资料 ›</a>' : ""}</p></div>
${notes({ ok })}
<form class="search" method="get" action="/pros" role="search">
  <label class="field">搜选手、战队<span class="help">ID、真名、战队名都行，不管大小写</span>
    <span class="search-row"><input class="input" type="search" name="q" value="${escape(query)}" autocomplete="off" autocapitalize="off" spellcheck="false">
      <button class="btn btn-primary" type="submit">搜</button></span></label>
</form>
${query && !foundPlayers.length && !foundTeams.length ? `<p class="muted">没找到「${escape(query)}」。还没抓到的人可以在"收录的比赛"里加上他打过的比赛。</p>` : ""}
${foundPlayers.length ? `<section class="card"><h2>选手（${foundPlayers.length}）</h2>
  <ul class="list">${foundPlayers.map((p) => playerRow(p, linkedBy.get(p.page) ?? [], knownTeams)).join("")}</ul></section>` : ""}
${foundTeams.length ? `<section class="card"><h2>战队（${foundTeams.length}）</h2><ul class="list">${teamRows}</ul></section>` : ""}
${!players.length && !teams.length ? `<p class="muted">还没抓到资料：先在<a href="/liquipedia">收录的比赛</a>里加一场比赛。</p>` : ""}` });
}

// ---------- 选手页 ----------

function fieldRow(me, p, field, now) {
  const value = field === "nats" ? (p.nats ?? []).join("、") : field === "birth" ? p.birth : p[field];
  const fix = p.fixed?.[field];
  const editable = can(me, "edit");
  const label = field === "status" && !fix ? STATUS[value] ?? value : value;
  const shown = field === "team" ? (value ? escape(value) : '<span class="muted">现在没有队</span>') : label ? escape(label) : '<span class="muted">没写</span>';
  const source = fix ? `<div class="small muted">手动修正：${escape(fix.by)} ${escape(ago(fix.at, now))}改的${fix.reason ? `（${escape(fix.reason)}）` : ""}；
      Liquipedia 写的是「${escape(fix.original || "（空）")}」${fix.conflict ? '<span class="badge badge-warn">Liquipedia 后来改了，在待确认里</span>' : ""}</div>` : "";
  const edit = editable ? `<div class="row-actions">
    <details class="act"><summary class="btn">改</summary>
      <form class="panel" method="post" action="${proHref(p.page)}/fix"><input type="hidden" name="field" value="${field}">
        <label class="field">${FIELDS[field]}<input class="input" name="value" value="${escape(value ?? "")}" maxlength="80" autocomplete="off"
          ${field === "birth" ? 'placeholder="1999-05-20"' : ""}></label>
        <label class="field">原因<span class="help">比如：11-18 改 ID 了，Liquipedia 还没更新</span><input class="input" name="reason" maxlength="100" autocomplete="off"></label>
        <button class="btn btn-primary" type="submit">保存修正</button></form></details>
    ${fix ? `<form method="post" action="${proHref(p.page)}/unfix"><input type="hidden" name="field" value="${field}">
      <button class="btn" type="submit">撤掉修正</button></form>` : ""}</div>` : "";
  return `<li class="row"><div class="row-main"><div class="row-meta"><span>${FIELDS[field]}</span>${fixedBadge(p, field)}</div>
    <div class="row-name">${shown}</div>${source}</div>${edit}</li>`;
}

// shadowed：以前手动建过这个人的资料，现在 Liquipedia 有页面了（用 Liquipedia 的，那一份留着不显示）
export function proPage(me, { p, squads, results, linked, knownTeams, now, error = "", ok = "", shadowed = null }) {
  const facts = [p.nats?.length ? escape(p.nats.join("、")) : "", age(p.birth, now), p.input ? escape(p.input) : "",
                 p.status && p.status !== "active" ? escape(STATUS[p.status] ?? p.status) : ""].filter(Boolean);
  const stints = (squads ?? []).filter((s) => !s.type || s.type === "player").map((s) => `<li class="row"><div class="row-main">
    <div class="row-name">${knownTeams.has(s.teamPage) ? `<a href="${teamHref(s.teamPage)}">${escape(s.team)}</a>` : escape(s.team)}
      ${!s.leave && s.status === "active" ? '<span class="badge badge-live">现在</span>' : ""}</div>
    <div class="row-meta"><span>${escape(s.join || "？")} – ${escape(s.leave || (s.status === "active" ? "至今" : "？"))}</span>${s.role ? `<span>${escape(s.role)}</span>` : ""}</div></div></li>`).join("");
  const rows = [...(results ?? [])].sort((a, b) => (b.date || "").localeCompare(a.date || ""));
  const resultRow = (r) => `<li class="row"><div class="row-main">
    <div class="row-name"><span class="place">${escape(place(r.place))}</span>${r.tournament ? escape(r.tournament) : `<a href="${escape(lpUrl(r.event))}" target="_blank" rel="noopener noreferrer">${escape(r.event)}</a>`}</div>
    <div class="row-meta"><span>${escape(r.date)}</span>${TIERS[r.tier] ? `<span class="badge">${TIERS[r.tier]}</span>` : ""}${r.tierType ? `<span>${escape(r.tierType)}</span>` : ""}
      ${r.team ? `<span>${escape(r.team)}</span>` : ""}${r.prize ? `<span>全队奖金 ${money(r.prize)}</span>` : ""}</div></div></li>`;
  const first = rows.slice(0, 15).map(resultRow).join("");
  const rest = rows.slice(15).map(resultRow).join("");
  return layout({ title: p.id, me, current: "pros", body: `
<p class="small"><a href="/pros">‹ 资料</a></p>
${notes({ error, ok })}
<section class="card stack">
  <div class="title-row"><h1>${escape(p.id)}</h1>${fixedBadge(p, "id")}</div>
  ${p.name || p.localName ? `<p>${escape([p.name, p.localName].filter(Boolean).join(" · "))}</p>` : ""}
  <p>${p.team ? `现在在 ${teamLink(p, knownTeams)}` : "现在没有队"}${facts.length ? ` · ${facts.join(" · ")}` : ""}</p>
  ${p.legends?.length ? `<p class="small muted">常用传奇：${escape(p.legends.join("、"))}</p>` : ""}
  ${p.earnings ? `<p class="small muted">累计奖金（按 Liquipedia）：${money(p.earnings)}</p>` : ""}
  <p class="small"><a href="${escape(lpUrl(p.page))}" target="_blank" rel="noopener noreferrer">在 Liquipedia 上看</a>
    ${linked.length ? ` · 选手库里是 ${linked.map((m) => `「${escape(m.name)}」（${escape(m.team)}）`).join("、")}` : " · 选手库里没有连上他"}</p>
  <p class="small muted">资料 ${escape(ago(p.fetchedAt, now))}从 Liquipedia 抓的（${escape(beijingText(p.fetchedAt))}）。</p>
  ${shadowed ? `<div class="note note-info small">${escape(shadowed.createdBy)} 以前手动建过他的资料，现在 Liquipedia 有这个页面了，用的是 Liquipedia 的。
    ${can(me, "edit") ? `<form method="post" action="${proHref(p.page)}/delete"><button class="btn btn-small" type="submit">删掉手动建的那份</button></form>` : ""}</div>` : ""}
</section>
<section class="card"><h2>资料</h2>
  <p class="small muted">Liquipedia 还没更新的（比如改了 ID），点"改"手动修正：页面上用你们改的，Liquipedia 以后改成一样的，修正会自动撤掉；改成别的，会放进待确认。</p>
  <ul class="list">${Object.keys(FIELDS).map((field) => fieldRow(me, p, field, now)).join("")}</ul></section>
<section class="card"><h2>战队经历</h2>${stints ? `<ul class="list">${stints}</ul>` : '<p class="muted">Liquipedia 上没有记。</p>'}</section>
<section class="card"><h2>比赛成绩（${rows.length}）</h2>
  ${rows.length ? `<ul class="list">${first}</ul>${rest ? `<details class="more"><summary class="btn">再看 ${rows.length - 15} 场</summary><ul class="list">${rest}</ul></details>` : ""}`
    : '<p class="muted">还没有成绩（或者还没抓到）。</p>'}</section>` });
}

// ---------- 战队页 ----------

export function teamProfilePage(me, { t, current, past, now, inRoster = new Set() }) {
  const facts = [t.region, t.status === "disbanded" ? `已解散${t.disbanded ? `（${t.disbanded}）` : ""}` : "",
                 t.created ? `${t.created} 成立` : "", t.earnings ? `累计奖金 ${money(t.earnings)}` : ""].filter(Boolean);
  const person = (p, extra = "") => `<li class="row"><div class="row-main">
    <div class="row-name"><a href="${proHref(p.page)}">${escape(p.id)}</a>${p.name ? `<span class="muted small">${escape(p.name)}</span>` : ""}</div>
    <div class="row-meta">${p.nats?.length ? `<span>${escape(p.nats.join("、"))}</span>` : ""}${extra}</div></div></li>`;
  return layout({ title: t.name, me, current: "pros", body: `
<p class="small"><a href="/pros">‹ 资料</a></p>
<section class="card stack"><h1>${escape(t.name)}</h1>
  ${facts.length ? `<p>${facts.map(escape).join(" · ")}</p>` : ""}
  <p class="small"><a href="${escape(lpUrl(t.page))}" target="_blank" rel="noopener noreferrer">在 Liquipedia 上看</a></p>
  <p class="small muted">资料 ${escape(ago(t.fetchedAt, now))}从 Liquipedia 抓的。</p></section>
<section class="card stack"><h2>现役选手（${current.length}）</h2>
  ${current.length ? `<ul class="list">${current.map((p) => person(p, inRoster.has(p.page) ? '<span class="badge">已在选手库</span>' : "")).join("")}</ul>`
    : '<p class="muted">收录的选手里没有现在在这支队的。</p>'}
  ${can(me, "edit") && current.some((p) => !inRoster.has(p.page)) ? `<form method="post" action="${teamHref(t.page)}/to-roster">
    <button class="btn btn-primary" type="submit">把现役选手加进选手库（${current.filter((p) => !inRoster.has(p.page)).length} 人）</button></form>
  <p class="small muted">选手名、队名、Liquipedia 页面名自动填好；Liquipedia 上有斗鱼、虎牙、B站、抖音直播间的，直播间也填好，没有的标"直播间待填"。
    选手库里已经有的跳过。队名想用中文名，加完在选手库里"改队名"。</p>` : ""}</section>
<section class="card"><h2>待过的选手（${past.length}）</h2>
  <p class="small muted">只算我们抓了资料的选手（收录的比赛里的人、选手库里连上的人）。</p>
  ${past.length ? `<ul class="list">${past.map(({ p, s }) => person(p, `<span>${escape(s.join || "？")} – ${escape(s.leave || "？")}</span>`)).join("")}</ul>` : '<p class="muted">没有。</p>'}</section>` });
}

// ---------- 手动建的选手资料（2026-11-19 用户定的：Liquipedia 上没有页面的选手，管理员、编辑能手动建）----------

const statusOptions = (selected) => Object.entries(STATUSES).map(([k, v]) => `<option value="${k}"${k === (selected || "active") ? " selected" : ""}>${v}</option>`).join("");

function proFields(x) {
  return `<label class="field">ID<span class="help">比赛里用的名字，比如 Dokiovo</span>
      <input class="input" name="id" value="${escape(x.id)}" maxlength="40" required autocomplete="off" autocapitalize="off" spellcheck="false"></label>
    <div class="two">
      <label class="field">真名<span class="help">不知道就空着</span><input class="input" name="name" value="${escape(x.name)}" maxlength="80" autocomplete="off"></label>
      <label class="field">现在的队<span class="help">没有队就空着</span><input class="input" name="team" value="${escape(x.team)}" maxlength="80" autocomplete="off"></label>
      <label class="field">国籍<span class="help">好几个用顿号隔开，比如 中国、加拿大</span><input class="input" name="nats" value="${escape((x.nats ?? []).join("、"))}" maxlength="80" autocomplete="off"></label>
      <label class="field">生日<span class="help">写成 1999-05-20，不知道就空着</span><input class="input" name="birth" value="${escape(x.birth)}" maxlength="10" placeholder="1999-05-20" autocomplete="off"></label>
      <label class="field">输入设备<span class="help">比如 键鼠、手柄</span><input class="input" name="input" value="${escape(x.input)}" maxlength="80" autocomplete="off"></label>
      <label class="field">状态<select class="input" name="status">${statusOptions(x.status)}</select></label>
    </div>`;
}

// 新建：draft 是从待确认带过来的（page、link、team）或者出错时填的
export function newProPage(me, { draft = {}, playerNames = [], error = "" }) {
  const fromPending = Boolean(draft.page);
  return layout({ title: "手动建选手资料", me, current: "pros", narrow: true, body: `
<p class="small"><a href="${fromPending ? "/liquipedia#confirm" : "/pros"}">‹ ${fromPending ? "待确认" : "资料"}</a></p>
<div class="stack"><h1>手动建一份选手资料</h1>
  <p class="muted">给 Liquipedia 上没有页面的选手建一份资料，资料页里能搜到、标"手动建的"。以后 Liquipedia 有了他的页面，就改用 Liquipedia 的。</p></div>
${notes({ error })}
<form class="card stack" method="post" action="/pros/new">
  ${fromPending ? `<input type="hidden" name="page" value="${escape(draft.page)}">
    <p class="small muted">选手库里填的页面名是「${escape(draft.page)}」：建好以后就连上了，待确认里那一条也就没了。</p>` : ""}
  ${proFields({ id: draft.id ?? draft.link ?? "", name: draft.name ?? "", team: draft.team ?? "", nats: draft.nats ? String(draft.nats).split("、") : [],
                birth: draft.birth ?? "", input: draft.input ?? "", status: draft.status ?? "active" })}
  <label class="field">连上选手库里的谁<span class="help">可以空着；选了的话，他的 Liquipedia 页面名填成这份资料（已经填了别的就不动）</span>
    <input class="input" name="link" value="${escape(draft.link ?? "")}" list="player-names" maxlength="40" autocomplete="off"></label>
  <datalist id="player-names">${playerNames.map((n) => `<option value="${escape(n)}">`).join("")}</datalist>
  <div><button class="btn btn-primary" type="submit">建好</button></div>
</form>` });
}

// 手动建的那一份的选手页：资料、谁建的、改、删
export function manualProPage(me, { p, item, linked, now, error = "", ok = "" }) {
  const editable = can(me, "edit");
  const facts = [p.nats?.length ? escape(p.nats.join("、")) : "", age(p.birth, now), p.input ? escape(p.input) : "",
                 p.status && p.status !== "active" ? escape(STATUSES[p.status] ?? p.status) : ""].filter(Boolean);
  return layout({ title: p.id, me, current: "pros", body: `
<p class="small"><a href="/pros">‹ 资料</a></p>
${notes({ error, ok })}
<section class="card stack">
  <div class="title-row"><h1>${escape(p.id)}</h1><span class="badge">手动建的</span></div>
  ${p.name ? `<p>${escape(p.name)}</p>` : ""}
  <p>${p.team ? `现在在 ${escape(p.team)}` : "现在没有队"}${facts.length ? ` · ${facts.join(" · ")}` : ""}</p>
  <p class="small">Liquipedia 上没有这个页面（${escape(p.page)}）${linked.length ? ` · 选手库里是 ${linked.map((m) => `「${escape(m.name)}」（${escape(m.team)}）`).join("、")}` : " · 选手库里没有连上他"}</p>
  <p class="small muted">${escape(item.createdBy)} ${escape(ago(item.created, now))}手动建的${item.updated !== item.created ? `；${escape(item.updatedBy)} ${escape(ago(item.updated, now))}改过` : ""}。</p>
</section>
${editable ? `<details class="card fold"${error ? " open" : ""}><summary class="fold-head"><span>改资料</span></summary>
  <form class="stack" method="post" action="${proHref(p.page)}/edit"><input type="hidden" name="rev" value="${item.rev}">
    ${proFields(item)}
    <div><button class="btn btn-primary" type="submit">保存</button></div></form></details>
<section class="card stack"><h2>删掉这份资料</h2>
  <p class="small muted">删了以后资料页里就没有他了；选手库里填的页面名不动（待确认里会重新列出来"Liquipedia 上没有这个页面"）。</p>
  <details class="act"><summary class="btn btn-danger">删除</summary><div class="panel">
    <p class="small">删掉「${escape(p.id)}」的资料？删了找不回来。</p>
    <form method="post" action="${proHref(p.page)}/delete"><button class="btn btn-danger btn-wide" type="submit">确定删除</button></form></div></details>
</section>` : ""}` });
}
