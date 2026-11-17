// 协作网页的测试：node server/team/test.mjs（只要 Node.js，不用 wrangler、不用联网）。
// 用内存里的假存储桶把 worker.js 的每条路走一遍：
//   test_members.mjs   成员、邀请链接、登录（第 1 步）
//   test_esports.mjs   选手库、赛程、从表格导入、发布、平台开关、改动记录（第 2 步）
//
// 另一个用法（tests/test_team_publish.py 用）：node server/team/test.mjs --build 输入.json
//   输入 {"players": [...], "events": [...], "switches": {...}, "now": 毫秒}，打印 esports.js 生成的 esports.json 和检查结果，
//   Python 那边拿同一份数据跑 tools/esports.py 的 build，两边逐项比；--platforms 打印平台名单（和 config/watch.json 比）

import { readFileSync } from "node:fs";
import { PLATFORMS, build } from "./esports.js";
import { summary } from "./testkit.mjs";

const args = process.argv.slice(2);
if (args[0] === "--build") {
  const input = JSON.parse(readFileSync(args[1], "utf-8"));
  console.log(JSON.stringify(build(input.players, input.events, input.switches, input.now)));
} else if (args[0] === "--platforms") {
  console.log(JSON.stringify(Object.entries(PLATFORMS).map(([key, p]) => [key, p.name, p.room])));
} else {
  console.log("成员、邀请、登录");
  await import("./test_members.mjs");
  console.log("\n选手库、赛程、发布");
  await import("./test_esports.mjs");
  process.exit(summary());
}
