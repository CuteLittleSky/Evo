// 成员、邀请链接、登录（2026-11-17 第 1 步）。说明见 worker.js 开头。
//
// 所有成员和邀请都存在 R2 里的一个文件 members.json（人少，一个文件就够）：
//   {"version": 1,
//    "members": [{"id", "name", "role": "admin"|"editor"|"viewer", "publish": 能不能发布到 Evo,
//                 "created", "createdBy", "lastSeen", "sessions": [{"h", "device", "created", "seen", "expires"}]}],
//    "invites": [{"id", "h", "name", "role", "publish", "created", "createdBy", "expires", "memberId"?}]}
// 时间都是毫秒（Date.now()）。链接里的口令只存它的 SHA-256（h），原文只出现在发给对方的链接里：
// 文件被人看到也拿不到能登录的东西。memberId 是"给已有成员发新链接"（换了手机、电脑）：用了以后登录成原来那个人。
// 改文件用 R2 的 etag 做条件写入：两个人同时改，后写的那个重新读一遍再改，不会冲掉别人的改动。
// tools/team.py 在命令行里改同一个文件（第一次生成管理员链接），格式要和这里一样。

import { Refuse, randomHex, readDoc, updateDoc } from "./store.js";

export const ROLES = { admin: "管理员", editor: "编辑", viewer: "只读" };
export const COOKIE = "__Host-evo_team";
export const KEY = "members.json";
const DAY = 24 * 3600 * 1000;
export const INVITE_DAYS = 7;
export const SESSION_DAYS = 90;
const TOUCH_MS = 12 * 3600 * 1000;  // 登录状态最多 12 小时续一次期、记一次"最后来过"（每次打开都写太浪费）
const MAX_SESSIONS = 10;             // 一个人最多同时在几台设备上登录，多了挤掉最早的
const MAX_MEMBERS = 50;
const MAX_INVITES = 30;
const NAME_MAX = 20;
const ID = /^[0-9a-f]{8}$/;
const TOKEN = /^[A-Za-z0-9_-]{32}$/;

export { Refuse };

// 链接里的口令：24 个随机字节，写成 32 个字母数字（base64url）
export function newToken() {
  const raw = crypto.getRandomValues(new Uint8Array(24));
  return btoa(String.fromCharCode(...raw)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function hash(token) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function validToken(token) {
  return typeof token === "string" && TOKEN.test(token);
}

export function validId(id) {
  return typeof id === "string" && ID.test(id);
}

function empty() {
  return { version: 1, members: [], invites: [] };
}

// 读 members.json；还没有这个文件（刚部署）就当作没有人
export function load(bucket) {
  return readDoc(bucket, KEY, empty);
}

// 读 → change(data) 改 → 写回去（见 store.js）
export function update(bucket, change) {
  return updateDoc(bucket, KEY, empty, change);
}

// 过期的邀请、过期的登录顺手清掉
function prune(data, now) {
  data.invites = data.invites.filter((i) => i.expires > now);
  for (const m of data.members) m.sessions = (m.sessions ?? []).filter((s) => s.expires > now);
}

// "手机 · 微信"这样的一小段，成员页上看是哪台设备。只记这几个字，不存完整的浏览器信息
export function deviceLabel(userAgent) {
  const ua = String(userAgent ?? "");
  const kind = /Mobile|Android|iPhone|iPad/i.test(ua) ? "手机" : "电脑";
  const browser = /MicroMessenger/i.test(ua) ? "微信" : /QQ\//i.test(ua) ? "QQ" : /Edg\//.test(ua) ? "Edge"
    : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "浏览器";
  return `${kind} · ${browser}`;
}

export function cleanName(value) {
  const name = String(value ?? "").replace(/[\u0000-\u001f\u007f]/g, "").replace(/\s+/g, " ").trim();
  if (!name) throw new Refuse("名字不能空着");
  if ([...name].length > NAME_MAX) throw new Refuse(`名字最多 ${NAME_MAX} 个字`);
  return name;
}

// 管理员什么都能做（能发布）；只读的人不能发布
function cleanRole(role, publish) {
  if (!Object.hasOwn(ROLES, role)) throw new Refuse("身份只能是管理员、编辑、只读");
  return { role, publish: role === "admin" ? true : role === "editor" ? Boolean(publish) : false };
}

export function can(member, action) {
  if (!member) return false;
  if (action === "members") return member.role === "admin";
  if (action === "edit") return member.role === "admin" || member.role === "editor";
  if (action === "publish") return member.role === "admin" || (member.role === "editor" && member.publish);
  return false;  // 不认识的动作一律不行（登录了就能看，不用问）
}

function admins(data) {
  return data.members.filter((m) => m.role === "admin");
}

function findMember(data, id) {
  const member = data.members.find((m) => m.id === id);
  if (!member) throw new Refuse("没有这个成员（可能刚被别人移出了）");
  return member;
}

// ---------- 登录状态 ----------

export function readCookie(request, name) {
  for (const part of (request.headers.get("Cookie") ?? "").split(";")) {
    const at = part.indexOf("=");
    if (at > 0 && part.slice(0, at).trim() === name) return part.slice(at + 1).trim();
  }
  return null;
}

export function sessionCookie(token) {
  return `${COOKIE}=${token}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${SESSION_DAYS * 24 * 3600}`;
}

export function clearCookie() {
  return `${COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0`;
}

// 这次请求是谁：{member, data, sessionHash}，没登录（或登录已过期、已被移出）是 null
export async function whoami(bucket, request, now) {
  const token = readCookie(request, COOKIE);
  if (!validToken(token)) return null;
  const h = await hash(token);
  const { data } = await load(bucket);
  const member = data.members.find((m) => (m.sessions ?? []).some((s) => s.h === h && s.expires > now));
  if (!member) return null;
  const session = member.sessions.find((s) => s.h === h);
  if (now - session.seen > TOUCH_MS) {
    try {
      await update(bucket, (fresh) => {
        const m = fresh.members.find((x) => x.id === member.id);
        const s = m?.sessions?.find((x) => x.h === h);
        if (s) {
          s.seen = now;
          s.expires = now + SESSION_DAYS * DAY;
          m.lastSeen = now;
        }
      });
    } catch {
      // 续期没写上不要紧，下次再续
    }
  }
  return { member, data, sessionHash: h };
}

export async function logout(bucket, sessionHash) {
  await update(bucket, (data) => {
    for (const m of data.members) m.sessions = (m.sessions ?? []).filter((s) => s.h !== sessionHash);
  });
}

// ---------- 邀请 ----------

// 管理员发邀请：返回链接里的口令（原文只在这一次出现）。memberId：给已有成员发新链接
export async function createInvite(bucket, by, form, now) {
  const token = newToken();
  const h = await hash(token);
  const invite = await update(bucket, (data) => {
    prune(data, now);
    let fields;
    if (form.memberId) {
      const member = findMember(data, form.memberId);
      data.invites = data.invites.filter((i) => i.memberId !== member.id);  // 一个人只留最新的一个链接
      fields = { name: member.name, role: member.role, publish: member.publish, memberId: member.id };
    } else {
      if (data.members.length >= MAX_MEMBERS) throw new Refuse(`成员最多 ${MAX_MEMBERS} 个`);
      fields = { name: cleanName(form.name), ...cleanRole(form.role, form.publish) };
    }
    if (data.invites.length >= MAX_INVITES) throw new Refuse("还没用的邀请太多了，先作废几个");
    const record = { id: randomHex(4), h, ...fields, created: now, createdBy: by.name, expires: now + INVITE_DAYS * DAY };
    data.invites.push(record);
    return record;
  });
  return { token, invite };
}

// 打开链接时看一眼是谁的邀请（不用掉）。无效、过期、已经用过都是 null
export async function findInvite(bucket, token, now) {
  if (!validToken(token)) return null;
  const h = await hash(token);
  const { data } = await load(bucket);
  const invite = data.invites.find((i) => i.h === h && i.expires > now);
  if (!invite) return null;
  if (invite.memberId && !data.members.some((m) => m.id === invite.memberId)) return null;
  return invite;
}

// 点"进入"：用掉邀请，登录。返回 {member, token}（token 放进 Cookie），邀请不能用了返回 null
export async function acceptInvite(bucket, token, now, userAgent) {
  if (!validToken(token)) return null;
  const h = await hash(token);
  const sessionToken = newToken();
  const sessionHash = await hash(sessionToken);
  const member = await update(bucket, (data) => {
    prune(data, now);
    const invite = data.invites.find((i) => i.h === h);
    if (!invite) return null;
    data.invites = data.invites.filter((i) => i !== invite);
    let m;
    if (invite.memberId) {
      m = data.members.find((x) => x.id === invite.memberId);
      if (!m) return null;
    } else {
      if (data.members.length >= MAX_MEMBERS) throw new Refuse(`成员最多 ${MAX_MEMBERS} 个`);
      m = { id: randomHex(4), name: invite.name, role: invite.role, publish: invite.publish,
            created: now, createdBy: invite.createdBy, lastSeen: now, sessions: [] };
      data.members.push(m);
    }
    m.sessions.push({ h: sessionHash, device: deviceLabel(userAgent), created: now, seen: now,
                      expires: now + SESSION_DAYS * DAY });
    m.sessions = m.sessions.slice(-MAX_SESSIONS);
    m.lastSeen = now;
    return m;
  });
  return member ? { member, token: sessionToken } : null;
}

export async function cancelInvite(bucket, inviteId) {
  await update(bucket, (data) => {
    data.invites = data.invites.filter((i) => i.id !== inviteId);
  });
}

// ---------- 成员 ----------

export async function setRole(bucket, memberId, form) {
  await update(bucket, (data) => {
    const member = findMember(data, memberId);
    const next = cleanRole(form.role, form.publish);
    if (member.role === "admin" && next.role !== "admin" && admins(data).length === 1) {
      throw new Refuse("至少要留一个管理员：先把别人设成管理员，再改这个人");
    }
    Object.assign(member, next);
    // 改了身份，他还没用的"新链接"也跟着改
    for (const invite of data.invites) if (invite.memberId === member.id) Object.assign(invite, next);
  });
}

export async function removeMember(bucket, memberId) {
  await update(bucket, (data) => {
    const member = findMember(data, memberId);
    if (member.role === "admin" && admins(data).length === 1) {
      throw new Refuse("至少要留一个管理员：这是最后一个管理员，不能移出");
    }
    data.members = data.members.filter((m) => m !== member);
    data.invites = data.invites.filter((i) => i.memberId !== member.id);
  });
}
