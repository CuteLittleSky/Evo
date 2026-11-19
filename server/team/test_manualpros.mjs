// 手动建的选手资料的测试（2026-11-19：Liquipedia 上没有页面的选手，管理员和编辑能手动建）。由 test.mjs 运行。
// 走一遍：待确认里"Liquipedia 上没有这个页面"那一条有"手动建资料"、建好以后那一条没了；资料页上能搜到、标"手动建的"；
// 从资料页新建（页面名用 ID）、连上选手库的人；权限；写错的提示；改（rev）、删；战队页；Liquipedia 后来有了页面就用 Liquipedia 的。

import { hash, newToken, update } from "./auth.js";
import { LOG, PLAYERS } from "./data.js";
import { INDEX } from "./lpsync.js";
import { PROS } from "./manualpros.js";
import { FakeBucket, check, makeBrowser } from "./testkit.mjs";

const MINUTE = 60 * 1000;
const now = Date.UTC(2026, 10, 20, 4, 0);
const env = { DATA: new FakeBucket(), PUBLIC: new FakeBucket() };
const browser = () => makeBrowser(env, () => now);
const pros = () => env.DATA.json(PROS, '{"items":{}}').items;
const players = () => env.DATA.json(PLAYERS, '{"items":[]}').items;
const logs = () => env.DATA.json(LOG, '{"items":[]}').items.map((x) => x.text);

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

const admin = await join("黄指导", "admin");
const editor = await join("毛毛", "editor");
const viewer = await join("看看", "viewer");

// 抓到的：Dokiovo、NotWinter 在 Liquipedia 上没有页面；BBB_Esports 是一支有资料的队
await env.DATA.put(INDEX, JSON.stringify({ events: {}, teams: { Bbz: { page: "Bbz", name: "Bbz" } },
  players: { Dokiovo: { page: "Dokiovo", missing: true }, NotWinter: { page: "NotWinter", missing: true },
             Real: { page: "Real", id: "Real", team: "Bbz", teamPage: "Bbz", nats: ["中国"] } } }));
await editor.post("/players/add", { name: "Dokiovo", team: "Bbz", platform: "none", room: "", liquipedia: "Dokiovo" });
await editor.post("/players/add", { name: "NotWinter", team: "Bbz", platform: "none", room: "", liquipedia: "NotWinter" });
await editor.post("/players/add", { name: "suibingbing", team: "Bbz", platform: "none", room: "" });

// ---------- 待确认里的"Liquipedia 上没有这个页面" ----------
let r = await editor.get("/liquipedia");
check("待确认：没有页面的那两条有手动建资料（带上页面名、选手、队）", r.text.includes("待确认（2）")
  && r.text.includes('href="/pros/new?page=Dokiovo&amp;link=Dokiovo&amp;team=Bbz"'));
r = await viewer.get("/liquipedia");
check("只读的没有手动建资料", !r.text.includes("/pros/new"));
r = await viewer.get("/pros/new");
check("只读的打不开新建", r.status === 403);
r = await editor.get("/pros/new?page=Dokiovo&link=Dokiovo&team=Bbz");
check("新建页：从待确认来的，带着页面名、ID、队、要连上的人", r.status === 200 && r.text.includes('name="page" value="Dokiovo"')
  && r.text.includes('name="id" value="Dokiovo"') && r.text.includes('name="team" value="Bbz"') && r.text.includes('name="link" value="Dokiovo"'));
r = await editor.post("/pros/new", { page: "Dokiovo", id: "Dokiovo", name: "", team: "Bbz", nats: "中国台湾", birth: "2003/5/1", input: "", status: "active", link: "Dokiovo" });
check("生日写错 → 提示，填的留着", r.status === 400 && r.text.includes("生日写成 1999-05-20") && r.text.includes('value="中国台湾"') && !pros().Dokiovo);
r = await editor.post("/pros/new", { page: "Dokiovo", id: "Dokiovo", name: "", team: "Bbz", nats: "中国台湾", birth: "2003-05-01", input: "键鼠", status: "active", link: "Dokiovo" });
check("建好 → 去他的选手页", r.location === "/pros/Dokiovo?ok=created" && pros().Dokiovo?.createdBy === "毛毛" && pros().Dokiovo.nats[0] === "中国台湾");
check("改动记录", logs()[0] === "手动建了选手资料 Dokiovo（Liquipedia 上没有页面），连上选手库的「Dokiovo」");
r = await editor.get("/liquipedia");
check("待确认里那一条没了", r.text.includes("待确认（1）") && !r.text.includes("「Dokiovo」填的 Liquipedia 页面名"));
r = await viewer.get("/pros/Dokiovo");
check("选手页：手动建的、谁建的、选手库连着谁；只读的没有改、删", r.status === 200 && r.text.includes("手动建的") && r.text.includes("毛毛 刚刚手动建的")
  && r.text.includes("选手库里是 「Dokiovo」（Bbz）") && !r.text.includes("/pros/Dokiovo/edit") && !r.text.includes("/pros/Dokiovo/delete"));
r = await viewer.get("/pros?q=doki");
check("资料页上能搜到、标手动建的", r.text.includes('href="/pros/Dokiovo"') && r.text.includes("手动建的"));
r = await viewer.get("/teams/Bbz");
check("战队页：现在在这支队的手动建的人也算", r.text.includes('href="/pros/Dokiovo"') && r.text.includes('href="/pros/Real"'));
r = await editor.post("/pros/new", { page: "Dokiovo", id: "Dokiovo" });
check("同一个页面名再建 → 提示", r.status === 400 && r.text.includes("已经手动建过 Dokiovo"));
r = await editor.post("/pros/new", { page: "Real", id: "Real" });
check("Liquipedia 上有的 → 不用手动建", r.status === 400 && r.text.includes("Liquipedia 上已经有 Real 的资料了"));

// ---------- 从资料页新建：页面名用 ID，连上选手库的人 ----------
r = await editor.get("/pros");
check("资料页上有手动建一份资料", r.text.includes('href="/pros/new"'));
r = await editor.post("/pros/new", { id: "sui bing bing", name: "", team: "Bbz", nats: "", birth: "", input: "", status: "inactive", link: "suibingbing" });
check("页面名用 ID（空格换成下划线）、连上的人填好页面名", r.location === "/pros/sui_bing_bing?ok=created"
  && players().find((p) => p.name === "suibingbing").liquipedia === "sui_bing_bing" && pros().sui_bing_bing.status === "inactive");
r = await admin.post("/pros/new", { id: "" });
check("ID 空着 → 提示", r.status === 400 && r.text.includes("「ID」不能空着"));
r = await admin.post("/pros/new", { id: "a#b" });
check("ID 里有 # → 提示", r.status === 400 && r.text.includes("不能用"));

// ---------- 改、删 ----------
r = await viewer.post("/pros/Dokiovo/edit", { id: "Dokiovo", rev: "1" });
check("只读的不能改", r.status === 403);
r = await editor.post("/pros/Dokiovo/edit", { id: "Dokiovo", name: "王小明", team: "Bbz", nats: "中国台湾", birth: "2003-05-01", input: "键鼠", status: "active", rev: "1" });
check("改 → 记下改了什么", r.location === "/pros/Dokiovo?ok=saved" && pros().Dokiovo.name === "王小明" && pros().Dokiovo.rev === 2
  && logs()[0] === "修改手动建的选手资料 Dokiovo：真名 （空） → 王小明");
r = await admin.post("/pros/Dokiovo/edit", { id: "Dokiovo", name: "别的", rev: "1" });
check("别人刚改过（rev 对不上）→ 不存", r.status === 400 && r.text.includes("毛毛 刚改过") && pros().Dokiovo.name === "王小明");
r = await editor.post("/pros/sui_bing_bing/delete");
check("删 → 回资料页；选手库里填的页面名不动", r.location === "/pros?ok=deleted" && !pros().sui_bing_bing
  && players().find((p) => p.name === "suibingbing").liquipedia === "sui_bing_bing");

// ---------- Liquipedia 后来有了这个页面 ----------
const idx = env.DATA.json(INDEX);
idx.players.Dokiovo = { page: "Dokiovo", id: "Dokiovo", team: "Bbz", teamPage: "Bbz", nats: ["中国台湾"], fetchedAt: now };
await env.DATA.put(INDEX, JSON.stringify(idx));
r = await editor.get("/pros/Dokiovo");
check("Liquipedia 有了页面 → 用 Liquipedia 的，提一句以前手动建过、能删", r.text.includes("在 Liquipedia 上看") && r.text.includes("以前手动建过他的资料")
  && r.text.includes("删掉手动建的那份"));
r = await editor.get("/pros?q=doki");
check("资料页上不重复列", (r.text.match(/href="\/pros\/Dokiovo"/g) ?? []).length === 1 && !r.text.includes("手动建的</span>"));
r = await editor.post("/pros/Dokiovo/delete");
check("删掉手动建的那份 → 回 Liquipedia 的选手页", r.location === "/pros/Dokiovo?ok=deleted" && !pros().Dokiovo);
