// 协作网页的测试：node server/team/test.mjs（只要 Node.js，不用 wrangler、不用联网）。
// 用内存里的假存储桶（支持 etag 条件写入）把 worker.js 的每条路走一遍：登录页、邀请（打开不用掉、点进入才用掉）、
// 身份和权限、改身份、至少留一个管理员、发新链接、移出、作废、退出、过期、两个人同时改、只认本站的表单、安全相关的响应头。

import worker, { handle } from "./worker.js";
import { COOKIE, KEY, hash, newToken, update } from "./auth.js";

let failures = 0;
let checks = 0;
function check(name, condition) {
  checks++;
  if (!condition) {
    failures++;
    console.log(`  ✗ ${name}`);
  }
}

// ---------- 假存储桶 ----------
class FakeBucket {
  constructor() { this.objects = new Map(); this.version = 0; this.conflicts = 0; }
  async get(key) {
    const object = this.objects.get(key);
    return object ? { etag: object.etag, text: async () => object.body } : null;
  }
  async put(key, body, options = {}) {
    const current = this.objects.get(key);
    const want = options.onlyIf?.etagMatches;
    if (want && (!current || current.etag !== want)) return null;
    const etag = `e${++this.version}`;
    this.objects.set(key, { body: String(body), etag });
    return { etag };
  }
  json() { return JSON.parse(this.objects.get(KEY)?.body ?? '{"members":[],"invites":[]}'); }
}

const ORIGIN = "https://team.evotracker.cn";
const DAY = 24 * 3600 * 1000;
let now = Date.UTC(2026, 10, 17, 12, 0);
const env = { DATA: new FakeBucket() };

// 一个"浏览器"：记着自己的 Cookie
function browser(userAgent = "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0") {
  const jar = { cookie: "" };
  async function go(method, path, { form = null, origin = ORIGIN } = {}) {
    const headers = { "User-Agent": userAgent };
    if (jar.cookie) headers.Cookie = jar.cookie;
    if (origin !== null && method === "POST") headers.Origin = origin;
    let body;
    if (form) {
      body = new URLSearchParams(form).toString();
      headers["Content-Type"] = "application/x-www-form-urlencoded";
    }
    const response = await handle(new Request(ORIGIN + path, { method, headers, body }), env, now);
    const setCookie = response.headers.get("Set-Cookie");
    if (setCookie) {
      const [pair] = setCookie.split(";");
      jar.cookie = /Max-Age=0/.test(setCookie) ? "" : pair;
    }
    return { status: response.status, location: response.headers.get("Location"), headers: response.headers,
             text: await response.text(), setCookie };
  }
  return { go, jar, get: (path) => go("GET", path), post: (path, form, options) => go("POST", path, { form, ...options }) };
}

// 照 tools/team.py 的做法往存储桶里放一个邀请（命令行生成第一个管理员）
async function seedInvite(name, role = "admin") {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: "0000aaaa", h, name, role, publish: role === "admin", created: now, createdBy: "命令行",
                        expires: now + 7 * DAY });
  });
  return token;
}

function linkFrom(html) {
  return html.match(/\/join\/([A-Za-z0-9_-]{32})/)?.[1] ?? null;
}

// ---------- 没登录 ----------
console.log("没登录");
const stranger = browser();
let r = await stranger.get("/");
check("首页是登录说明", r.status === 200 && r.text.includes("请找管理员要一个邀请链接"));
check("不收录：响应头", r.headers.get("X-Robots-Tag") === "noindex, nofollow");
check("不收录：页面里", r.text.includes('name="robots" content="noindex'));
check("CSP 只许本站脚本、不许被嵌", /script-src 'self'/.test(r.headers.get("Content-Security-Policy"))
  && /frame-ancestors 'none'/.test(r.headers.get("Content-Security-Policy")));
check("Referrer-Policy same-origin", r.headers.get("Referrer-Policy") === "same-origin");
check("不缓存", r.headers.get("Cache-Control") === "no-store");
r = await stranger.get("/robots.txt");
check("robots.txt 全部不许", r.text.includes("Disallow: /"));
r = await stranger.get("/members");
check("没登录看成员页 → 401 登录说明", r.status === 401 && r.text.includes("邀请链接"));
r = await stranger.post("/members/invite", { name: "坏人", role: "admin" });
check("没登录提交表单 → 回首页，不保存", r.status === 303 && env.DATA.json().invites.length === 0);
r = await stranger.get("/join/" + "x".repeat(32));
check("乱写的邀请链接 → 不能用", r.status === 410);
r = await stranger.get("/join/abc");
check("格式不对的邀请链接 → 不能用", r.status === 410);

// ---------- 第一个管理员 ----------
console.log("第一个管理员（命令行生成的链接）");
const adminToken = await seedInvite("August");
const admin = browser();
r = await admin.get("/join/" + adminToken);
check("打开链接显示是谁的邀请", r.status === 200 && r.text.includes("August") && r.text.includes("管理员"));
check("打开（链接预览）不用掉邀请", env.DATA.json().invites.length === 1);
r = await admin.post("/join/" + adminToken, null, { origin: "https://evil.example" });
check("别的网站的表单 → 403，不用掉", r.status === 403 && env.DATA.json().invites.length === 1);
r = await admin.post("/join/" + adminToken, null, { origin: null });
check("没有 Origin → 403", r.status === 403);
r = await admin.post("/join/" + adminToken);
check("点进入 → 登录、回首页", r.status === 303 && r.location === "/" && admin.jar.cookie.startsWith(COOKIE + "="));
check("Cookie 是 HttpOnly、Secure、SameSite=Lax、90 天", /HttpOnly/.test(r.setCookie) && /Secure/.test(r.setCookie)
  && /SameSite=Lax/.test(r.setCookie) && /Max-Age=7776000/.test(r.setCookie));
let saved = env.DATA.json();
check("邀请用掉了", saved.invites.length === 0);
check("成员里有 August（管理员、能发布）", saved.members.length === 1 && saved.members[0].role === "admin" && saved.members[0].publish);
check("存的是口令的 SHA-256，不是原文", !JSON.stringify(saved).includes(adminToken)
  && !JSON.stringify(saved).includes(admin.jar.cookie.split("=")[1]));
check("记下设备：电脑 · Chrome", saved.members[0].sessions[0].device === "电脑 · Chrome");
r = await browser().post("/join/" + adminToken);
check("同一个链接第二次用 → 不能用", r.status === 410);
r = await admin.get("/");
check("首页：你好、身份、成员入口", r.text.includes("你好，August") && r.text.includes('href="/members"'));

// ---------- 邀请编辑、只读 ----------
console.log("邀请成员");
r = await admin.get("/members");
check("成员页能打开", r.status === 200 && r.text.includes("邀请新成员"));
r = await admin.post("/members/invite", { name: "  学弟  ", role: "editor" });
const editorToken = linkFrom(r.text);
check("生成邀请链接，页面上显示一次", r.status === 200 && editorToken && r.text.includes(`${ORIGIN}/join/${editorToken}`));
check("名字去掉前后空格", env.DATA.json().invites[0].name === "学弟");
check("编辑默认不能发布", env.DATA.json().invites[0].publish === false);
r = await admin.post("/members/invite", { name: "", role: "editor" });
check("名字空着 → 提示，不保存", r.status === 400 && r.text.includes("名字不能空着") && env.DATA.json().invites.length === 1);
r = await admin.post("/members/invite", { name: "一".repeat(21), role: "editor" });
check("名字太长 → 提示", r.status === 400 && r.text.includes("最多 20 个字"));
r = await admin.post("/members/invite", { name: "x", role: "owner" });
check("不认识的身份 → 提示", r.status === 400 && r.text.includes("身份只能是"));
r = await admin.post("/members/invite", { name: "<i>只读</i>", role: "viewer" });
const viewerToken = linkFrom(r.text);
check("名字里的 HTML 照字面显示", r.text.includes("&lt;i&gt;只读&lt;/i&gt;") && !r.text.includes("<i>只读</i>"));

const editor = browser("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Mobile MicroMessenger/8.0");
await editor.post("/join/" + editorToken);
const viewer = browser();
await viewer.post("/join/" + viewerToken);
saved = env.DATA.json();
check("三个成员", saved.members.length === 3);
check("微信里打开记成 手机 · 微信", saved.members.find((m) => m.name === "学弟").sessions[0].device === "手机 · 微信");

console.log("权限");
r = await editor.get("/");
check("编辑的首页没有成员入口", r.status === 200 && !r.text.includes('href="/members"') && r.text.includes("发布到 Evo 要找管理员"));
r = await editor.get("/members");
check("编辑打不开成员页", r.status === 403);
r = await editor.post("/members/invite", { name: "混进来", role: "admin" });
check("编辑不能发邀请", r.status === 403 && env.DATA.json().invites.length === 0);
r = await viewer.get("/");
check("只读的首页写着只能看", r.text.includes("只能看，不能改"));

// ---------- 改身份 ----------
console.log("改身份");
const editorId = saved.members.find((m) => m.name === "学弟").id;
const adminId = saved.members.find((m) => m.name === "August").id;
r = await admin.post(`/members/${editorId}/role`, { role: "editor", publish: "1" });
check("给编辑开发布", r.status === 303 && env.DATA.json().members.find((m) => m.id === editorId).publish === true);
r = await editor.get("/");
check("编辑的首页写着能发布", r.text.includes("也能发布到 Evo"));
r = await admin.post(`/members/${editorId}/role`, { role: "viewer", publish: "1" });
check("改成只读，发布跟着关", env.DATA.json().members.find((m) => m.id === editorId).publish === false);
r = await admin.post(`/members/${adminId}/role`, { role: "editor" });
check("最后一个管理员不能改成别的", r.status === 400 && r.text.includes("至少要留一个管理员")
  && env.DATA.json().members.find((m) => m.id === adminId).role === "admin");
r = await admin.post(`/members/${adminId}/remove`);
check("最后一个管理员不能移出", r.status === 400 && env.DATA.json().members.length === 3);
r = await admin.post("/members/zzzzzzzz/role", { role: "viewer" });
check("乱写的成员编号 → 提示", r.status === 400);

// ---------- 发新链接（换设备） ----------
console.log("发新链接");
r = await admin.post(`/members/${editorId}/link`);
const relinkToken = linkFrom(r.text);
check("给已有成员发新链接", relinkToken && r.text.includes("新链接"));
r = await admin.post(`/members/${editorId}/link`);
const relinkToken2 = linkFrom(r.text);
check("再发一次，旧的作废（一个人只留最新的）", env.DATA.json().invites.filter((i) => i.memberId === editorId).length === 1);
const laptop = browser();
r = await laptop.post("/join/" + relinkToken);
check("旧的新链接不能用了", r.status === 410);
r = await laptop.post("/join/" + relinkToken2);
saved = env.DATA.json();
check("用新链接登录成原来那个人，不多一个成员", r.status === 303 && saved.members.length === 3
  && saved.members.find((m) => m.id === editorId).sessions.length === 2);
r = await laptop.get("/");
check("新设备上是原来的名字和身份（只读）", r.text.includes("你好，学弟") && r.text.includes("只能看"));

// ---------- 作废邀请 ----------
console.log("作废邀请");
r = await admin.post("/members/invite", { name: "没来的人", role: "viewer" });
const unusedToken = linkFrom(r.text);
const inviteId = env.DATA.json().invites[0].id;
r = await admin.get("/members");
check("还没用的邀请列出来", r.text.includes("还没用的邀请（1）") && r.text.includes("没来的人"));
r = await admin.post(`/invites/${inviteId}/cancel`);
check("作废", r.status === 303 && env.DATA.json().invites.length === 0);
r = await browser().get("/join/" + unusedToken);
check("作废的链接不能用", r.status === 410);

// ---------- 移出 ----------
console.log("移出");
r = await admin.post(`/members/${editorId}/remove`);
check("移出学弟", r.status === 303 && r.location === "/members?ok=removed" && env.DATA.json().members.length === 2);
r = await editor.get("/");
check("被移出的人手机上马上进不来", r.text.includes("请找管理员要一个邀请链接"));
r = await laptop.get("/");
check("另一台设备也进不来", r.text.includes("请找管理员要一个邀请链接"));
r = await admin.get("/members?ok=removed");
check("成员页提示已经移出", r.text.includes("已经移出"));

// ---------- 两个管理员、移出自己 ----------
console.log("两个管理员");
r = await admin.post("/members/invite", { name: "合作者", role: "admin" });
const admin2 = browser();
await admin2.post("/join/" + linkFrom(r.text));
r = await admin2.post(`/members/${adminId}/role`, { role: "editor", publish: "1" });
check("有两个管理员时可以把另一个改成编辑", r.status === 303
  && env.DATA.json().members.find((m) => m.id === adminId).role === "editor");
r = await admin2.post(`/members/${adminId}/role`, { role: "admin" });
const admin2Id = env.DATA.json().members.find((m) => m.name === "合作者").id;
r = await admin2.post(`/members/${admin2Id}/remove`);
check("移出自己 → 回首页、进不来", r.status === 303 && r.location === "/");
r = await admin2.get("/");
check("移出自己以后是登录说明", r.text.includes("请找管理员要一个邀请链接"));

// ---------- 退出、过期 ----------
console.log("退出、过期");
r = await viewer.post("/logout");
check("退出：清掉 Cookie", r.status === 303 && viewer.jar.cookie === "");
r = await viewer.get("/");
check("退出以后是登录说明", r.text.includes("请找管理员要一个邀请链接"));
check("退出以后服务器上也删了这个登录", env.DATA.json().members.find((m) => m.name.includes("只读")).sessions.length === 0);
r = await admin.post("/members/invite", { name: "慢的人", role: "viewer" });
const slowToken = linkFrom(r.text);
now += 8 * DAY;
r = await browser().get("/join/" + slowToken);
check("8 天后邀请过期", r.status === 410);
const lastSeen = env.DATA.json().members.find((m) => m.id === adminId).lastSeen;
r = await admin.get("/");
check("隔了 12 小时以上再来：续期、记最后来过", r.status === 200 && env.DATA.json().members.find((m) => m.id === adminId).lastSeen === now
  && lastSeen < now);
now += 91 * DAY;
r = await admin.get("/");
check("91 天没来 → 登录过期", r.text.includes("请找管理员要一个邀请链接"));

// ---------- 两个人同时改 ----------
console.log("同时改");
const bucket = env.DATA;
let first = true;
const realPut = bucket.put.bind(bucket);
bucket.put = async (key, body, options) => {
  if (first) {
    first = false;
    await realPut(key, JSON.stringify({ ...bucket.json(), invites: [{ id: "bbbbbbbb", h: "x", name: "别人刚加的", role: "viewer",
      publish: false, created: now, createdBy: "别人", expires: now + DAY }] }));  // 别人抢先写了一次
  }
  return realPut(key, body, options);
};
await update(bucket, (data) => { data.invites.push({ id: "cccccccc", h: "y", name: "我加的", role: "viewer", publish: false,
  created: now, createdBy: "我", expires: now + DAY }); });
bucket.put = realPut;
const names = env.DATA.json().invites.map((i) => i.name);
check("后写的重新读一遍再改，两边都在", names.includes("别人刚加的") && names.includes("我加的"));

// ---------- 出错 ----------
console.log("出错");
const broken = await worker.fetch(new Request(ORIGIN + "/", { headers: { Cookie: `${COOKIE}=${"a".repeat(32)}` } }),
  { DATA: { get: async () => { throw new Error("R2 坏了"); } } });
check("存储桶出错 → 500 页面，不露出错误细节", broken.status === 500 && !(await broken.text()).includes("R2 坏了"));
r = await browser().go("PUT", "/");
check("别的请求方式 → 405", r.status === 405);
r = await browser().go("HEAD", "/");
check("HEAD（测速工具）照 GET 回响应头、没有内容", r.status === 200 && r.text === ""
  && r.headers.get("X-Robots-Tag") === "noindex, nofollow");

console.log(failures ? `\n${failures} / ${checks} 项没通过` : `\n全部通过（${checks} 项）`);
process.exit(failures ? 1 : 0);
