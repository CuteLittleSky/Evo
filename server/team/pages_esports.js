// 选手库、赛程、发布、改动记录这几页（2026-11-17 第 2 步）。外框、颜色在 pages.js。
// 只读的人能看，看不到改的按钮；编辑能改；能发布的人（管理员、开了发布的编辑）才有发布按钮；平台开关只有管理员。

import { can } from "./auth.js";
import { EVENT_COLUMNS, MAIN_MAX, NO_STREAM, NO_STREAM_NAME, PLATFORMS, PLAYER_COLUMNS, STATUS_WORDS, TYPES, beijingText, checkPlayers, eventLabel, isoBeijing,
  parseBeijing, parseIso, roomUrl, status } from "./esports.js";
import { teamsOf } from "./data.js";
import { PUBLIC_URL, TARGETS, describe } from "./publish.js";
import { ago, escape, layout, logList, noteList } from "./pages.js";
import { PUBLIC_LOGO_URL } from "./logos.js";
import { logoBox, logoHref } from "./pages_logos.js";

function notes({ error = "", ok = "" }) {
  return (error ? `<div class="note note-error" role="alert">${escape(error).replace(/\n/g, "<br>")}</div>` : "")
    + (ok ? `<p class="note note-ok" role="status">${escape(ok)}</p>` : "");
}

function by(item, now) {
  return `${escape(item.updatedBy ?? item.createdBy ?? "")} ${escape(ago(item.updated ?? item.created, now))}改的`;
}

function confirmDelete(action, what) {
  return `<details class="act"><summary class="btn btn-danger">删除</summary>
    <div class="panel"><p class="small">删掉「${escape(what)}」？删了找不回来，要的话得重新添加。</p>
      <form method="post" action="${action}"><button class="btn btn-danger btn-wide" type="submit">确定删除</button></form></div></details>`;
}

// ---------- 选手库 ----------

function platformOptions(selected) {
  return Object.entries(PLATFORMS).map(([key, p]) => `<option value="${key}"${key === selected ? " selected" : ""}>${p.name}</option>`).join("");
}

// 选手的平台多一个"不直播"（主舞台不能选它）；从 Liquipedia 加进来、直播间还没填的，先显示"请选平台"
function playerPlatformOptions(selected) {
  return (selected ? "" : '<option value="" selected>请选平台</option>') + platformOptions(selected)
    + `<option value="${NO_STREAM}"${selected === NO_STREAM ? " selected" : ""}>${NO_STREAM_NAME}</option>`;
}

function playerForm(action, p, teamNames, submit) {
  return `<form class="stack" method="post" action="${action}">
    ${p.rev ? `<input type="hidden" name="rev" value="${p.rev}">` : ""}
    <label class="field">选手<span class="help">直播间里的名字，比如 Kite</span>
      <input class="input" name="name" value="${escape(p.name)}" maxlength="40" required autocomplete="off"></label>
    <label class="field">战队<span class="help">从下拉里选已有的队，或者写一个新队名</span>
      <input class="input" name="team" value="${escape(p.team)}" maxlength="40" required list="team-names" autocomplete="off"></label>
    <div class="two">
      <label class="field">平台<span class="help">不开直播的选"不直播"</span><select class="input" name="platform" required>${playerPlatformOptions(p.platform)}</select></label>
      <label class="field">房间号<span class="help">直播间地址最后那一段：一般是数字；抖音也可以是主播的抖音号（字母、数字、下划线）。不直播的空着</span>
        <input class="input" name="room" value="${escape(p.room)}" maxlength="24" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
    </div>
    <label class="field">Liquipedia 页面名<span class="help">可以空着；贴 Liquipedia 选手页的网址就行，会自动存成页面名（比如 Panic_(American_player)）</span>
      <input class="input" name="liquipedia" value="${escape(p.liquipedia)}" maxlength="200" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
    <div><button class="btn btn-primary" type="submit">${submit}</button></div>
  </form>`;
}

function importBox(action, columns, example, draft = "") {
  return `<details class="card fold"${draft ? " open" : ""}><summary class="fold-head"><span>从 Excel 粘贴导入</span><span class="muted small">一次加很多行</span></summary>
  <form class="stack" method="post" action="${action}">
    <p class="small muted">在 Excel 或 WPS 里选中表格（带不带表头都行），复制，粘贴到下面。列的顺序：${columns.map(escape).join("、")}。
      名字已经有的会更新，没有的新加；有一行不对就整批都不存，会告诉你是第几行。</p>
    <textarea class="input mono" name="table" rows="6" required placeholder="${escape(example)}">${escape(draft)}</textarea>
    <div><button class="btn btn-primary" type="submit">导入</button></div>
  </form></details>`;
}

// 直播间待填的人按队列出来，默认收起（不挡发布：发布时按"不直播"算）
function pendingRooms(items) {
  const byTeam = new Map();
  for (const p of items.filter((x) => !x.platform)) {
    if (!byTeam.has(p.team)) byTeam.set(p.team, []);
    byTeam.get(p.team).push(p.name);
  }
  const count = [...byTeam.values()].reduce((n, names) => n + names.length, 0);
  if (!count) return "";
  return `<details class="note note-info"><summary><span>${count} 位选手直播间待填<span class="help">（${byTeam.size} 支队；不补也能发布，发布时按"不直播"算）</span></span></summary>
    <ul class="plain team-pend">${[...byTeam].map(([team, names]) => `<li><b>${escape(team)}</b>：${names.map(escape).join("、")}</li>`).join("")}</ul>
    <p><a class="btn" href="/players/rooms">在一张表里填 ›</a></p></details>`;
}

// ---------- 一张表批量填直播间（2026-11-19 用户要的）----------
// 默认只列直播间待填的人，可以切到全部；按队分组。只存改过的，有一个不对整批不存（data.js 的 saveRooms）。
// draft：保存出错时填的样子（form 原样），照它显示
export function roomsPage(me, items, { now, all = false, error = "", ok = "", draft = null, banner = "" }) {
  const editable = can(me, "edit");
  const pendingCount = items.filter((p) => !p.platform).length;
  const shown = all ? items : items.filter((p) => !p.platform || (draft && Object.hasOwn(draft, `p_${p.id}`)));
  const teams = [...new Set(shown.map((p) => p.team))];
  const value = (p, key, current) => (draft && Object.hasOwn(draft, `${key}_${p.id}`) ? draft[`${key}_${p.id}`] : current);
  const row = (p) => {
    const platform = value(p, "p", p.platform);
    const room = value(p, "r", p.room);
    return `<li class="bulk-row">
      <div class="bulk-who"><b>${escape(p.name)}</b>${p.platform ? "" : '<span class="badge badge-warn">待填</span>'}
        ${p.platform && p.platform !== NO_STREAM ? `<a class="small" href="${escape(roomUrl(p.platform, p.room))}" target="_blank" rel="noopener noreferrer">打开直播间</a>` : ""}</div>
      <input type="hidden" name="v_${p.id}" value="${p.rev ?? ""}">
      <select class="input" name="p_${p.id}" aria-label="${escape(p.name)} 的平台"${editable ? "" : " disabled"}>
        ${platform ? "" : '<option value="" selected>直播间待填</option>'}${platformOptions(platform)}
        <option value="${NO_STREAM}"${platform === NO_STREAM ? " selected" : ""}>${NO_STREAM_NAME}</option></select>
      <input class="input" name="r_${p.id}" value="${escape(room)}" placeholder="房间号" aria-label="${escape(p.name)} 的房间号" maxlength="24"
        autocomplete="off" autocapitalize="off" spellcheck="false"${editable ? "" : " disabled"}>
    </li>`;
  };
  const groups = teams.map((team) => `<section class="bulk-team"><h3>${escape(team)}</h3>
    <ul class="list bulk-list">${shown.filter((p) => p.team === team).map(row).join("")}</ul></section>`).join("");
  const tabs = `<nav class="tabs" aria-label="列哪些人">
    <a href="/players/rooms"${all ? "" : ' aria-current="page"'}>直播间待填的（${pendingCount}）</a>
    <a href="/players/rooms?all=1"${all ? ' aria-current="page"' : ""}>全部（${items.length}）</a></nav>`;
  return layout({ title: "批量填直播间", me, current: "players", banner, body: `
<p class="small"><a href="/players">‹ 选手库</a></p>
<div class="stack"><h1>批量填直播间</h1>
  <p class="muted">一张表里改很多人的平台和房间号，点一次保存。房间号是直播间地址最后那一段：一般是数字；抖音也可以是主播的抖音号。不开直播的选"不直播"，房间号空着。
    只存改过的；有一个人不对就整批都不存，会说是谁。</p></div>
${notes({ error, ok })}
${tabs}
${shown.length ? `<form class="stack" method="post" action="/players/rooms">${all ? '<input type="hidden" name="all" value="1">' : ""}
  ${groups}
  ${editable ? `<div class="bulk-bar"><button class="btn btn-primary" type="submit">保存改过的</button>
    <span class="small muted">改了还没保存的，换页、刷新就没了</span></div>` : ""}
</form>` : `<p class="muted">${all ? "选手库里还没有人。" : "没有直播间待填的人了。"}</p>`}` });
}

export function playersPage(me, items, { now, error = "", ok = "", draft = null, draftTable = "", logos = {}, banner = "" }) {
  const editable = can(me, "edit");
  const teams = teamsOf(items);
  const problems = checkPlayers(items).errors;
  const datalist = `<datalist id="team-names">${[...teams.keys()].map((t) => `<option value="${escape(t)}">`).join("")}</datalist>`;
  const blank = { name: "", team: "", platform: "douyu", room: "", liquipedia: "" };
  const add = editable ? `<details class="card fold"${!items.length || draft ? " open" : ""}><summary class="fold-head"><span>添加选手</span><span class="muted small">一个一个加</span></summary>
    ${playerForm("/players/add", draft ? { ...blank, ...draft, rev: undefined } : blank, teams, "添加")}</details>` : "";
  const sections = [...teams.keys()].map((team) => {
    const members = items.filter((p) => p.team === team);
    const size = `<span class="badge">${members.length} 人</span>`;  // 一支队几个人不限（11-18 用户定的）
    const rename = editable ? `<details class="act"><summary class="btn">改队名</summary>
      <form class="panel" method="post" action="/teams/rename"><input type="hidden" name="from" value="${escape(team)}">
        <label class="field">新队名<span class="help">选手库和赛程里一起改</span>
          <input class="input" name="to" value="${escape(team)}" maxlength="40" required></label>
        <button class="btn btn-primary" type="submit">改名</button></form></details>` : "";
    const others = [...teams.keys()].filter((t) => t !== team);
    // 两支队并成一支（11-19 用户要的）：这一队的人都挪过去，赛程跟着改。队名用页面上那一份下拉（datalist），不每个按钮放一份 40 个选项
    const merge = editable && others.length ? `<details class="act"><summary class="btn">并到另一支队</summary>
      <form class="panel" method="post" action="/teams/merge"><input type="hidden" name="from" value="${escape(team)}">
        <label class="field">并进哪一队<span class="help">点输入框从下拉里选，或者打几个字</span>
          <input class="input" name="to" list="team-names" maxlength="40" required autocomplete="off"></label>
        <p class="small muted">「${escape(team)}」的 ${members.length} 位选手都挪过去，赛程里的「${escape(team)}」也换成它（同一场里两支都有的只留一个）；
          那一队没有队徽的话用「${escape(team)}」的。</p>
        <button class="btn btn-primary" type="submit">并过去</button></form></details>` : "";
    // 选手换队（11-19 用户要的）：从下拉里选，或者写一个新队名
    const move = (p) => `<details class="act"><summary class="btn">换队</summary>
      <form class="panel" method="post" action="/players/${p.id}/move"><input type="hidden" name="rev" value="${p.rev}">
        <label class="field">换到哪一队<span class="help">点输入框从下拉里选；选手库里还没有的队，直接写新队名</span>
          <input class="input" name="team" list="team-names" maxlength="40" required autocomplete="off"></label>
        <p class="small muted">只挪「${escape(p.name)}」一个人，赛程不动。整支队改名、并队用队名旁边的按钮。</p>
        <button class="btn btn-primary" type="submit">换过去</button></form></details>`;
    const rows = members.map((p) => `<li class="row">
      <div class="row-main">
        <div class="row-name">${escape(p.name)}</div>
        <div class="row-meta">${!p.platform ? '<span class="badge badge-warn">直播间待填</span>' : p.platform === NO_STREAM ? '<span class="badge">不直播</span>'
          : `<a href="${escape(roomUrl(p.platform, p.room))}" target="_blank" rel="noopener noreferrer">${PLATFORMS[p.platform].name} ${escape(p.room)}</a>`}
          ${p.liquipedia ? `<span>Liquipedia：${escape(p.liquipedia)}</span>` : ""}<span>${by(p, now)}</span></div>
      </div>
      ${editable ? `<div class="row-actions">
        <details class="act"><summary class="btn">改</summary><div class="panel">${playerForm(`/players/${p.id}/save`, p, teams, "保存")}</div></details>
        ${move(p)}
        ${confirmDelete(`/players/${p.id}/delete`, p.name)}</div>` : ""}
    </li>`).join("");
    const logo = logos[team]?.logo;
    const badge = `<a class="logo-link" href="${logoHref(team)}" title="队徽">${logo ? logoBox(PUBLIC_LOGO_URL + logo, team, "small")
      : '<span class="logo-box logo-small logo-empty" aria-hidden="true">＋</span>'}<span class="small">${logo ? "队徽" : "加队徽"}</span></a>`;
    return `<section class="card"><div class="title-row team-head">${badge}<h2>${escape(team)}</h2>${size}<span class="grow"></span>${rename}${merge}</div>
      <ul class="list">${rows}</ul></section>`;
  }).join("");
  return layout({ title: "选手库", me, current: "players", banner, body: `
<div class="stack"><h1>选手库</h1>
  <p class="muted">${items.length} 位选手、${teams.size} 支队。一支队几个人都行（不开直播的平台选"不直播"），一个选手只能在一支队。点平台和房间号能打开直播间核对。</p>
  ${editable && items.length ? '<p class="links"><a href="/players/rooms">在一张表里批量填直播间 ›</a><a href="/logos">一键配队徽 ›</a></p>' : ""}</div>
${notes({ error, ok })}
${noteList("error", `发布前要改好（${problems.length} 处）：`, problems)}
${pendingRooms(items)}
${datalist}${add}
${sections || `<p class="muted">还没有选手。</p>`}
${editable ? importBox("/players/import", PLAYER_COLUMNS, "Kite\t星火电竞\t斗鱼\t520013\t", draftTable) : ""}` });
}

// ---------- 赛程 ----------

function datetimeValue(iso) {
  const ms = parseIso(iso);
  return ms === null ? "" : beijingText(ms).replace(" ", "T");
}

function asIso(text) {
  const ms = parseBeijing(text ?? "");
  return ms === null ? "" : isoBeijing(ms);
}

// 主舞台：播导播视角的直播间（Evo 看直播页队伍栏最上面一栏）。已有的几行 + 空着的两行，最多 8 行
function mainFields(main) {
  const rows = [...main, ...Array.from({ length: Math.min(2, MAIN_MAX - main.length) }, () => ({ platform: "", room: "", name: "" }))];
  const row = (m, i) => `<div class="main-row">
      <select class="input" name="main_platform" aria-label="主舞台第 ${i + 1} 路的平台"><option value="">平台</option>${platformOptions(m.platform)}</select>
      <input class="input" name="main_room" value="${escape(m.room)}" placeholder="房间号" aria-label="主舞台第 ${i + 1} 路的房间号" maxlength="24" autocomplete="off" autocapitalize="off" spellcheck="false">
      <input class="input" name="main_name" value="${escape(m.name)}" placeholder="名字，比如 官方直播" aria-label="主舞台第 ${i + 1} 路的名字" maxlength="40" autocomplete="off"></div>`;
  return `<fieldset><legend>主舞台（可以空着）</legend>
    <span class="help">播导播视角的直播间，Evo 里放在队伍栏最上面。最多 ${MAIN_MAX} 路，不进选手库；第一路在播的话，Evo 默认先看它。空着的行不用管</span>
    ${rows.map(row).join("")}</fieldset>`;
}

function eventForm(action, e, teams, submit) {
  const typeOptions = Object.keys(TYPES).map((t) => `<label class="option option-inline"><input type="radio" name="type" value="${t}"${e.type === t ? " checked" : ""} required><b>${t}</b></label>`).join("");
  const chosen = new Set(e.teams ?? []);
  const missing = [...chosen].filter((t) => !teams.has(t));  // 选手库里已经没有的（删了、改名了）也列出来，免得悄悄丢掉
  const boxes = [...teams.entries(), ...missing.map((t) => [t, 0])].map(([team, n]) => `<label class="pick"><input type="checkbox" name="teams" value="${escape(team)}"${chosen.has(team) ? " checked" : ""}>
      <span>${escape(team)}</span><span class="${n ? "muted" : "warn"} small">${n ? `${n} 人` : "不在选手库里"}</span></label>`).join("");
  return `<form class="stack" method="post" action="${action}">
    ${e.rev ? `<input type="hidden" name="rev" value="${e.rev}">` : ""}
    <label class="field">名称<input class="input" name="name" value="${escape(e.name)}" maxlength="80" required autocomplete="off"></label>
    <fieldset><legend>类型</legend><div class="inline-options">${typeOptions}</div></fieldset>
    <label class="field">阶段<span class="help">可以空着，比如 B 组对 C 组</span>
      <input class="input" name="stage" value="${escape(e.stage)}" maxlength="80" autocomplete="off"></label>
    <div class="two">
      <label class="field">开始（北京时间）<input class="input" type="datetime-local" name="start" value="${datetimeValue(e.start)}" required></label>
      <label class="field">结束（北京时间）<input class="input" type="datetime-local" name="end" value="${datetimeValue(e.end)}" required></label>
    </div>
    ${mainFields(e.main ?? [])}
    <fieldset><legend>参赛队伍</legend><span class="help">从选手库里勾；一般是 20 支</span>
      ${teams.size || missing.length ? `<div class="picks">${boxes}</div>` : `<p class="small warn">选手库里还没有队伍，先去加选手。</p>`}</fieldset>
    <div><button class="btn btn-primary" type="submit">${submit}</button></div>
  </form>`;
}

// copy：点了某一场的"复制"（2026-11-19 用户要的：A 组、B 组，每天的训练赛，队伍和主舞台不用再录一遍），添加表单照它填好
export function eventsPage(me, items, playerItems, { now, error = "", ok = "", draft = null, draftTable = "", banner = "", copy = null }) {
  const editable = can(me, "edit");
  const teams = teamsOf(playerItems);
  const groups = { live: [], upcoming: [], ended: [] };
  for (const e of items) groups[status(e, now)].push(e);
  groups.live.sort((a, b) => parseIso(a.start) - parseIso(b.start));
  groups.upcoming.sort((a, b) => parseIso(a.start) - parseIso(b.start));
  groups.ended.sort((a, b) => parseIso(b.end) - parseIso(a.end));
  // 观战的人（11-19 用户要的）：开直播的选手才能选（选手名用页面上那一份下拉 streamer-names），看的队从这场的参赛队里选
  const nameOf = (pid) => playerItems.find((p) => p.id === pid)?.name ?? "（已删的选手）";
  const streaming = playerItems.filter((p) => p.platform && p.platform !== NO_STREAM);
  const observerRow = (e, o, i) => `<div class="obs-row">
      <input class="input" name="obs_player" value="${escape(o ? nameOf(o.player) : "")}" list="streamer-names" placeholder="选手（开直播的）"
        aria-label="观战的第 ${i + 1} 位选手" maxlength="40" autocomplete="off">
      <select class="input" name="obs_team" aria-label="第 ${i + 1} 位在看哪支队"><option value="">看哪支队</option>
        ${e.teams.map((t) => `<option value="${escape(t)}"${o?.team === t ? " selected" : ""}>${escape(t)}</option>`).join("")}</select></div>`;
  const observersPanel = (e) => {
    const list = e.observers ?? [];
    const rows = [...list.map((o, i) => observerRow(e, o, i)), ...Array.from({ length: 2 }, (_, i) => observerRow(e, null, list.length + i))].join("");
    return `<details class="act"><summary class="btn">观战${list.length ? `（${list.length}）` : ""}</summary><div class="panel panel-wide">
      <form class="stack" method="post" action="/events/${e.id}/observers"><input type="hidden" name="rev" value="${e.rev}">
        <p class="small muted">正在看别的队比赛、开着直播的选手（比如替补、别的队的选手）：放到他看的那支队下面，Evo 看直播页标"观战"。
          换了看谁就在这里改；保存以后要再发布一次，Evo 最慢 5 分钟更新。清空一行就是去掉他。</p>
        ${rows}
        <div><button class="btn btn-primary" type="submit">保存观战</button></div></form></div></details>`;
  };
  const row = (e) => {
    const old = now - parseIso(e.end) > 7 * 24 * 3600 * 1000;
    return `<li class="row">
    <div class="row-main">
      <div class="row-name"><span class="badge badge-${status(e, now)}">${STATUS_WORDS[status(e, now)]}</span>${escape(e.name)}</div>
      <div class="row-meta"><span>${escape(e.type)}${e.stage ? ` · ${escape(e.stage)}` : ""}</span>
        <span>${escape(beijingText(parseIso(e.start)))}–${escape(beijingText(parseIso(e.end)).slice(11))}</span>
        <span>${e.teams.length} 支队</span>${e.main?.length ? `<span>主舞台 ${e.main.length} 路</span>` : ""}
        ${e.observers?.length ? `<span>观战：${e.observers.map((o) => `${escape(nameOf(o.player))} 看 ${escape(o.team)}`).join("、")}</span>` : ""}
        ${old ? '<span class="badge">结束超过 7 天，不会发布</span>' : ""}<span>${by(e, now)}</span></div>
    </div>
    ${editable ? `<div class="row-actions">
      <details class="act"><summary class="btn">改</summary><div class="panel panel-wide">${eventForm(`/events/${e.id}/save`, e, teams, "保存")}</div></details>
      ${status(e, now) === "ended" ? "" : observersPanel(e)}
      <a class="btn" href="/events?copy=${e.id}#add" title="照这一场填好添加表单（队伍、主舞台都带上）">复制</a>
      ${confirmDelete(`/events/${e.id}/delete`, e.name)}</div>` : ""}
  </li>`;
  };
  const section = (key, title) => (groups[key].length ? `<section class="card"><h2>${title}（${groups[key].length}）</h2>
    <ul class="list">${groups[key].map(row).join("")}</ul></section>` : "");
  const blank = { name: "", type: "联赛", stage: "", start: "", end: "", teams: [] };
  // 添加出错时留着填的内容（时间是网页上的 2026-11-17T19:00，换成文件里的写法再显示）
  const kept = draft ? { ...blank, ...draft, rev: undefined, start: asIso(draft.start), end: asIso(draft.end),
                          main: (draft.main ?? []).filter((m) => m.platform || m.room || m.name).slice(0, MAIN_MAX) }
    : copy ? { name: copy.name, type: copy.type, stage: copy.stage, start: copy.start, end: copy.end, teams: [...copy.teams], main: [...(copy.main ?? [])] }
      : blank;
  const copied = copy && !draft ? `<p class="note note-info">照${escape(eventLabel(copy))}填好了（队伍、主舞台都带上了）：改一下阶段、时间，再点下面的"添加"。</p>` : "";
  const add = editable ? `<details class="card fold" id="add"${!items.length || draft || copy ? " open" : ""}><summary class="fold-head"><span>添加赛事</span><span class="muted small">联赛、训练赛、杯赛</span></summary>
    <div class="stack">${copied}${eventForm("/events/add", kept, teams, "添加")}</div></details>` : "";
  return layout({ title: "赛程", me, current: "events", banner, body: `
<div class="stack"><h1>赛程</h1>
  <p class="muted">时间都是北京时间。进行中、即将开始的会出现在 Evo 的赛事页；结束超过 7 天的不会发布。</p></div>
${notes({ error, ok })}
${editable ? `<datalist id="streamer-names">${streaming.map((p) => `<option value="${escape(p.name)}">${escape(p.team)}</option>`).join("")}</datalist>` : ""}
${add}
${section("live", "进行中")}${section("upcoming", "即将开始")}${section("ended", "已结束")}
${items.length ? "" : `<p class="muted">还没有赛事。</p>`}
${editable ? importBox("/events/import", [...EVENT_COLUMNS, "主舞台（可以没有：平台 房间号 名字，多路用顿号隔开）"], "亚太北区职业联赛 第 3 周\t联赛\tB 组对 C 组\t2026-11-17 19:00\t2026-11-17 23:00\t星火电竞、北辰\tB站 7734200 官方直播", draftTable) : ""}` });
}

// ---------- 发布 ----------

function fileSummary(info) {
  if (!info) return `<p class="muted">线上还没有这个文件。</p>`;
  return `<p>${escape(info.summary)}${info.logos ? `，${info.logos} 支队有队徽` : ""}</p><p class="small muted">文件里写的更新时间：${escape(info.updated || "没有")}`
    + `${info.platformsOff.length ? `；关掉的平台：${escape(info.platformsOff.join("、"))}` : "；四个平台都开着"}</p>`;
}

function eventList(info) {
  if (!info?.events.length) return "";
  return `<ul class="list compact">${info.events.map((e) => `<li class="row"><div class="row-main">
    <div class="row-name"><span class="badge">${escape(e.status)}</span>${escape(e.text)}</div>
    <div class="row-meta"><span>${escape(e.when)}</span><span>${e.teams} 支队、${e.players} 人</span></div></div></li>`).join("")}</ul>`;
}

export function publishPage(me, view, last, { now, error = "", ok = "", banner = "" }) {
  const result = view.built.official;
  const allowed = can(me, "publish");
  const blocked = result.errors.length > 0;
  const check = blocked ? noteList("error", `还不能发布，有 ${result.errors.length} 处要改：`, result.errors)
    : `<p class="note note-ok">检查过了，可以发布。</p>`;
  const reminders = noteList("info", "提醒（不改也能发布）：", result.notes, 3);
  const target = (key) => {
    const t = TARGETS[key];
    const live = describe(view.live[key].data, now);
    const record = last[key];
    const button = !allowed ? `<p class="small muted">发布要找管理员（或者请管理员给你开发布）。</p>`
      : blocked ? `<button class="btn" type="button" disabled>先把上面的问题改好</button>`
      : key === "test" ? `<form method="post" action="/publish/test"><button class="btn btn-primary" type="submit">发布到测试文件</button></form>`
      : `<details class="act"><summary class="btn btn-primary">发布到正式文件</summary><div class="panel">
          <p class="small">所有玩家打开 Evo 的赛事页，看到的就会变成"要发布的内容"那样。先发过测试文件、核对过了吗？</p>
          <form method="post" action="/publish/official"><button class="btn btn-primary btn-wide" type="submit">确定发布到正式文件</button></form></div></details>`;
    const switches = me.role === "admin" && view.live[key].data ? `<div class="switches"><span class="small muted">平台开关：</span>${Object.entries(PLATFORMS).map(([pk, p]) => {
      const on = view.live[key].data.platforms[pk];
      return `<details class="act"><summary class="btn btn-small ${on ? "" : "btn-danger"}">${p.name}：${on ? "开" : "关"}</summary><div class="panel">
        <p class="small">${on ? `关掉以后，Evo 不在窗口里播${p.name}，改成"去${p.name}看"。平台出问题、播不了的时候用，不用发新版本。` : `打开以后，Evo 又在窗口里播${p.name}。`}</p>
        <form method="post" action="/publish/platform"><input type="hidden" name="target" value="${key}"><input type="hidden" name="platform" value="${pk}">
          <input type="hidden" name="on" value="${on ? "0" : "1"}"><button class="btn ${on ? "btn-danger" : "btn-primary"} btn-wide" type="submit">确定${on ? "关掉" : "打开"}${p.name}</button></form></div></details>`;
    }).join("")}</div>` : "";
    return `<section class="card stack">
      <div class="title-row"><h2>${t.name}</h2><a class="small" href="${PUBLIC_URL}/${t.file}" target="_blank" rel="noopener noreferrer">${t.file}</a></div>
      <p class="small muted">${key === "test" ? "玩家的 Evo 不读它：先发到这里，点上面的文件名看一眼，没问题再发正式文件。" : "玩家的 Evo 赛事页读的就是它。"}
        ${record ? `上次是 ${escape(record.by)} ${escape(ago(record.at, now))}发的（${record.events} 场赛事、${record.players} 位选手）。` : "这个网页还没发过。"}</p>
      <h3>线上现在</h3>${fileSummary(live)}
      ${switches}
      ${button}
    </section>`;
  };
  const coming = describe(result.data, now);
  return layout({ title: "发布", me, current: "publish", banner, body: `
<div class="stack"><h1>发布到 Evo</h1>
  <p class="muted">把选手库和赛程生成 Evo 赛事页读的文件（放在 dl.evotracker.cn 上）。先发测试文件，核对没问题再发正式文件。</p></div>
${notes({ error, ok })}
${check}${reminders}
<section class="card stack"><h2>要发布的内容</h2>${blocked ? `<p class="small muted">（有问题的地方改好以后才能发；下面是现在能生成的样子）</p>` : ""}
  ${fileSummary(coming)}${eventList(coming)}</section>
${target("test")}${target("official")}` });
}

// ---------- 改动记录 ----------

export function logPage(me, items, now) {
  return layout({ title: "改动记录", me, current: "", body: `
<div class="stack"><h1>改动记录</h1><p class="muted">最近 ${items.length} 条（最多留 300 条），新的在前。时间是北京时间。</p></div>
<section class="card">${logList(items, now)}</section>` });
}
