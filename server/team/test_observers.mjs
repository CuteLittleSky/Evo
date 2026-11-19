// 发布全部选手、赛事里"观战的人"的测试（2026-11-19 用户在「赛事页和看直播」窗口定的："默认职业选手就是主播"，
// "给正在 OB 的职业选手一个观战的标签……每场比赛里手动放"）。由 test.mjs 运行。
// 走一遍：发布时选手放全部（没比赛的队、直播间待填的也在）、队徽照旧只放用到的队；观战：权限、保存、写错的几种、rev、
// 发布出去的写法（按选手的 id，换了直播间也跟着变）、Evo 读的规则、页面上显示、改赛事去掉了队、战队改名、删选手、复制不带观战。

import { hash, newToken, update } from "./auth.js";
import { EVENTS, LOG, PLAYERS } from "./data.js";
import { clean } from "./esports.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const HOUR = 3600 * 1000;
const now = Date.UTC(2026, 10, 20, 4, 0);  // 北京时间 11-20 12:00
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const P = () => env.DATA.json(PLAYERS, '{"items":[]}').items;
const E = () => env.DATA.json(EVENTS, '{"items":[]}').items;
const logs = () => env.DATA.json(LOG, '{"items":[]}').items.map((x) => x.text);
const who = (name) => P().find((p) => p.name === name);

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
const form = (name, stage, teams) => [["name", name], ["type", "联赛"], ["stage", stage], ["start", "2026-11-20T17:00"], ["end", "2026-11-20T20:00"],
  ...teams.map((t) => ["teams", t])];
await editor.post("/events/add", form("LCQ胜者组第1轮", "A组", ["星火", "北辰"]));
const ev = () => E().find((e) => e.stage === "A组");

// ---------- 发布：选手放全部 ----------
let r = await admin.post("/publish/test");
let sent = env.PUBLIC.json("esports_test.json");
check("发布：选手放全部（没比赛的队、不直播、直播间待填的都在）", r.location === "/publish?ok=test" && sent.players.length === 7
  && sent.players.some((p) => p.name === "闲人" && p.platform === "huya") && sent.players.find((p) => p.name === "待填").platform === "none");
check("发布：队徽照旧只放用到的队", JSON.stringify(sent.teams) === JSON.stringify({ 星火: { logo: "aaaaaaaaaaaaaaaa.png" } }));
check("发布：没有人观战的赛事不写 observers", sent.events.every((e) => !("observers" in e)));

// ---------- 观战：权限、保存 ----------
r = await viewer.get("/events");
check("只读的看不到观战的按钮", !r.text.includes("/observers"));
r = await editor.get("/events");
check("编辑：每场还没结束超过 7 天的赛事有观战；选手下拉只有开直播的人", r.text.includes(`action="/events/${ev().id}/observers"`)
  && r.text.includes('<datalist id="streamer-names">') && r.text.includes('<option value="皮特">') && !r.text.includes('<option value="不播">')
  && !r.text.includes('<option value="待填">'));
const obs = (rows, rev = ev().rev) => [["rev", String(rev)], ...rows.flatMap(([player, team]) => [["obs_player", player], ["obs_team", team]])];
r = await viewer.post(`/events/${ev().id}/observers`, obs([["皮特", "星火"]]));
check("只读的不能改观战", r.status === 403);
r = await editor.post(`/events/${ev().id}/observers`, obs([["皮特", "星火"], ["Kite", "北辰"], ["", ""]]));
check("保存：皮特看星火（不是这场的人也行）、Kite 看北辰（看别的队）", r.location === "/events?ok=observers"
  && JSON.stringify(ev().observers) === JSON.stringify([{ player: who("皮特").id, team: "星火" }, { player: who("Kite").id, team: "北辰" }]));
check("改动记录写清楚", logs()[0] === "修改赛事「LCQ胜者组第1轮 · A组」的观战：加了 皮特（看 星火）、Kite（看 北辰）");
r = await editor.get(r.location);
check("赛程页上写着谁在看哪队、提醒要发布", r.text.includes("观战：皮特 看 星火、Kite 看 北辰") && r.text.includes("发布以后 Evo 里才看得到")
  && r.text.includes("有改动还没发布到 Evo"));
r = await editor.post(`/events/${ev().id}/observers`, obs([["皮特", "北辰"], ["Kite", "北辰"]]));
check("改看别的队 → 记下来", logs()[0] === "修改赛事「LCQ胜者组第1轮 · A组」的观战：皮特 改看 北辰");
r = await editor.post(`/events/${ev().id}/observers`, obs([["皮特", "北辰"], ["Kite", "北辰"]]));
check("没改 → 说没有改动、不记", r.location === "/events?ok=nochange" && logs()[0].includes("皮特 改看 北辰"));

// ---------- 写错的几种 ----------
const bad = async (rows, text, name) => {
  const res = await editor.post(`/events/${ev().id}/observers`, obs(rows));
  check(name, res.status === 400 && res.text.includes(text));
};
await bad([["不播", "星火"]], "「不播」不开直播", "不直播的人 → 不能观战");
await bad([["待填", "星火"]], "「待填」的直播间还没填", "直播间待填的人 → 不能观战");
await bad([["皮特", "DF"]], "「DF」不是这场的参赛队伍", "看的队不在这场里 → 提示");
await bad([["皮特", "星火"], ["皮特", "北辰"]], "「皮特」写了两次", "同一位写两次 → 提示");
await bad([["皮特", ""]], "少了一样", "只写了选手没写队 → 提示");
await bad([["没有这人", "星火"]], "选手库里没有「没有这人」", "选手库里没有的人 → 提示");
r = await editor.post(`/events/${ev().id}/observers`, obs([["皮特", "星火"]], 1));
check("别人刚改过（rev 对不上）→ 不存", r.status === 400 && r.text.includes("刚改过") && ev().observers[0].team === "北辰");

// ---------- 发布出去的写法 ----------
r = await admin.post("/publish/official");
sent = env.PUBLIC.json("esports.json");
let a = sent.events.find((e) => e.stage === "A组");
check("发布：observers 写选手的 id 和他看的队，放在 teams（主舞台）后面", r.location === "/publish?ok=official"
  && JSON.stringify(a.observers) === JSON.stringify([{ id: "bilibili-23097212", team: "北辰" }, { id: "douyu-520013", team: "北辰" }])
  && Object.keys(a).at(-1) === "observers");
check("Evo 读的时候照样留着（同 esports_data.clean）", JSON.stringify(clean(sent).events.find((e) => e.stage === "A组").observers) === JSON.stringify(a.observers));
r = await editor.get("/players");
check("发布以后不再提醒", !r.text.includes("有改动还没发布"));
const kite = who("Kite");
await editor.post(`/players/${kite.id}/save`, { name: "Kite", team: "星火", platform: "douyu", room: "520099", rev: String(kite.rev) });
r = await admin.post("/publish/test");
a = env.PUBLIC.json("esports_test.json").events.find((e) => e.stage === "A组");
check("Kite 换了直播间：观战里的 id 跟着变（按选手存的）", a.observers[1].id === "douyu-520099");
// 存的数据里观战的人后来不开直播了：这次不放、提醒
{
  const d = env.DATA.json(PLAYERS);
  d.items.find((p) => p.name === "皮特").platform = "none";
  d.items.find((p) => p.name === "皮特").room = "";
  await env.DATA.put(PLAYERS, JSON.stringify(d));
}
r = await admin.get("/publish");
check("观战的人后来不开直播了 → 这次不放、提醒", r.text.includes("的观战：「皮特」不开直播（或直播间还没填），这次不放"));
{
  const d = env.DATA.json(PLAYERS);
  Object.assign(d.items.find((p) => p.name === "皮特"), { platform: "bilibili", room: "23097212" });
  await env.DATA.put(PLAYERS, JSON.stringify(d));
}

// ---------- 跟着改：赛事去掉了队、战队改名、删选手；复制不带观战 ----------
r = await editor.get(`/events?copy=${ev().id}`);
check("复制赛事不带观战（每场看谁不一样）", !/action="\/events\/add"[\s\S]*?obs_player[\s\S]*?<\/form>/.test(r.text.split('id="add"')[1].split("</details>")[0]));
await editor.post("/teams/rename", { from: "北辰", to: "北辰电竞" });
check("战队改名：观战看的队跟着改", ev().observers.every((o) => o.team === "北辰电竞"));
await editor.post(`/events/${ev().id}/save`, [...form("LCQ胜者组第1轮", "A组", ["星火"]), ["rev", String(ev().rev)]]);
check("改赛事去掉了那支队 → 看它的人也去掉、记下来", (ev().observers ?? []).length === 0
  && logs()[0].includes("观战的 2 位（看的队不在这场里了）也去掉了"));
await editor.post(`/events/${ev().id}/observers`, obs([["皮特", "星火"]]));
await editor.post(`/players/${who("皮特").id}/delete`);
check("删选手 → 他在赛事里的观战也去掉", (ev().observers ?? []).length === 0);
