// 发布全部选手（2026-11-19 用户在「赛事页和看直播」窗口定的："默认职业选手就是主播"），和主舞台的直播间记忆（2026-11-21：
// 赛管毛毛要的"直播间记忆功能，保存主播直播间号，在发布赛事时直接选择"、"主播直播间最多八路，可能塞不下"；用户定的：观战并进主舞台、
// 主舞台最多 24 路）。由 test.mjs 运行。文件名还是 11-19 做"观战"时起的。
// 走一遍：发布时选手放全部（没比赛的队、直播间待填的也在）、队徽照旧只放用到的队、不再写 observers（存着的老数据也不发、保存时去掉）；
// 赛程页没有观战按钮；主舞台名字的下拉（记下的直播间 + 选手库里开直播的人）、只写名字时照名字补平台和房间号、认不出的名字、
// 记下的直播间（从已有的赛程算一份、用新名字、忘掉、权限）、新的一场 8 行空行、改完回到那一场。

import { hash, newToken, update } from "./auth.js";
import { EVENTS, LOG, MAIN_ROOMS, PLAYERS } from "./data.js";
import { MAIN_MAX } from "./esports.js";
import { APP_JS } from "./pages.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const HOUR = 3600 * 1000;
const now = Date.UTC(2026, 10, 20, 4, 0);  // 北京时间 11-20 12:00
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const P = () => env.DATA.json(PLAYERS, '{"items":[]}').items;
const E = () => env.DATA.json(EVENTS, '{"items":[]}').items;
const rooms = () => env.DATA.json(MAIN_ROOMS, '{"items":{}}').items;
const logs = () => env.DATA.json(LOG, '{"items":[]}').items.map((x) => x.text);

async function join(name, role) {
  const token = newToken();
  const h = await hash(token);
  await update(env.DATA, (data) => {
    data.invites.push({ id: Math.random().toString(16).slice(2, 10).padEnd(8, "0"), h, name, role, publish: role === "admin", created: now,
                        createdBy: "命令行", expires: now + 7 * 24 * HOUR });
  });
  const b = browser();
  await b.post(`/join/${token}`);
  return b;
}
const admin = await join("黄指导", "admin");
const editor = await join("毛毛", "editor");
const viewer = await join("看看", "viewer");

const add = (name, team, platform, room) => editor.post("/players/add", { name, team, platform, room });
await add("Kite", "星火", "douyu", "520013");
await add("Ace", "星火", "bilibili", "100");
await add("北一", "北辰", "douyin", "bei1");
await add("不播", "北辰", "none", "");
await add("皮特", "DF", "bilibili", "23097212");
await add("闲人", "没比赛的队", "huya", "777");
{  // 直播间待填的（网页上存不进来，只有从 Liquipedia 一键加进来的才这样）
  const d = env.DATA.json(PLAYERS);
  d.items.push({ id: "a0000001", name: "待填", team: "北辰", platform: "", room: "", liquipedia: "", rev: 1 });
  await env.DATA.put(PLAYERS, JSON.stringify(d));
}
await env.DATA.put("manual/teams.json", JSON.stringify({ items: { 星火: { logo: "aaaaaaaaaaaaaaaa.png" }, 没比赛的队: { logo: "bbbbbbbbbbbbbbbb.png" } } }));
// mains：主舞台的几行 [名字, 平台, 房间号]（表单上名字在最前面）
const form = (name, stage, teams, mains = [], more = []) => [["name", name], ["type", "联赛"], ["stage", stage], ["start", "2026-11-20T17:00"],
  ["end", "2026-11-20T20:00"], ...teams.map((t) => ["teams", t]),
  ...mains.flatMap(([n, platform, room]) => [["main_name", n], ["main_platform", platform], ["main_room", room]]), ...more];

// ---------- 从已有的赛程算一份"记下的直播间"（还没有 main_rooms.json 的时候） ----------
await editor.post("/events/add", form("LCQ胜者组第1轮", "A组", ["星火", "北辰"]));
{  // 11-21 以前存的赛程：有主舞台、还有 11-19 到 11-21 的"观战"
  const d = env.DATA.json(EVENTS);
  Object.assign(d.items[0], { main: [{ platform: "douyu", room: "9999", name: "快乐滋崩在这里" }],
                              observers: [{ player: P().find((p) => p.name === "皮特").id, team: "星火" }] });
  await env.DATA.put(EVENTS, JSON.stringify(d));
  await env.DATA.delete?.(MAIN_ROOMS);
}
const ev = () => E().find((e) => e.stage === "A组");

// ---------- 发布：选手放全部；不再写观战 ----------
let r = await admin.post("/publish/test");
let sent = env.PUBLIC.json("esports_test.json");
check("发布：选手放全部（没比赛的队、不直播、直播间待填的都在）", r.location === "/publish?ok=test" && sent.players.length === 7
  && sent.players.some((p) => p.name === "闲人" && p.platform === "huya") && sent.players.find((p) => p.name === "待填").platform === "none");
check("发布：队徽照旧只放用到的队", JSON.stringify(sent.teams) === JSON.stringify({ 星火: { logo: "aaaaaaaaaaaaaaaa.png" } }));
check("发布：不再写观战（存着的老数据也不发；11-21 观战并进了主舞台）", sent.events.every((e) => !("observers" in e))
  && sent.events[0].main.length === 1);

// ---------- 赛程页：没有观战；主舞台名字的下拉 ----------
r = await editor.get("/events");
check("赛程页上没有观战的按钮、没有观战的字样", !r.text.includes("/observers") && !r.text.includes("观战：") && !r.text.includes("streamer-names"));
const list = r.text.split('<datalist id="main-rooms">')[1]?.split("</datalist>")[0] ?? "";
check("主舞台名字的下拉：记下的直播间（从已有的赛程算的）+ 选手库里开直播的人；不直播、待填的不在",
  list.includes('value="快乐滋崩在这里" label="斗鱼 9999"') && list.includes('value="皮特" label="B站 23097212 · 选手库（DF）"')
  && list.includes('data-p="bilibili" data-r="23097212"') && !list.includes('value="不播"') && !list.includes('value="待填"')
  && list.indexOf("快乐滋崩在这里") < list.indexOf("皮特"));
check("名字那一格用这个下拉、排在平台前面", /<input class="input" name="main_name" value="" list="main-rooms"[^>]*>\s*<select class="input" name="main_platform"/.test(r.text));
const addForm = r.text.split('id="add"')[1].split("</details>")[0];
check("新的一场：主舞台 8 行空行；写着最多 24 路、老版本只显示前 8 路", (addForm.match(/name="main_name"/g) ?? []).length === 8
  && addForm.includes(`最多 ${MAIN_MAX} 路`) && MAIN_MAX === 24 && addForm.includes("0.7.0 以前的 Evo 只显示前 8 路"));
const editForm = r.text.split(`action="/events/${ev().id}/save"`)[1].split("</form>")[0];
check("改的时候：已有的 1 行 + 4 行空行", (editForm.match(/name="main_name"/g) ?? []).length === 5);
check("记下的直播间列在赛程页最下面", r.text.includes("记下的直播间（1）") && r.text.includes('value="douyu-9999"'));
check("/app.js：从下拉里选了就填平台和房间号", APP_JS.includes("input[name=main_name]") && APP_JS.includes("option.dataset.p"));
r = await viewer.get("/events");
check("只读的：没有下拉、没有忘掉的勾", !r.text.includes('<datalist id="main-rooms">') && !r.text.includes('name="forget"') && r.text.includes("记下的直播间（1）"));

// ---------- 只写名字：照名字补平台和房间号 ----------
r = await editor.post(`/events/${ev().id}/save`, form("LCQ胜者组第1轮", "A组", ["星火", "北辰"],
  [["快乐滋崩在这里", "", ""], ["皮特", "", ""], ["kite", "", ""], ["官方直播", "bilibili", "7734200"], ["", "", ""]], [["rev", String(ev().rev)]]));
check("保存：只写名字的照记下的直播间、选手库补上（大小写不管）；新的照填的；改完回到这一场", r.location === `/events?ok=saved#e-${ev().id}`
  && JSON.stringify(ev().main) === JSON.stringify([{ platform: "douyu", room: "9999", name: "快乐滋崩在这里" },
    { platform: "bilibili", room: "23097212", name: "皮特" }, { platform: "douyu", room: "520013", name: "kite" },
    { platform: "bilibili", room: "7734200", name: "官方直播" }]));
check("保存时顺手去掉 11-19 的观战", !("observers" in ev()));
check("记下的直播间：新填的都记下（选手库里的人也记，名字用这次填的）", rooms()["bilibili-7734200"]?.name === "官方直播"
  && rooms()["douyu-9999"]?.name === "快乐滋崩在这里" && rooms()["bilibili-23097212"]?.name === "皮特" && Object.keys(rooms()).length === 4);
r = await editor.get("/events");
check("页面上那一行有编号，地址后面的 #e-… 跳得到", r.text.includes(`<li class="row" id="e-${ev().id}">`));
r = await editor.post(`/events/${ev().id}/save`, form("LCQ胜者组第1轮", "A组", ["星火", "北辰"], [["没有这个人", "", ""]], [["rev", String(ev().rev)]]));
check("只写了名字、哪里都没有 → 说清楚，不存", r.status === 400 && r.text.includes("主舞台「没有这个人」：记下的直播间和选手库里都没有这个名字")
  && ev().main.length === 4);
r = await editor.post("/events/add", form("新的一场", "B组", ["星火"], [["官方直播", "", ""]]));
check("添加赛事也能只写名字", r.location === "/events?ok=added" && E().find((e) => e.stage === "B组").main[0].room === "7734200");
await editor.post(`/events/${ev().id}/save`, form("LCQ胜者组第1轮", "A组", ["星火", "北辰"],
  [["官方直播间", "bilibili", "7734200"]], [["rev", String(ev().rev)]]));
check("同一个直播间换了名字 → 记新的名字", rooms()["bilibili-7734200"].name === "官方直播间" && Object.keys(rooms()).length === 4);

// ---------- 24 路 ----------
const many = Array.from({ length: 24 }, (_, i) => [`第 ${i + 1} 路`, "bilibili", String(5000 + i)]);
r = await editor.post("/events/add", form("大赛", "C组", ["星火"], many));
check("24 路存得下，发布出去也是 24 路", r.location === "/events?ok=added" && E().find((e) => e.stage === "C组").main.length === 24);
await admin.post("/publish/test");
check("（发布的文件里 24 路都在）", env.PUBLIC.json("esports_test.json").events.find((e) => e.stage === "C组").main.length === 24);

// ---------- 忘掉记下的直播间 ----------
r = await viewer.post("/events/rooms/forget", [["forget", "bilibili-7734200"]]);
check("只读的不能忘掉", r.status === 403 && rooms()["bilibili-7734200"]);
r = await editor.post("/events/rooms/forget", []);
check("一个都没勾 → 提示", r.status === 400 && r.text.includes("没有勾选要忘掉的直播间"));
r = await editor.post("/events/rooms/forget", [["forget", "bilibili-7734200"], ["forget", "douyu-9999"], ["forget", "没有这个"]]);
check("忘掉勾选的：下拉里没了、记下来；赛程里已经填着的不动", r.location === "/events?ok=forgot#rooms" && !rooms()["bilibili-7734200"]
  && !rooms()["douyu-9999"] && logs()[0] === "忘掉记下的直播间：官方直播间（B站 7734200）、快乐滋崩在这里（斗鱼 9999）"
  && ev().main[0].room === "7734200");
r = await editor.get("/events");
check("（下拉里不再有它们）", !r.text.includes('value="官方直播间" label') && !r.text.includes('value="快乐滋崩在这里" label'));
