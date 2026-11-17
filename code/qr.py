"""画二维码：看直播页的 B站扫码登录（panel_esports.py）和绑定微信小程序时设置页显示的那个（cloud_sync.py）都用它。

按 QR 码标准（ISO/IEC 18004）自己写的，不另加依赖：只做"字节模式 + 纠错等级 M"、版本 1–10（最多 213 个字节），
绑定用的网址（约 40 个字符）用版本 3 就够，B站登录的网址（约 145 个字节）用版本 8。生成小程序码要用微信的云调用，可云调用只能从小程序里触发，
电脑调的云函数用不了，所以二维码由 Evo 自己画。

    matrix(text)  → 二维码的方块（list[list[bool]]，True = 黑）
    svg(text)     → SVG 文字（四周留 4 格白边），面板里直接显示
测试：tests/test_bili_login.py、tests/test_cloud_sync.py 用 OpenCV 自带的二维码识别器读回来核对。
2026-11-17 起进 GitHub（用户定的：B站扫码登录要用；原来和小程序的文件一起只留在本机）。
"""

# 每个版本（纠错等级 M）：(纠错码字数 / 块, [(块数, 每块数据码字数), ...])
_BLOCKS = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
# 校正图形的中心坐标（行、列都取这些值的组合）
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}
_LEVEL_M = 0b00  # 格式信息里纠错等级的两位：L=01、M=00、Q=11、H=10

# ---------- GF(256) 上的 Reed-Solomon 纠错码（本原多项式 x^8+x^4+x^3+x^2+1） ----------

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(degree):
    """生成多项式 (x - α^0)(x - α^1)…(x - α^(degree-1)) 的系数，最高次项在前。"""
    poly = [1]
    for i in range(degree):
        out = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            out[j] ^= c
            out[j + 1] ^= _mul(c, _EXP[i])
        poly = out
    return poly


def _ec_codewords(data, degree):
    """一块数据的纠错码字：数据多项式乘 x^degree 后除以生成多项式的余数。"""
    gen = _generator(degree)
    rest = list(data) + [0] * degree
    for i in range(len(data)):
        factor = rest[i]
        if factor:
            for j, g in enumerate(gen):
                rest[i + j] ^= _mul(g, factor)
    return rest[len(data):]


# ---------- 编码：数据 → 码字 ----------

def _capacity(version):
    """这个版本（纠错等级 M）能放几个字节。"""
    data_words = sum(n * k for n, k in _BLOCKS[version][1])
    count_bits = 8 if version <= 9 else 16
    return (data_words * 8 - 4 - count_bits) // 8


def _codewords(payload, version):
    ec_per_block, groups = _BLOCKS[version]
    data_words = sum(n * k for n, k in groups)
    bits = [0, 1, 0, 0]  # 字节模式
    count_bits = 8 if version <= 9 else 16
    bits += [(len(payload) >> i) & 1 for i in reversed(range(count_bits))]
    for byte in payload:
        bits += [(byte >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, data_words * 8 - len(bits))  # 结束符
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    words += [pad[i % 2] for i in range(data_words - len(words))]

    blocks, at = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[at:at + size])
            at += size
    ecs = [_ec_codewords(b, ec_per_block) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):  # 交错：各块的第 1 个、第 2 个……
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_per_block):
        out += [e[i] for e in ecs]
    return out


# ---------- 画方块 ----------

def _bch(value, poly, length):
    """BCH 码的余数（格式信息、版本信息用）。"""
    rest = value << length
    top = poly.bit_length() - 1
    while rest.bit_length() - 1 >= top:
        rest ^= poly << (rest.bit_length() - 1 - top)
    return (value << length) | rest


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


class _Grid:
    def __init__(self, version):
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]  # 功能图形占的格子：放数据、加掩码时跳过

    def put(self, r, c, dark):
        if 0 <= r < self.size and 0 <= c < self.size:
            self.dark[r][c] = dark
            self.fixed[r][c] = True

    def function_patterns(self):
        n = self.size
        for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):  # 三个角上的定位图形，外面一圈白边
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    ring = max(abs(dr - 3), abs(dc - 3))
                    self.put(r0 + dr, c0 + dc, ring in (0, 1, 3))
        for i in range(8, n - 8):  # 定时图形
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        centers = _ALIGN[self.version]
        for r in centers:
            for c in centers:
                if (r, c) in ((6, 6), (6, n - 7), (n - 7, 6)):
                    continue  # 和定位图形重叠的不画
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        self.put(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
        self.format_bits(0)  # 先占位，选好掩码后再写真的
        self.put(n - 8, 8, True)  # 固定的黑点
        if self.version >= 7:
            info = _bch(self.version, 0x1F25, 12)
            for i in range(18):
                a, b = n - 11 + i % 3, i // 3
                self.put(a, b, (info >> i) & 1 == 1)
                self.put(b, a, (info >> i) & 1 == 1)

    def format_bits(self, mask):
        n = self.size
        bits = _bch((_LEVEL_M << 3) | mask, 0x537, 10) ^ 0x5412
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        for i in range(6):  # 左上角那一份
            self.put(i, 8, bit[i])
        self.put(7, 8, bit[6])
        self.put(8, 8, bit[7])
        self.put(8, 7, bit[8])
        for i in range(9, 15):
            self.put(8, 14 - i, bit[i])
        for i in range(8):  # 右上、左下那一份
            self.put(8, n - 1 - i, bit[i])
        for i in range(8, 15):
            self.put(n - 15 + i, 8, bit[i])
        self.put(n - 8, 8, True)

    def place(self, words):
        n = self.size
        bits = [(w >> (7 - i)) & 1 == 1 for w in words for i in range(8)]
        at, upward, col = 0, True, n - 1
        while col > 0:
            if col == 6:  # 跳过竖的定时图形那一列
                col -= 1
            rows = range(n - 1, -1, -1) if upward else range(n)
            for r in rows:
                for c in (col, col - 1):
                    if not self.fixed[r][c]:
                        self.dark[r][c] = at < len(bits) and bits[at]
                        at += 1
            upward = not upward
            col -= 2

    def masked(self, mask):
        test = _MASKS[mask]
        return [[self.dark[r][c] ^ (not self.fixed[r][c] and test(r, c)) for c in range(self.size)]
                for r in range(self.size)]


def _penalty(m):
    """掩码的扣分（标准里的四条规则），选扣分最少的掩码。"""
    n = len(m)
    score = 0
    lines = m + [list(col) for col in zip(*m)]
    for line in lines:  # 1：一行（列）里连续 5 个以上同色
        run = 1
        for i in range(1, n):
            if line[i] == line[i - 1]:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        score += run - 2 if run >= 5 else 0
    for r in range(n - 1):  # 2：2×2 同色
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    finder = [True, False, True, True, True, False, True]
    for line in lines:  # 3：像定位图形的 1:1:3:1:1，一边有 4 格白
        for i in range(n - 6):
            if line[i:i + 7] == finder:
                before = line[max(0, i - 4):i]
                after = line[i + 7:i + 11]
                if (len(before) == 4 and not any(before)) or (len(after) == 4 and not any(after)):
                    score += 40
    dark = sum(map(sum, m))  # 4：黑白比例离 50% 越远扣得越多
    score += abs(dark * 100 // (n * n) - 50) // 5 * 10
    return score


def matrix(text):
    """二维码的方块：list[list[bool]]，True = 黑。太长（版本 10 也放不下）时 ValueError。"""
    payload = text.encode("utf-8")
    version = next((v for v in _BLOCKS if _capacity(v) >= len(payload)), None)
    if version is None:
        raise ValueError(f"太长了，二维码放不下（{len(payload)} 字节，最多 {_capacity(10)}）")
    grid = _Grid(version)
    grid.function_patterns()
    grid.place(_codewords(payload, version))
    best = None
    for mask in range(8):
        grid.format_bits(mask)
        candidate = grid.masked(mask)
        score = _penalty(candidate)
        if best is None or score < best[0]:
            best = (score, mask)
    grid.format_bits(best[1])
    return grid.masked(best[1])


def svg(text, border=4):
    """SVG 文字：黑方块拼成一条路径，四周留 border 格白边（扫码要），按格子大小缩放。"""
    m = matrix(text)
    n = len(m) + 2 * border
    path = "".join(f"M{c + border},{r + border}h1v1h-1z" for r, row in enumerate(m) for c, dark in enumerate(row) if dark)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges" '
            f'role="img" aria-label="二维码"><rect width="{n}" height="{n}" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')
