// Evo 赛事数据的协作网页（2026-11-17 用户决定）：Cloudflare Worker，地址 https://team.evotracker.cn。
// 几个人一起管选手库、赛程和从 Liquipedia 抓来的选手资料，管好了发布成 Evo 赛事页读的 esports.json。
// 计划和用户定的做法见 claude/todo.md「赛事数据管理网页」；部署、怎么用见 docs/底座.md"协作网页"。
//
// 第 1 步（现在）：登录、邀请、身份、成员管理，首页是空的骨架。
// - 没有账号密码：管理员在「成员」页生成邀请链接（/join/口令），微信发给对方；对方打开、点"进入"，
//   浏览器记住 90 天（Cookie）。链接 7 天内有效、用一次就作废。打开链接只显示是谁的邀请，点了"进入"才用掉：
//   微信、QQ 生成链接预览时会先打开一次链接，那样不会把邀请用掉
// - 身份：管理员（什么都能做）、编辑（能改，发布要单独开）、只读。至少留一个管理员
// - 第一个管理员的链接、管理员自己进不来时：python tools/team.py（在命令行里直接改存储桶里的成员文件）
// - 数据在不公开的 R2 存储桶 evo-team（成员见 auth.js）；不用 D1：D1 的免费额度整个账号共用，
//   统计服务器 2026-11-08 超过一次，一超当天所有数据库都报错
// - 不记请求日志（observability 关着）、不存 IP；搜索引擎不收录（robots.txt + noindex）
// - 只许本站的表单提交（看 Origin），页面只许运行本站的脚本（CSP），不许被别的网站嵌进去
//
// 本机试：cd server/team && wrangler dev（本机模拟的存储桶），python tools/team.py invite 名字 --local 拿链接
// 测试：node server/team/test.mjs
// 部署：cd server/team && wrangler deploy（第一次先 wrangler r2 bucket create evo-team）

import {
  Refuse, acceptInvite, can, cancelInvite, clearCookie, createInvite, findInvite, load, logout, removeMember,
  sessionCookie, setRole, validId, whoami,
} from "./auth.js";
import { APP_JS, homePage, invitePage, layout, loginPage, membersPage, messagePage, publishBanner } from "./pages.js";
import { eventsPage, logPage, playersPage, publishPage } from "./pages_esports.js";
import { importEvents, importPlayers } from "./esports.js";
import {
  addEvent, addPlayer, deleteEvent, deletePlayer, events, importEventRows, importPlayerRows, players, published, recentLog,
  renameTeam, saveEvent, savePlayer, teamsOf,
} from "./data.js";
import { preview, publish, setPlatform, unpublished } from "./publish.js";
import { liquipediaPage } from "./pages_lp.js";
import { addFromLiquipedia, eventEntries, teamEntries } from "./lpimport.js";
import { logoHref, logoPage } from "./pages_logos.js";
import { LOGO_FILE, library, libraryKey, pickLogo, removeLogo, search, suggest, teamLogos, uploadLogo } from "./logos.js";
import { proHref, proPage, prosPage, teamProfilePage } from "./pages_pros.js";
import { merged, overrides, removeOverride, resolveConflict, setOverride } from "./overrides.js";
import {
  addTracked, confirmMatch, index, pending, pendingCount, progress, rejectMatch, rejects, removeTracked, requestRefresh, resultsOf,
  squadsOf, state, tick, tracked,
} from "./lpsync.js";

const MAX_FORM = 64 * 1024;  // 从 Excel 粘贴一大张表也够

const SECURITY = {
  // 图片：本站、上传前的预览（blob:）、下载服务器上的队徽
  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; img-src 'self' data: blob: https://dl.evotracker.cn; "
    + "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
  // same-origin：本站之间照常带（表单提交要靠 Origin 认是不是本站），点到别的网站不带，邀请链接不会漏出去
  "Referrer-Policy": "same-origin",
  "X-Content-Type-Options": "nosniff",
  "X-Robots-Tag": "noindex, nofollow",
  "Cache-Control": "no-store",
};

function respond(body, { status = 200, type = "text/html; charset=utf-8", headers = {} } = {}) {
  return new Response(body, { status, headers: { ...SECURITY, "Content-Type": type, ...headers } });
}

function redirect(location, headers = {}) {
  return new Response(null, { status: 303, headers: { ...SECURITY, Location: location, ...headers } });
}

// 表单只认本站提交的（别的网站放一个表单骗你点，带不上本站的 Origin）
function sameOrigin(request) {
  const origin = request.headers.get("Origin");
  return origin !== null && origin === new URL(request.url).origin;
}

async function readForm(request) {
  const length = Number(request.headers.get("Content-Length") ?? 0);
  if (length > MAX_FORM) throw new Refuse("提交的内容太长了");
  const form = await request.formData();
  const get = (key) => {
    const value = form.get(key);
    return typeof value === "string" ? value : "";
  };
  return { name: get("name"), role: get("role"), publish: get("publish") === "1" };
}

// 选手、赛事的表单：每一项原样拿出来（勾选的队伍是一串）
async function readFields(request) {
  const length = Number(request.headers.get("Content-Length") ?? 0);
  if (length > MAX_FORM) throw new Refuse("提交的内容太长了：一次少粘贴一些");
  const out = { teams: [] };
  if (!/^(application\/x-www-form-urlencoded|multipart\/form-data)/.test(request.headers.get("Content-Type") ?? "")) return out;
  const form = await request.formData();
  const lists = { main_platform: [], main_room: [], main_name: [] };
  for (const [key, value] of form.entries()) {
    if (typeof value !== "string") continue;
    if (key === "teams") out.teams.push(value);
    else if (Object.hasOwn(lists, key)) lists[key].push(value);
    else out[key] = value;
  }
  if (form.has("main_platform")) {  // 赛事表单里主舞台的几行
    out.main = lists.main_platform.map((platform, i) => ({ platform, room: lists.main_room[i] ?? "", name: lists.main_name[i] ?? "" }));
  }
  if ((out.table ?? "").length > MAX_FORM) throw new Refuse("提交的内容太长了：一次少粘贴一些");
  return out;
}

// 改好以后回到列表页上显示的那句话（地址里只放代号，不放原话）
const OK = {
  added: "加好了", saved: "保存好了", deleted: "删掉了", renamed: "队名改好了，赛程里也跟着改了",
  test: "测试文件发好了（写进去以后读回来核对过，一字不差）", official: "正式文件发好了：玩家下次打开 Evo 的赛事页就能看到",
  platform: "平台开关改好了",
  tracked: "收录了：几分钟内开始抓（每 3 分钟抓一步）", untracked: "不再收录了", refresh: "排上了：几分钟内开始新的一轮",
  matched: "填好了：选手库里记上了 Liquipedia 页面名", rejected: "记下了：以后不再这样建议",
  fixed: "修正好了：页面上用你们改的", unfixed: "撤掉了：改回用 Liquipedia 的", resolved: "处理好了",
  logo: "队徽换好了：下次发布 Evo 就能看到", unlogo: "队徽去掉了",
};

function okText(params) {
  const count = (key) => Number.parseInt(params.get(key), 10) || 0;
  if (params.get("ok") === "fromlp") {
    return `从 Liquipedia 加进来 ${count("added")} 位${count("pending") ? `（${count("pending")} 位直播间待填：在下面补上，或者平台选"不直播"）` : ""}`
      + `${count("skipped") ? `，跳过 ${count("skipped")} 位（选手库里已经有了）` : ""}`;
  }
  if (params.get("ok") === "imported") {
    const added = Number.parseInt(params.get("added"), 10) || 0;
    const changed = Number.parseInt(params.get("changed"), 10) || 0;
    return `导入好了：新加 ${added} 条，更新 ${changed} 条`;
  }
  return OK[params.get("ok")] ?? "";
}

export async function handle(request, env, now = Date.now()) {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;
  const bucket = env.DATA;

  // HEAD（测速、看网站通不通的工具常用）：照 GET 走一遍，只回响应头
  if (method === "HEAD") {
    const response = await handle(new Request(request, { method: "GET" }), env, now);
    return new Response(null, { status: response.status, headers: response.headers });
  }
  if (path === "/robots.txt") return respond("User-agent: *\nDisallow: /\n", { type: "text/plain; charset=utf-8" });
  if (path === "/app.js") return respond(APP_JS, { type: "text/javascript; charset=utf-8" });
  if (path === "/favicon.ico") return new Response(null, { status: 204, headers: SECURITY });
  if (method !== "GET" && method !== "POST") return respond("", { status: 405 });
  if (method === "POST" && !sameOrigin(request)) {
    return respond(messagePage("没有保存", "这次提交不是从本站的页面发出的，没有保存。请回到页面重新点一次。"), { status: 403 });
  }

  // 邀请链接
  const join = path.match(/^\/join\/([^/]+)$/);
  if (join) {
    const token = join[1];
    if (method === "GET") {
      const invite = await findInvite(bucket, token, now);
      if (!invite) {
        return respond(messagePage("链接不能用了", "这个邀请链接已经用过、过期了，或者被作废了。请找管理员要一个新的。"), { status: 410 });
      }
      return respond(invitePage(invite, path));
    }
    const accepted = await acceptInvite(bucket, token, now, request.headers.get("User-Agent"));
    if (!accepted) {
      return respond(messagePage("链接不能用了", "这个邀请链接已经用过、过期了，或者被作废了。请找管理员要一个新的。"), { status: 410 });
    }
    return redirect("/", { "Set-Cookie": sessionCookie(accepted.token) });
  }

  const who = await whoami(bucket, request, now);
  if (!who) {
    if (method === "POST") return redirect("/");
    if (path === "/") return respond(loginPage());
    return respond(loginPage(), { status: 401 });
  }
  const me = who.member;

  if (path === "/logout" && method === "POST") {
    await logout(bucket, who.sessionHash);
    return redirect("/", { "Set-Cookie": clearCookie() });
  }
  if (path === "/" && method === "GET") {
    const [playerItems, eventItems, recent, last, idx, no, ov] = await Promise.all([players(bucket), events(bucket), recentLog(bucket, 8),
      published(bucket), index(bucket), rejects(bucket), overrides(bucket)]);
    const pend = pending(playerItems, idx, no, ov);
    return respond(homePage(me, { now, recent, banner: publishBanner(me, await unpublished(env, now), now), stats: { players: playerItems.length, teams: teamsOf(playerItems).size,
      events: eventItems.length, published: last, lpPlayers: Object.values(idx.players).filter((x) => !x.missing).length,
      pending: pendingCount(pend) } }));
  }
  if (path === "/log" && method === "GET") return respond(logPage(me, await recentLog(bucket), now));
  const notFound = () => respond(messagePage("没有这个页面", "地址可能写错了。", me), { status: 404 });
  const readOnly = () => respond(messagePage("不能改", "只读的身份只能看，要改请找管理员。", me), { status: 403 });

  // ---------- 队徽 ----------
  if (path.startsWith("/logo-library/")) {  // 队标库里的图（不公开的存储桶，登录了才看得到）
    const file = path.slice("/logo-library/".length);
    const object = method === "GET" && LOGO_FILE.test(file) ? await bucket.get(libraryKey(file)) : null;
    if (!object) return notFound();
    return new Response(object.body, { headers: { ...SECURITY, "Content-Type": "image/png", "Cache-Control": "private, max-age=86400" } });
  }
  if (path === "/logo" || path.startsWith("/logo/")) {
    const show = async (team, options = {}, status = 200) => {
      const [items, logos, playerItems] = await Promise.all([library(bucket), teamLogos(bucket), players(bucket)]);
      if (!teamsOf(playerItems).has(team)) return respond(messagePage("没有这支队", "选手库里没有这个队名（可能刚改了名）。", me), { status: 404 });
      const q = url.searchParams.get("q") ?? "";
      return respond(logoPage(me, { team, current: logos[team], suggestions: suggest(team, items), results: search(q, items), q,
        libraryCount: items.length, now, ...options }), { status });
    };
    if (path === "/logo" && method === "GET") return show(url.searchParams.get("team") ?? "", { ok: okText(url.searchParams) });
    if (method !== "POST") return notFound();
    if (!can(me, "edit")) return readOnly();
    let team = "";
    try {
      if (path === "/logo/upload") {
        if (Number(request.headers.get("Content-Length") ?? 0) > 200 * 1024) throw new Refuse("图片太大了：最多 60 KB（网页上会自动缩，缩不下来就换一张）");
        const form = await request.formData();
        team = String(form.get("team") ?? "");
        const file = form.get("file");
        await uploadLogo(env, me, team, file && typeof file !== "string" ? await file.arrayBuffer() : null, now);
      } else {
        const form = await readFields(request);
        team = form.team ?? "";
        if (path === "/logo/pick") await pickLogo(env, me, team, form.file ?? "", now);
        else if (path === "/logo/remove") await removeLogo(bucket, me, team, now);
        else return notFound();
      }
      return redirect(logoHref(team, `&ok=${path === "/logo/remove" ? "unlogo" : "logo"}`));
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show(team, { error: error.message }, 400);
    }
  }

  // ---------- 选手库 ----------
  if (path === "/players" || path.startsWith("/players/") || path === "/teams/rename") {
    const show = async (options = {}, status = 200) => {
      const [items, logos, changed] = await Promise.all([players(bucket), teamLogos(bucket), unpublished(env, now)]);
      return respond(playersPage(me, items, { now, logos, banner: publishBanner(me, changed, now), ...options }), { status });
    };
    if (method === "GET") return path === "/players" ? show({ ok: okText(url.searchParams) }) : notFound();
    if (!can(me, "edit")) return readOnly();
    let form = {};
    try {
      form = await readFields(request);
      if (path === "/players/add") {
        await addPlayer(bucket, me, form, now);
        return redirect("/players?ok=added");
      }
      if (path === "/players/import") {
        const { rows, errors } = importPlayers(form.table);
        if (errors.length) throw new Refuse(`没有导入，有 ${errors.length} 处要改：\n${errors.join("\n")}`);
        if (!rows.length) throw new Refuse("没读到任何一行：在 Excel 里选中表格、复制，再粘贴进来");
        const result = await importPlayerRows(bucket, me, rows, now);
        return redirect(`/players?ok=imported&added=${result.added}&changed=${result.changed}`);
      }
      if (path === "/teams/rename") {
        await renameTeam(bucket, me, form.from, form.to, now);
        return redirect("/players?ok=renamed");
      }
      const action = path.match(/^\/players\/([0-9a-f]{8})\/(save|delete)$/);
      if (!action) return notFound();
      if (action[2] === "save") await savePlayer(bucket, me, action[1], form, now);
      else await deletePlayer(bucket, me, action[1], now);
      return redirect(`/players?ok=${action[2] === "save" ? "saved" : "deleted"}`);
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message, draft: path === "/players/add" ? form : null,
                    draftTable: path === "/players/import" ? form.table : "" }, 400);
    }
  }

  // ---------- 赛程 ----------
  if (path === "/events" || path.startsWith("/events/")) {
    const show = async (options = {}, status = 200) => {
      const [eventItems, playerItems, changed] = await Promise.all([events(bucket), players(bucket), unpublished(env, now)]);
      return respond(eventsPage(me, eventItems, playerItems, { now, banner: publishBanner(me, changed, now), ...options }), { status });
    };
    if (method === "GET") return path === "/events" ? show({ ok: okText(url.searchParams) }) : notFound();
    if (!can(me, "edit")) return readOnly();
    let form = {};
    try {
      form = await readFields(request);
      if (path === "/events/add") {
        await addEvent(bucket, me, form, now);
        return redirect("/events?ok=added");
      }
      if (path === "/events/import") {
        const known = [...teamsOf(await players(bucket)).keys()];
        const { rows, errors } = importEvents(form.table, known);
        if (errors.length) throw new Refuse(`没有导入，有 ${errors.length} 处要改：\n${errors.join("\n")}`);
        if (!rows.length) throw new Refuse("没读到任何一行：在 Excel 里选中表格、复制，再粘贴进来");
        const result = await importEventRows(bucket, me, rows, now);
        return redirect(`/events?ok=imported&added=${result.added}&changed=${result.changed}`);
      }
      const action = path.match(/^\/events\/([0-9a-f]{8})\/(save|delete)$/);
      if (!action) return notFound();
      if (action[2] === "save") await saveEvent(bucket, me, action[1], form, now);
      else await deleteEvent(bucket, me, action[1], now);
      return redirect(`/events?ok=${action[2] === "save" ? "saved" : "deleted"}`);
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message, draft: path === "/events/add" ? form : null,
                    draftTable: path === "/events/import" ? form.table : "" }, 400);
    }
  }

  // ---------- Liquipedia ----------
  if (path === "/liquipedia" || path.startsWith("/liquipedia/")) {
    const show = async (options = {}, status = 200) => {
      const [trackedItems, idx, s, playerItems, no, ov] = await Promise.all([tracked(bucket), index(bucket), state(bucket), players(bucket),
        rejects(bucket), overrides(bucket)]);
      return respond(liquipediaPage(me, { tracked: trackedItems, idx, progress: progress(s, now), pend: pending(playerItems, idx, no, ov),
        keySet: Boolean(String(env.LIQUIPEDIA_API_KEY ?? "").trim()), now, ...options }), { status });
    };
    if (method === "GET") return path === "/liquipedia" ? show({ ok: okText(url.searchParams) }) : notFound();
    if (!can(me, "edit")) return readOnly();
    try {
      const form = await readFields(request);
      if (path === "/liquipedia/track") {
        await addTracked(bucket, me, form.page, now);
        return redirect("/liquipedia?ok=tracked");
      }
      if (path === "/liquipedia/untrack") {
        await removeTracked(bucket, me, form.page ?? "", now);
        return redirect("/liquipedia?ok=untracked");
      }
      if (path === "/liquipedia/to-roster") {
        const r = await addFromLiquipedia(bucket, me, await eventEntries(bucket, form.page ?? ""), `比赛 ${form.page ?? ""}`, now);
        return redirect(`/players?ok=fromlp&added=${r.added}&pending=${r.pending}&skipped=${r.skipped}`);
      }
      if (path === "/liquipedia/refresh") {
        await requestRefresh(bucket);
        return redirect("/liquipedia?ok=refresh");
      }
      if (path === "/liquipedia/match") {
        if (form.answer === "yes") await confirmMatch(bucket, me, form.player ?? "", form.page ?? "", now);
        else await rejectMatch(bucket, me, form.player ?? "", form.page ?? "", now);
        return redirect(`/liquipedia?ok=${form.answer === "yes" ? "matched" : "rejected"}#confirm`);
      }
      return notFound();
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message }, 400);
    }
  }

  // ---------- 资料：选手、战队（Liquipedia 抓来的 + 手动修正） ----------
  if (path === "/pros" || path.startsWith("/pros/") || path.startsWith("/teams/")) {
    const decode = (text) => {
      try {
        return decodeURIComponent(text);
      } catch {
        return null;
      }
    };
    const load = async () => {
      const [idx, ov, manual] = await Promise.all([index(bucket), overrides(bucket), players(bucket)]);
      const linkedBy = new Map();
      for (const m of manual) {
        if (!m.liquipedia) continue;
        if (!linkedBy.has(m.liquipedia)) linkedBy.set(m.liquipedia, []);
        linkedBy.get(m.liquipedia).push(m);
      }
      const teams = Object.values(idx.teams).filter((t) => !t.missing);
      return { idx, ov, linkedBy, teams, knownTeams: new Set(teams.map((t) => t.page)),
               all: Object.values(idx.players).filter((p) => !p.missing).map((p) => merged(p, ov[p.page])) };
    };
    if (path === "/pros" && method === "GET") {
      const d = await load();
      return respond(prosPage(me, { q: url.searchParams.get("q") ?? "", players: d.all, teams: d.teams, linkedBy: d.linkedBy, now }));
    }
    const toRoster = path.match(/^\/teams\/([^/]+)\/to-roster$/);
    if (toRoster) {
      if (method !== "POST") return notFound();
      if (!can(me, "edit")) return readOnly();
      const page = decode(toRoster[1]);
      try {
        const d = await load();
        const r = await addFromLiquipedia(bucket, me, teamEntries(d.idx, d.ov, page), `战队 ${page}`, now);
        return redirect(`/players?ok=fromlp&added=${r.added}&pending=${r.pending}&skipped=${r.skipped}`);
      } catch (error) {
        if (!(error instanceof Refuse)) throw error;
        return respond(messagePage("没有加", error.message, me), { status: 400 });
      }
    }
    const teamMatch = path.match(/^\/teams\/([^/]+)$/);
    if (teamMatch) {
      if (method !== "GET") return notFound();
      const page = decode(teamMatch[1]);
      const d = await load();
      const t = d.idx.teams[page];
      if (!t || t.missing) return respond(messagePage("没有这支战队的资料", "可能还没抓到，或者地址写错了。", me), { status: 404 });
      const squads = await squadsOf(bucket);
      const current = d.all.filter((p) => (p.teamPage === page && !p.fixed?.team) || (p.fixed?.team && p.team === t.name));
      const past = [];
      for (const p of d.all) {
        for (const s of squads[p.page] ?? []) {
          if (s.teamPage === page && (!s.type || s.type === "player") && (s.leave || s.status !== "active")) past.push({ p, s });
        }
      }
      past.sort((a, b) => (b.s.leave || "").localeCompare(a.s.leave || ""));
      const inRoster = new Set((await players(bucket)).map((p) => p.liquipedia).filter(Boolean));
      return respond(teamProfilePage(me, { t, current, past, now, inRoster }));
    }
    const pro = path.match(/^\/pros\/([^/]+)(?:\/(fix|unfix|resolve))?$/);
    const page = pro ? decode(pro[1]) : null;
    if (!pro || page === null) return notFound();
    const show = async (options = {}, status = 200) => {
      const d = await load();
      const raw = d.idx.players[page];
      if (!raw || raw.missing) return respond(messagePage("没有这位选手的资料", "可能还没抓到，或者 Liquipedia 上没有这个页面。", me), { status: 404 });
      const [squads, results] = await Promise.all([squadsOf(bucket), resultsOf(bucket, page)]);
      return respond(proPage(me, { p: merged(raw, d.ov[page]), squads: squads[page] ?? [], results, linked: d.linkedBy.get(page) ?? [],
        knownTeams: d.knownTeams, now, ...options }), { status });
    };
    if (!pro[2]) return method === "GET" ? show({ ok: okText(url.searchParams) }) : notFound();
    if (method !== "POST") return notFound();
    if (!can(me, "edit")) return readOnly();
    try {
      const form = await readFields(request);
      if (pro[2] === "fix") {
        await setOverride(bucket, me, page, form.field ?? "", form.value ?? "", form.reason ?? "", await index(bucket), now);
        return redirect(`${proHref(page)}?ok=fixed`);
      }
      if (pro[2] === "unfix") {
        await removeOverride(bucket, me, page, form.field ?? "", now);
        return redirect(`${proHref(page)}?ok=unfixed`);
      }
      await resolveConflict(bucket, me, page, form.field ?? "", form.keep === "ours", now);
      return redirect("/liquipedia?ok=resolved#confirm");
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return pro[2] === "resolve" ? redirect("/liquipedia#confirm") : show({ error: error.message }, 400);
    }
  }

  // ---------- 发布 ----------
  if (path === "/publish" || path.startsWith("/publish/")) {
    const show = async (options = {}, status = 200) => {
      const [view, last, changed] = await Promise.all([preview(env, now), published(bucket), unpublished(env, now)]);
      return respond(publishPage(me, view, last, { now, banner: publishBanner(me, changed, now, true), ...options }), { status });
    };
    if (method === "GET") return path === "/publish" ? show({ ok: okText(url.searchParams) }) : notFound();
    try {
      if (path === "/publish/test" || path === "/publish/official") {
        if (!can(me, "publish")) return respond(messagePage("不能发布", "发布要找管理员（或者请管理员给你开发布）。", me), { status: 403 });
        const target = path.slice("/publish/".length);
        await publish(env, me, target, now);
        return redirect(`/publish?ok=${target}`);
      }
      if (path === "/publish/platform") {
        if (me.role !== "admin") return respond(messagePage("不能改", "平台开关只有管理员能改。", me), { status: 403 });
        const form = await readFields(request);
        await setPlatform(env, me, form.target, form.platform, form.on === "1", now);
        return redirect("/publish?ok=platform");
      }
      return notFound();
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return show({ error: error.message }, 400);
    }
  }

  // ---------- 成员（只有管理员） ----------
  if (path === "/members" || path.startsWith("/members/") || path.startsWith("/invites/")) {
    if (!can(me, "members")) return respond(messagePage("只有管理员能看", "成员页只有管理员能打开。", me), { status: 403 });
    const view = (options, status = 200) => respond(membersPage(me, options.data ?? who.data,
      { origin: url.origin, now, ...options }), { status });
    if (path === "/members" && method === "GET") return view({ ok: url.searchParams.get("ok") ?? "" });
    if (method !== "POST") return respond(messagePage("没有这个页面", "地址可能写错了。", me), { status: 404 });

    const action = path.match(/^\/(members|invites)\/([^/]+)\/(invite|role|remove|link|cancel)$/)
      ?? (path === "/members/invite" ? [path, "members", "", "invite"] : null);
    if (!action) return respond(messagePage("没有这个页面", "地址可能写错了。", me), { status: 404 });
    const [, kind, id, verb] = action;
    try {
      if (kind === "members" && verb === "invite" && !id) {
        const created = await createInvite(bucket, me, await readForm(request), now);
        return view({ created, data: (await load(bucket)).data });
      }
      if (!validId(id)) throw new Refuse("没有这个成员（可能刚被别人移出了）");
      if (kind === "members" && verb === "link") {
        const created = await createInvite(bucket, me, { memberId: id }, now);
        return view({ created, data: (await load(bucket)).data });
      }
      if (kind === "members" && verb === "role") {
        await setRole(bucket, id, await readForm(request));
        return redirect("/members?ok=role");
      }
      if (kind === "members" && verb === "remove") {
        await removeMember(bucket, id);
        return redirect(id === me.id ? "/" : "/members?ok=removed");
      }
      if (kind === "invites" && verb === "cancel") {
        await cancelInvite(bucket, id);
        return redirect("/members?ok=cancelled");
      }
      return respond(messagePage("没有这个页面", "地址可能写错了。", me), { status: 404 });
    } catch (error) {
      if (!(error instanceof Refuse)) throw error;
      return view({ error: error.message, data: (await load(bucket)).data }, 400);
    }
  }

  return notFound();
}

export default {
  // 定时任务（wrangler.toml 的 crons，每 3 分钟）：从 Liquipedia 抓一步（lpsync.js）
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(tick(env, Date.now()));
  },
  async fetch(request, env) {
    try {
      return await handle(request, env);
    } catch (error) {
      if (error instanceof Refuse) return respond(messagePage("没有保存", error.message), { status: 400 });
      return respond(layout({ title: "出错了", narrow: true, body: `<section class="card stack"><h1>服务器出错了</h1>
        <p>请过一会儿再试。一直这样的话告诉管理员。</p></section>` }), { status: 500 });
    }
  },
};
