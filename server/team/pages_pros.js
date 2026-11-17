// 资料：搜职业选手和战队、选手页、战队页（2026-11-18 第 4 步）。数据是定时从 Liquipedia 抓的（lpsync.js），
// 有手动修正的项换成修正的值、标出来（overrides.js）。只读的人能看；编辑能改、撤掉修正。外框在 pages.js。

import { can } from "./auth.js";
import { beijingText, squash } from "./esports.js";
import { FIELDS } from "./overrides.js";
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
      ${linked.length ? `<span class="badge">选手库：${escape(linked.map((m) => m.name).join("、"))}</span>` : ""}</div>
    <div class="row-meta">${p.name ? `<span>${escape(p.name)}</span>` : ""}${p.team ? `<span>${teamLink(p, knownTeams)}</span>` : '<span>现在没有队</span>'}
      ${p.nats?.length ? `<span>${escape(p.nats.join("、"))}</span>` : ""}${p.results ? `<span>${p.results} 场成绩</span>` : ""}</div></div></li>`;
}

// ---------- 搜索、名单 ----------

// players：合并了修正的选手（不含 missing）；teams：战队资料；linkedBy：页面名 → 选手库里连着的人
export function prosPage(me, { q, players, teams, linkedBy, now }) {
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
  <p><a href="/liquipedia">收录的比赛、抓取进度、待确认 ›</a></p></div>
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

export function proPage(me, { p, squads, results, linked, knownTeams, now, error = "", ok = "" }) {
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

export function teamProfilePage(me, { t, current, past, now }) {
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
<section class="card"><h2>现役选手（${current.length}）</h2>
  ${current.length ? `<ul class="list">${current.map((p) => person(p)).join("")}</ul>` : '<p class="muted">收录的选手里没有现在在这支队的。</p>'}</section>
<section class="card"><h2>待过的选手（${past.length}）</h2>
  <p class="small muted">只算我们抓了资料的选手（收录的比赛里的人、选手库里连上的人）。</p>
  ${past.length ? `<ul class="list">${past.map(({ p, s }) => person(p, `<span>${escape(s.join || "？")} – ${escape(s.leave || "？")}</span>`)).join("")}</ul>` : '<p class="muted">没有。</p>'}</section>` });
}
