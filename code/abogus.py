"""抖音网页接口的反爬签名 a_bogus（查抖音直播间在不在播、取播放地址时要带）。纯计算，不联网、不跑 JS。

照 DTV（https://github.com/chen-zeong/DTV ，internal/platforms/douyin/abogus.go）的流程用 Python 自带的功能重写。
四步，顺序不能改：
1. SM3 哈希（国密的，不是 SHA-256）：把请求参数、浏览器标识压成摘要
2. RC4 加密
3. 抖音自己的 base64（字符表和标准的不一样）
4. 拼一个 44 项的字节表（时间、固定的页面编号和应用编号、上面几个摘要里的几个字节、校验和），再 RC4、再编码

签名对不对只有一个办法知道：拿真的接口试，错一个字节抖音就返回空数据。抖音会不定期改算法：坏了先用
esports.json 里的平台开关把抖音切成"去抖音看"，再照 DTV 的新版本修（见 docs/底座.md）。

用法：python code/abogus.py "参数串" "浏览器标识"   → 打印签名
"""

import sys
import time
from urllib.parse import quote_plus

SM3_IV = (0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600, 0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E)
MASK = 0xFFFFFFFF
# 抖音自己的字符表（s3 给浏览器标识用，s4 给最后的签名用）
TABLE_S3 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"
TABLE_S4 = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"
WINDOW_ENV = "1920|1080|1920|1040|0|30|0|0|1872|92|1920|1040|1857|92|1|24|Win32"  # 固定的"窗口环境"
SUFFIX = "cus"
PAGE_ID = 110624
AID = 6383


def _rotl(x, n):
    n %= 32
    return ((x << n) | (x >> (32 - n))) & MASK if n else x


def _compress(reg, block):
    w = [int.from_bytes(block[i:i + 4], "big") for i in range(0, 64, 4)]
    for j in range(16, 68):
        a = w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15)
        a = a ^ _rotl(a, 15) ^ _rotl(a, 23)
        w.append(a ^ _rotl(w[j - 13], 7) ^ w[j - 6])
    a, b, c, d, e, f, g, h = reg
    for j in range(64):
        tj = 0x79CC4519 if j < 16 else 0x7A879D8A
        ss1 = _rotl((_rotl(a, 12) + e + _rotl(tj, j)) & MASK, 7)
        ss2 = ss1 ^ _rotl(a, 12)
        ff = (a ^ b ^ c) if j < 16 else ((a & b) | (a & c) | (b & c))
        gg = (e ^ f ^ g) if j < 16 else ((e & f) | (~e & MASK & g))
        tt1 = (ff + d + ss2 + (w[j] ^ w[j + 4])) & MASK
        tt2 = (gg + h + ss1 + w[j]) & MASK
        d, c, b, a = c, _rotl(b, 9), a, tt1
        h, g, f, e = g, _rotl(f, 19), e, tt2 ^ _rotl(tt2, 9) ^ _rotl(tt2, 17)
    return [x ^ y for x, y in zip(reg, (a, b, c, d, e, f, g, h))]


def sm3(data):
    """SM3 摘要（32 字节）。"""
    data = bytes(data)
    padded = data + b"\x80"
    padded += b"\x00" * ((56 - len(padded) % 64) % 64) + (len(data) * 8).to_bytes(8, "big")
    reg = list(SM3_IV)
    for i in range(0, len(padded), 64):
        reg = _compress(reg, padded[i:i + 64])
    return b"".join(x.to_bytes(4, "big") for x in reg)


def rc4(data, key):
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for ch in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(ch ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def encode(data, table):
    """抖音的 base64：每 3 个字节变 4 个字符，最后不够 3 个的按 0 补，不加等号。"""
    out = []
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        n = int.from_bytes(chunk + b"\x00" * (3 - len(chunk)), "big")
        out.extend(table[(n >> shift) & 63] for shift in (18, 12, 6, 0))
    return "".join(out)


def _prefix():
    """签名开头的 12 个字节。名义上是随机数，DTV 和它参考的实现都用三个写死的数，所以每次一样。"""
    out = bytearray()
    for number, (o1, o2) in ((1234, (3, 45)), (9876, (1, 0)), (5555, (1, 5))):
        b1, b2 = number & 255, (number >> 8) & 255
        out += bytes(((b1 & 170) | (o1 & 85), (b1 & 85) | (o1 & 170), (b2 & 170) | (o2 & 85), (b2 & 85) | (o2 & 170)))
    return bytes(out)


def _body(query, user_agent, now_ms):
    start, end = now_ms, now_ms + 100
    url_hash = sm3(sm3((query + SUFFIX).encode("utf-8")))
    cus_hash = sm3(sm3(SUFFIX.encode("utf-8")))
    ua_hash = sm3(encode(rc4(user_agent.encode("utf-8"), bytes((0, 1, 14))), TABLE_S3).encode("ascii"))
    env = WINDOW_ENV.encode("ascii")
    b = {18: 44, 48: 3, 69: 0, 70: 0, 71: 0}
    b[20], b[21], b[22], b[23] = start.to_bytes(8, "big")[4:]
    b[24], b[25] = (start >> 32) & 255, (start >> 40) & 255
    b[26], b[27], b[28], b[29] = 0, 0, 0, 0   # 三个固定参数 0、1、14 拆成的字节
    b[30], b[31], b[32], b[33] = 0, 1, 0, 0
    b[34], b[35], b[36], b[37] = 0, 0, 0, 14
    b[38], b[39] = url_hash[21], url_hash[22]
    b[40], b[41] = cus_hash[21], cus_hash[22]
    b[42], b[43] = ua_hash[23], ua_hash[24]
    b[44], b[45], b[46], b[47] = end.to_bytes(8, "big")[4:]
    b[49], b[50] = (end >> 32) & 255, (end >> 40) & 255
    b[52], b[53], b[54], b[55] = PAGE_ID.to_bytes(4, "big")
    b[57], b[58], b[59], b[60] = AID.to_bytes(4, "little")
    b[65], b[66] = len(env) & 255, (len(env) >> 8) & 255
    order = (18, 20, 52, 26, 30, 34, 58, 38, 40, 53, 42, 21, 27, 54, 55, 31, 35, 57, 39, 41, 43, 22, 28, 32,
             60, 36, 23, 29, 33, 37, 44, 45, 59, 46, 47, 48, 49, 50, 24, 25, 65, 66, 70, 71)
    checksum = 0
    for index in order:  # 校验和：这 44 个字节全部异或（DTV 列的少一个第 34 项，它恒为 0，结果一样）
        checksum ^= b[index]
    return rc4(bytes(b[index] for index in order) + env + bytes((checksum,)), b"y")


def sign(query, user_agent, now_ms=None):
    """算 a_bogus。query 是地址里除 a_bogus 以外的全部参数（已经编码好、顺序和发出去的一样），
    user_agent 要和请求头里的一模一样。now_ms 只在测试时给（固定时间，结果才固定）。"""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return encode(_prefix() + _body(query, user_agent, now_ms), TABLE_S4) + "="


def signed_url(endpoint, params, user_agent, now_ms=None):
    """拼出带签名的完整地址。params 是 [(名字, 值), ...]，顺序就是发出去的顺序。"""
    query = "&".join(f"{quote_plus(str(k))}={quote_plus(str(v))}" for k, v in params)
    return f"{endpoint}?{query}&a_bogus={quote_plus(sign(query, user_agent, now_ms))}"


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    print(sign(sys.argv[1], sys.argv[2]))
