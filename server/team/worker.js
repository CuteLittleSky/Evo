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
import { APP_JS, homePage, invitePage, layout, loginPage, membersPage, messagePage } from "./pages.js";

const MAX_FORM = 8 * 1024;

const SECURITY = {
  "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; img-src 'self' data:; "
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

export async function handle(request, env, now = Date.now()) {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;
  const bucket = env.DATA;

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
  if (path === "/" && method === "GET") return respond(homePage(me));

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

  return respond(messagePage("没有这个页面", "地址可能写错了。", me), { status: 404 });
}

export default {
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
