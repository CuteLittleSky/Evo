// 从 Liquipedia 一键加进选手库的测试（2026-11-18）。由 test.mjs 运行。
// 走一遍：选手资料里认出直播间（斗鱼、虎牙、B站直播间、抖音直播间；个人主页、短链、Twitch 不算）、收录的比赛上的按钮和权限、
// 加进来的名字（手动修正过的用修正的）、队名、页面名、直播间自动填（别人用着的房间不填）、没有的标"直播间待填"、已经有的跳过、
// 待填的拦着发布、补上直播间、战队页加现役选手、名单还没抓到时提示。

import { hash, newToken, update } from "./auth.js";
import { PLAYERS } from "./data.js";
import { normPlayer } from "./liquipedia.js";
import { INDEX, TRACKED, eventKey } from "./lpsync.js";
import { OVERRIDES } from "./overrides.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const MINUTE = 60 * 1000;
const now = Date.UTC(2026, 10, 18, 4, 0);
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const players = () => env.DATA.json(PLAYERS, '{"items":[]}').items;
const find = (name) => players().find((p) => p.name === name);

async function join(name, role) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish: role === "admin", created: now,
                        createdBy: "命令行", expires: now + 7 * 24 * 60 * MINUTE });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}

// ---------- 认直播间 ----------
const p = normPlayer({ pagename: "3Mz", id: "3Mz", links: { vod: "https://v.douyu.com/author-replay/x", bilibili: "https://space.bilibili.com/38979291",
  douyin_page: "https://v.douyin.com/iM1VAd7L", douyu: "https://www.douyu.com/1667826", huyatv: "https://www.huya.com/18957845",
  bilibili2: "https://live.bilibili.com/21452505?broadcast_type=0", douyin: "https://live.douyin.com/XXFPS", twitch: "https://www.twitch.tv/x" } });
check("认出能用的直播间（抖音转小写），个人主页、短链、Twitch 不算", JSON.stringify(p.streams) === JSON.stringify([
  { platform: "douyu", room: "1667826" }, { platform: "huya", room: "18957845" }, { platform: "bilibili", room: "21452505" },
  { platform: "douyin", room: "xxfps" }]));
check("没有 links 的选手：没有直播间", normPlayer({ pagename: "x" }).streams.length === 0);

// ---------- 准备：抓到的资料、收录的比赛、选手库里已经有的 ----------
const EVENT = "ALGS/2026/LCQ";
await env.DATA.put(INDEX, JSON.stringify({
  events: { [EVENT]: { page: EVENT, name: "LCQ", rosterTeams: 2, rosterPlayers: 6, rosterAt: now } },
  players: {
    Ggod: { page: "Ggod", id: "Ggod", teamPage: "Wolves_Esports", team: "Wolves Esports", streams: [{ platform: "douyu", room: "111" }, { platform: "huya", room: "222" }] },
    YY: { page: "YY", id: "YY", teamPage: "Wolves_Esports", team: "Wolves Esports", streams: [{ platform: "bilibili", room: "333" }] },
    Chisu: { page: "Chisu", id: "Chisu", teamPage: "Wolves_Esports", team: "Wolves Esports", streams: [] },
    "Old_(player)": { page: "Old_(player)", id: "Old", teamPage: "Falcons", team: "Falcons", streams: [{ platform: "douyu", room: "444" }] },
    Fresh: { page: "Fresh", id: "Fresh", teamPage: "Falcons", team: "Falcons", streams: [{ platform: "douyin", room: "abc_1" }] },
    Third: { page: "Third", id: "Third", teamPage: "Falcons", team: "Falcons" },
  },
  teams: { Wolves_Esports: { page: "Wolves_Esports", name: "Wolves Esports" }, Falcons: { page: "Falcons", name: "Falcons" } },
}));
await env.DATA.put(eventKey(EVENT), JSON.stringify({ page: EVENT, teams: [
  { team: "Wolves", place: "1", players: [{ page: "Ggod", id: "Ggod" }, { page: "YY", id: "YY" }, { page: "Chisu", id: "Chisu" }] },
  { team: "Team Falcons", place: "2", players: [{ page: "Old_(player)", id: "Old" }, { page: "Fresh", id: "Fresh" }, { page: "Third", id: "Third" }] }] }));
await env.DATA.put(TRACKED, JSON.stringify({ items: [{ page: EVENT, addedBy: "黄指导", addedAt: now }, { page: "No/Roster", addedBy: "黄指导", addedAt: now }] }));
await env.DATA.put(OVERRIDES, JSON.stringify({ items: { YY: { id: { value: "YY_new", reason: "改名", by: "黄指导", at: now, source: "YY" } } } }));

const admin = await join("黄指导", "admin");
const editor = await join("Momona", "editor");
const viewer = await join("看看", "viewer");
await editor.post("/players/add", { name: "老人", team: "老队", platform: "douyu", room: "111" });               // 占着 Ggod 的斗鱼房间
await editor.post("/players/add", { name: "Old 本人", team: "老队", platform: "douyu", room: "9", liquipedia: "Old_(player)" });  // 已经连上了

// ---------- 收录的比赛：加进选手库 ----------
let r = await viewer.get("/liquipedia");
check("只读的看不到加进选手库", !r.text.includes("/liquipedia/to-roster"));
r = await editor.get("/liquipedia");
check("收录的比赛上有加进选手库（名单抓到了才有）", (r.text.match(/action="\/liquipedia\/to-roster"/g) ?? []).length === 1);
r = await viewer.post("/liquipedia/to-roster", { page: EVENT });
check("只读的不能加", r.status === 403);
r = await editor.post("/liquipedia/to-roster", { page: EVENT });
check("加进选手库 → 回选手库，说加了几位、几位待填、跳过几位", r.location === "/players?ok=fromlp&added=5&pending=2&skipped=1");
check("名字用手动修正过的 ID、队名用名单上的、页面名填好", find("YY_new")?.team === "Wolves" && find("YY_new").liquipedia === "YY"
  && find("Fresh").team === "Team Falcons");
check("直播间：别人占着的斗鱼不填、用下一个虎牙", find("Ggod").platform === "huya" && find("Ggod").room === "222");
check("直播间：B站直播间、抖音直播间自动填", find("YY_new").platform === "bilibili" && find("YY_new").room === "333"
  && find("Fresh").platform === "douyin" && find("Fresh").room === "abc_1");
check("没有直播间的标待填（平台、房间号空着）", find("Chisu").platform === "" && find("Chisu").room === "" && find("Third").platform === "");
check("选手库里已经连上的跳过", !players().some((x) => x.name === "Old"));
r = await editor.get(r.location);
check("选手库上提示加了几位、标着直播间待填", r.text.includes("从 Liquipedia 加进来 5 位（2 位直播间待填") && r.text.includes("直播间待填</span>")
  && r.text.includes("「Chisu」的直播间还没填"));
r = await editor.post("/liquipedia/to-roster", { page: EVENT });
check("再点一次：都跳过", r.location === "/players?ok=fromlp&added=0&pending=0&skipped=6");
r = await editor.post("/liquipedia/to-roster", { page: "No/Roster" });
check("名单还没抓到 → 提示", r.status === 400 && r.text.includes("名单还没抓到"));

// ---------- 待填的拦着发布、补上 ----------
await editor.post("/events/add", [["name", "LCQ"], ["type", "杯赛"], ["stage", ""], ["start", "2026-11-18T19:00"], ["end", "2026-11-18T22:00"],
  ["teams", "Wolves"], ["teams", "Team Falcons"]]);
r = await admin.get("/publish");
check("有直播间待填的 → 不能发布", r.text.includes("还不能发布") && r.text.includes("「Third」的直播间还没填"));
r = await editor.get("/players");
check("待填的人编辑时平台先是请选平台", r.text.includes('<option value="" selected>请选平台</option>'));
r = await editor.post(`/players/${find("Chisu").id}/save`, { name: "Chisu", team: "Wolves", platform: "", room: "" });
check("平台还空着就保存 → 提示选一个", r.status === 400 && r.text.includes("选一个平台"));
await editor.post(`/players/${find("Chisu").id}/save`, { name: "Chisu", team: "Wolves", platform: "none", room: "", liquipedia: "Chisu" });
await editor.post(`/players/${find("Third").id}/save`, { name: "Third", team: "Team Falcons", platform: "douyu", room: "555", liquipedia: "Third" });
r = await admin.get("/publish");
check("补上以后不再提示直播间还没填", !r.text.includes("直播间还没填"));

// ---------- 战队页：把现役选手加进选手库 ----------
r = await editor.get("/teams/Falcons");
check("战队页：标出已在选手库的、按钮写着还有几人没加", r.text.includes("已在选手库") && r.text.includes("把现役选手加进选手库（0 人）") === false);
await env.DATA.put(PLAYERS, JSON.stringify({ version: 1, items: players().filter((x) => x.team !== "Team Falcons") }));
r = await editor.get("/teams/Falcons");
check("战队页：没加的有按钮（写着几人）", r.text.includes("把现役选手加进选手库（2 人）"));
r = await viewer.post("/teams/Falcons/to-roster");
check("只读的不能加", r.status === 403);
r = await editor.post("/teams/Falcons/to-roster");
check("战队页加进来：队名用 Liquipedia 的队名", r.location === "/players?ok=fromlp&added=2&pending=1&skipped=1" && find("Fresh").team === "Falcons");
check("改动记录写明从哪加的", env.DATA.json("log.json").items[0].text.startsWith("从 Liquipedia 加进选手库（战队 Falcons）：新加 2 位"));
