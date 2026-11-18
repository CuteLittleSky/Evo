// 协作网页测试用的小工具：记结果、内存里的假存储桶、一个记着 Cookie 的"浏览器"。test.mjs 说明怎么跑。

import { handle } from "./worker.js";

export const ORIGIN = "https://team.evotracker.cn";
export const DAY = 24 * 3600 * 1000;

const tally = { checks: 0, failures: 0 };

export function check(name, condition) {
  tally.checks++;
  if (!condition) {
    tally.failures++;
    console.log(`  ✗ ${name}`);
  }
}

export function summary() {
  console.log(tally.failures ? `\n${tally.failures} / ${tally.checks} 项没通过` : `\n全部通过（${tally.checks} 项）`);
  return tally.failures ? 1 : 0;
}

// 支持 etag 条件写入（同 R2）；存文字也存图片（bytes），body 是当文字读出来的样子
export class FakeBucket {
  constructor() { this.objects = new Map(); this.version = 0; this.puts = 0; }
  async get(key) {
    const o = this.objects.get(key);
    return o ? { etag: o.etag, size: o.bytes.byteLength, text: async () => o.body, arrayBuffer: async () => o.bytes.slice().buffer,
                 body: o.bytes.slice() } : null;
  }
  async head(key) {
    const o = this.objects.get(key);
    return o ? { etag: o.etag, size: o.bytes.byteLength } : null;
  }
  async put(key, body, options = {}) {
    const current = this.objects.get(key);
    const want = options.onlyIf?.etagMatches;
    if (want && (!current || current.etag !== want)) return null;
    const etag = `e${++this.version}`;
    const bytes = typeof body === "string" ? new TextEncoder().encode(body)
      : new Uint8Array(body instanceof ArrayBuffer ? body : body.buffer.slice(body.byteOffset, body.byteOffset + body.byteLength));
    this.objects.set(key, { body: typeof body === "string" ? body : new TextDecoder().decode(bytes), bytes, etag, options });
    this.puts++;
    return { etag };
  }
  json(key, fallback = "{}") { return JSON.parse(this.objects.get(key)?.body ?? fallback); }
}

// 一个"浏览器"：记着自己的 Cookie。clock() 给现在的时间（测试里会往后拨）。
// form 可以是 {名: 值}，也可以是 [[名, 值], ...]（同一个名字有好几个值时，比如勾选的队伍）
export function makeBrowser(env, clock, userAgent = "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0") {
  const jar = { cookie: "" };
  async function go(method, path, { form = null, origin = ORIGIN } = {}) {
    const headers = { "User-Agent": userAgent };
    if (jar.cookie) headers.Cookie = jar.cookie;
    if (origin !== null && method === "POST") headers.Origin = origin;
    let body;
    if (form instanceof FormData) {
      body = form;  // 带文件的表单（multipart），Content-Type 由 Request 自己写
    } else if (form) {
      body = new URLSearchParams(form).toString();
      headers["Content-Type"] = "application/x-www-form-urlencoded";
    }
    const response = await handle(new Request(ORIGIN + path, { method, headers, body }), env, clock());
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

export function linkFrom(html) {
  return html.match(/\/join\/([A-Za-z0-9_-]{32})/)?.[1] ?? null;
}
