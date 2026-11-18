// 队徽的测试（2026-11-18）。由 test.mjs 运行。
// 走一遍：认图片（PNG、三种 WebP，不认 SVG、JPG）、队标库的图要登录才看得到、按队名猜、搜、从队标库挑（复制到公开存储桶、
// 永久缓存、已经有的不重写）、上传（太大、太宽、不是图、没选）、去掉、权限、战队改名跟着搬、选手库上显示、发布时带上队徽、
// 页面允许加载下载服务器上的图。

import { hash, newToken, update } from "./auth.js";
import { LIBRARY_INDEX, TEAMS, imageInfo, libraryKey, sha16 } from "./logos.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const MINUTE = 60 * 1000;
let now = Date.UTC(2026, 10, 18, 4, 0);
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const doc = (key, fallback = "{}") => env.DATA.json(key, fallback);

async function join(name, role, publish = false) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish, created: now,
                        createdBy: "命令行", expires: now + 7 * 24 * 60 * MINUTE });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}

// ---------- 造几张"图"（只有文件头，够认种类和宽高） ----------
function png(w, h, size = 64) {
  const b = new Uint8Array(Math.max(size, 33));
  b.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52]);
  new DataView(b.buffer).setUint32(16, w);
  new DataView(b.buffer).setUint32(20, h);
  return b;
}
function webp(kind, w, h) {
  const b = new Uint8Array(64);
  const text = (i, s) => b.set([...s].map((c) => c.charCodeAt(0)), i);
  text(0, "RIFF");
  text(8, "WEBP");
  text(12, kind);
  if (kind === "VP8X") {
    b.set([(w - 1) & 255, ((w - 1) >> 8) & 255, (w - 1) >> 16, (h - 1) & 255, ((h - 1) >> 8) & 255, (h - 1) >> 16], 24);
  } else if (kind === "VP8L") {
    const bits = (w - 1) | ((h - 1) << 14);
    b.set([0x2f, bits & 255, (bits >> 8) & 255, (bits >> 16) & 255, (bits >>> 24) & 255], 20);
  } else {
    b.set([0x9d, 0x01, 0x2a, w & 255, w >> 8, h & 255, h >> 8], 23);
  }
  return b;
}
check("认 PNG 的宽高", JSON.stringify(imageInfo(png(192, 100))) === '{"type":"png","w":192,"h":100}');
check("认 WebP（VP8X、VP8L、VP8）", imageInfo(webp("VP8X", 200, 100)).w === 200 && imageInfo(webp("VP8L", 256, 31)).h === 31
  && imageInfo(webp("VP8 ", 120, 60)).w === 120);
check("不认 SVG、JPG", imageInfo(new TextEncoder().encode("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")) === null
  && imageInfo(new Uint8Array([0xff, 0xd8, 0xff, 0xe0, ...new Array(40).fill(0)])) === null);

// ---------- 队标库、选手库 ----------
const LIQ = "aaaaaaaaaaaaaaaa.png";
const library = [{ page: "Team_Liquid", name: "Team Liquid", file: LIQ, w: 192, h: 100, bytes: 64 },
                 { page: "Wolves_Esports", name: "Wolves Esports", file: "bbbbbbbbbbbbbbbb.png", w: 192, h: 192, bytes: 64 },
                 { page: "Alliance", name: "Alliance", file: "cccccccccccccccc.png", w: 150, h: 192, bytes: 64 }];
await env.DATA.put(LIBRARY_INDEX, JSON.stringify({ version: 1, items: library }));
for (const x of library) await env.DATA.put(libraryKey(x.file), png(x.w, x.h));

const admin = await join("黄指导", "admin");
const editor = await join("Momona", "editor");
const viewer = await join("看看", "viewer");
let room = 100;
for (const [team, names] of [["Team Liquid", ["L1", "L2", "L3"]], ["星火电竞", ["X1", "X2", "X3"]]]) {
  for (const name of names) await editor.post("/players/add", { name, team, platform: "douyu", room: String(room++) });
}

let r = await viewer.get("/players");
check("选手库：每支队旁边有加队徽的入口", r.text.includes(`href="/logo?team=${encodeURIComponent("Team Liquid")}"`) && r.text.includes("加队徽"));
check("页面允许加载下载服务器上的队徽", /img-src 'self' data: blob: https:\/\/dl\.evotracker\.cn/.test(r.headers.get("Content-Security-Policy")));
r = await viewer.get(`/logo?team=${encodeURIComponent("Team Liquid")}`);
check("队徽页：按队名猜到队标库里的 Team Liquid", r.status === 200 && r.text.includes(`/logo-library/${LIQ}`) && r.text.includes("按队名猜的"));
check("只读的看不到用这个、上传", !r.text.includes("/logo/pick") && !r.text.includes("/logo/upload"));
r = await viewer.get(`/logo?team=${encodeURIComponent("星火电竞")}&q=wolves`);
check("搜队标库", r.text.includes("bbbbbbbbbbbbbbbb.png") && !r.text.includes("cccccccccccccccc.png"));
r = await viewer.get("/logo?team=nope");
check("选手库里没有的队 → 404", r.status === 404);
r = await viewer.get(`/logo-library/${LIQ}`);
check("登录了能看队标库的图", r.status === 200 && r.headers.get("Content-Type") === "image/png");
r = await browser().get(`/logo-library/${LIQ}`);
check("没登录看不到队标库的图", r.status === 401);
r = await viewer.get("/logo-library/../members.json");
check("乱写的文件名 → 没有这个页面", r.status === 404);

// ---------- 从队标库挑 ----------
r = await viewer.post("/logo/pick", { team: "Team Liquid", file: LIQ });
check("只读的不能换队徽", r.status === 403);
r = await editor.post("/logo/pick", { team: "Team Liquid", file: LIQ });
check("挑一张 → 回队徽页", r.location === `/logo?team=${encodeURIComponent("Team Liquid")}&ok=logo`);
const published = env.PUBLIC.objects.get(`esports/logos/${LIQ}`);
check("复制到公开存储桶的 esports/logos/、永久缓存", published && published.options.httpMetadata.contentType === "image/png"
  && published.options.httpMetadata.cacheControl === "public, max-age=31536000, immutable");
check("记下哪支队用哪张、从哪来、谁换的", doc(TEAMS).items["Team Liquid"].logo === LIQ
  && doc(TEAMS).items["Team Liquid"].source === "派圈的队标库：Team Liquid" && doc(TEAMS).items["Team Liquid"].by === "Momona");
const putsBefore = env.PUBLIC.puts;
await admin.post("/logo/pick", { team: "Team Liquid", file: LIQ });
check("已经有的图不再写一遍", env.PUBLIC.puts === putsBefore);
r = await viewer.get("/players");
check("选手库上显示队徽（从下载服务器加载）", r.text.includes(`src="https://dl.evotracker.cn/esports/logos/${LIQ}"`));
r = await editor.post("/logo/pick", { team: "Team Liquid", file: "dddddddddddddddd.png" });
check("队标库里没有的图 → 提示", r.status === 400 && r.text.includes("队标库里没有这张图"));
r = await editor.post("/logo/pick", { team: "没有的队", file: LIQ });
check("选手库里没有的队 → 提示", r.status === 400 || r.status === 404);

// ---------- 上传 ----------
const upload = (b, team, bytes, name = "logo.webp") => {
  const form = new FormData();
  form.set("team", team);
  if (bytes) form.set("file", new Blob([bytes]), name);
  return b.post("/logo/upload", form);
};
const good = webp("VP8X", 200, 100);
r = await upload(editor, "星火电竞", good);
const goodName = `${await sha16(good)}.webp`;
check("上传 → 名字是内容哈希、放进公开存储桶", r.status === 303 && env.PUBLIC.objects.has(`esports/logos/${goodName}`)
  && doc(TEAMS).items["星火电竞"].logo === goodName && doc(TEAMS).items["星火电竞"].source === "上传的");
check("上传的 WebP 存成 image/webp", env.PUBLIC.objects.get(`esports/logos/${goodName}`).options.httpMetadata.contentType === "image/webp");
r = await upload(editor, "星火电竞", png(100, 100, 61 * 1024));
check("超过 60 KB → 提示", r.status === 400 && r.text.includes("图片太大了"));
r = await upload(editor, "星火电竞", png(300, 120));
check("最长边超过 256 → 提示", r.status === 400 && r.text.includes("最长边要在 256 像素以内"));
r = await upload(editor, "星火电竞", new TextEncoder().encode("<svg></svg>".padEnd(64)), "a.svg");
check("SVG → 不收", r.status === 400 && r.text.includes("只收 PNG、WebP"));
r = await upload(editor, "星火电竞", null);
check("没选图 → 提示", r.status === 400 && r.text.includes("没有选图片"));
r = await upload(viewer, "星火电竞", good);
check("只读的不能上传", r.status === 403);

// ---------- 去掉、改名、发布 ----------
r = await editor.post("/logo/remove", { team: "星火电竞" });
check("去掉队徽", r.location.endsWith("&ok=unlogo") && !doc(TEAMS).items["星火电竞"]);
r = await editor.post("/logo/remove", { team: "星火电竞" });
check("没有队徽的去掉 → 提示", r.status === 400);
await editor.post("/teams/rename", { from: "Team Liquid", to: "TL" });
check("战队改名：队徽跟着搬", doc(TEAMS).items.TL?.logo === LIQ && !doc(TEAMS).items["Team Liquid"]);
await editor.post("/events/add", [["name", "训练赛"], ["type", "训练赛"], ["stage", ""], ["start", "2026-11-18T19:00"],
  ["end", "2026-11-18T22:00"], ["teams", "TL"], ["teams", "星火电竞"]]);
r = await admin.get("/publish");
check("发布页写着几支队有队徽", r.text.includes("1 支队有队徽"));
await admin.post("/publish/test");
const file = JSON.parse(env.PUBLIC.objects.get("esports_test.json").body);
check("发布：esports.json 带上用到的队的队徽（只有文件名）", JSON.stringify(file.teams) === JSON.stringify({ TL: { logo: LIQ } })
  && Object.keys(file).indexOf("teams") > Object.keys(file).indexOf("events"));
check("改动记录写明", doc("log.json").items.some((x) => x.text === "给「Team Liquid」换了队徽（派圈的队标库：Team Liquid）")
  && doc("log.json").items.some((x) => x.text === "去掉了「星火电竞」的队徽"));
