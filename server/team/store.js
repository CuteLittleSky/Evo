// 存储桶里的 JSON 文件：读、改（etag 条件写入）。成员（auth.js）、选手库和赛程（data.js）都用它。
// 改：读 → change(data) 改 → 写回去；中间有别人写过（etag 变了）就从头再来，不会冲掉别人的改动。

export class Refuse extends Error {}  // 给页面看的错误（"名字不能空着"这类），worker.js 把 message 原样显示

export async function readDoc(bucket, key, empty) {
  const object = await bucket.get(key);
  if (!object) return { data: empty(), etag: null };
  return { data: { ...empty(), ...JSON.parse(await object.text()) }, etag: object.etag };
}

export async function updateDoc(bucket, key, empty, change) {
  for (let attempt = 0; attempt < 5; attempt++) {
    const { data, etag } = await readDoc(bucket, key, empty);
    const result = await change(data);
    const options = { httpMetadata: { contentType: "application/json; charset=utf-8" } };
    if (etag) options.onlyIf = { etagMatches: etag };
    const written = await bucket.put(key, JSON.stringify(data), options);
    if (written) return result;
  }
  throw new Refuse("刚才有几个人同时在改，没保存上，请再试一次");
}

export function randomHex(bytes) {
  return [...crypto.getRandomValues(new Uint8Array(bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");
}
