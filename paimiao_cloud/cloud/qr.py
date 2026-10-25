from __future__ import annotations

_DATA_CODEWORDS = {

    1: (19, 16, 13, 9),
    2: (34, 28, 22, 16),
    3: (55, 44, 34, 26),
    4: (80, 64, 48, 36),
    5: (108, 86, 62, 46),
    6: (136, 108, 76, 60),
    7: (156, 124, 88, 66),
    8: (194, 154, 110, 86),
    9: (232, 182, 132, 100),
    10: (274, 216, 154, 122),
}

_ECC_CODEWORDS_PER_BLOCK = {
    1: (7, 10, 13, 17),
    2: (10, 16, 22, 28),
    3: (15, 26, 18, 22),
    4: (20, 18, 26, 16),
    5: (26, 24, 18, 22),
    6: (18, 16, 24, 28),
    7: (20, 18, 18, 26),
    8: (24, 22, 22, 26),
    9: (30, 22, 20, 24),
    10: (18, 26, 24, 28),
}

_NUM_ECC_BLOCKS = {
    1: (1, 1, 1, 1),
    2: (1, 1, 1, 1),
    3: (1, 1, 2, 2),
    4: (1, 2, 2, 4),
    5: (1, 2, 4, 4),
    6: (2, 4, 4, 4),
    7: (2, 4, 6, 5),
    8: (2, 4, 6, 6),
    9: (2, 5, 8, 8),
    10: (4, 5, 8, 8),
}

assert _DATA_CODEWORDS[1][1] == 16, "M 级版本 1 的数据码字必须是 16（19 是 L 级）"
assert _ECC_CODEWORDS_PER_BLOCK[1][1] == 10
assert _NUM_ECC_BLOCKS[1][1] == 1

_ALIGN_POSITIONS = {
    1: (),
    2: (6, 18),
    3: (6, 22),
    4: (6, 26),
    5: (6, 30),
    6: (6, 34),
    7: (6, 22, 38),
    8: (6, 24, 42),
    9: (6, 26, 46),
    10: (6, 28, 50),
}

ECL_FORMAT_BITS = 0b00

ECL_M = 1
MAX_VERSION = 10

class QrError(ValueError):
    pass

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256

def _init_gf():
    value = 1
    for i in range(255):
        _GF_EXP[i] = value
        _GF_LOG[value] = i
        value <<= 1
        if value & 0x100:
            value ^= 0x11D

    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]

_init_gf()

def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]

def _rs_generator(degree: int) -> list[int]:

    poly = [1]
    for i in range(degree):

        nxt = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, _GF_EXP[i])
        poly = nxt
    return poly

def _rs_remainder(data: list[int], degree: int) -> list[int]:

    gen = _rs_generator(degree)
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(degree):
            rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem

def _pick_version(length: int) -> int:

    for version in range(1, MAX_VERSION + 1):
        count_bits = 8 if version <= 9 else 16
        capacity_bits = _DATA_CODEWORDS[version][ECL_M] * 8
        if 4 + count_bits + length * 8 <= capacity_bits:
            return version
    raise QrError(f"内容太长，超出二维码容量（{length} 字节）")

def _encode_data(payload: bytes, version: int) -> list[int]:

    count_bits = 8 if version <= 9 else 16
    bits: list[int] = []

    def push(value: int, width: int):
        for i in range(width - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)
    push(len(payload), count_bits)
    for byte in payload:
        push(byte, 8)

    capacity = _DATA_CODEWORDS[version][ECL_M] * 8

    push(0, min(4, capacity - len(bits)))

    while len(bits) % 8:
        bits.append(0)

    codewords = []
    for i in range(0, len(bits), 8):
        byte = 0
        for bit in bits[i:i + 8]:
            byte = (byte << 1) | bit
        codewords.append(byte)

    pads = (0xEC, 0x11)
    index = 0
    while len(codewords) < _DATA_CODEWORDS[version][ECL_M]:
        codewords.append(pads[index % 2])
        index += 1
    return codewords

def _interleave(codewords: list[int], version: int) -> list[int]:

    num_blocks = _NUM_ECC_BLOCKS[version][ECL_M]
    ecc_len = _ECC_CODEWORDS_PER_BLOCK[version][ECL_M]
    total_data = _DATA_CODEWORDS[version][ECL_M]

    short_len = total_data // num_blocks
    num_long = total_data % num_blocks
    num_short = num_blocks - num_long

    blocks: list[list[int]] = []
    offset = 0
    for i in range(num_blocks):
        size = short_len + (1 if i >= num_short else 0)
        blocks.append(codewords[offset:offset + size])
        offset += size

    ecc_blocks = [_rs_remainder(block, ecc_len) for block in blocks]

    out: list[int] = []
    longest = short_len + (1 if num_long else 0)
    for i in range(longest):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ecc_len):
        for block in ecc_blocks:
            out.append(block[i])
    return out

class _Matrix:
    def __init__(self, version: int):
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]

        self.func = [[False] * self.size for _ in range(self.size)]

    def set_func(self, x: int, y: int, dark: bool):
        if 0 <= x < self.size and 0 <= y < self.size:
            self.modules[y][x] = dark
            self.func[y][x] = True

def _place_finder(m: _Matrix, cx: int, cy: int):

    for dy in range(-4, 5):
        for dx in range(-4, 5):
            x, y = cx + dx, cy + dy
            if not (0 <= x < m.size and 0 <= y < m.size):
                continue
            dist = max(abs(dx), abs(dy))
            m.set_func(x, y, dist != 2 and dist != 4)

def _place_alignment(m: _Matrix):
    centers = _ALIGN_POSITIONS[m.version]
    if not centers:
        return
    last = len(centers) - 1
    for i, cy in enumerate(centers):
        for j, cx in enumerate(centers):

            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    dist = max(abs(dx), abs(dy))
                    m.set_func(cx + dx, cy + dy, dist != 1)

def _place_timing(m: _Matrix):

    for i in range(8, m.size - 8):
        dark = i % 2 == 0
        m.set_func(i, 6, dark)
        m.set_func(6, i, dark)

def _place_version_bits(m: _Matrix):

    if m.version < 7:
        return
    d = m.version << 12
    while d.bit_length() - 1 >= 12:
        d ^= 0x1F25 << (d.bit_length() - 1 - 12)
    bits = (m.version << 12) | d

    def bit(i: int) -> bool:
        return ((bits >> i) & 1) != 0

    for i in range(18):
        dark = bit(i)

        m.set_func(m.size - 11 + i % 3, i // 3, dark)

        m.set_func(i // 3, m.size - 11 + i % 3, dark)

def _place_format_bits(m: _Matrix, mask: int):

    data = (ECL_FORMAT_BITS << 3) | mask

    d = data << 10
    gen_degree = 10
    while d.bit_length() - 1 >= gen_degree:
        d ^= 0x537 << (d.bit_length() - 1 - gen_degree)
    bits = ((data << 10) | d) ^ 0x5412

    def bit(i: int) -> bool:
        return ((bits >> i) & 1) != 0

    for i in range(15):
        if i < 6:
            m.set_func(8, i, bit(i))
        elif i < 8:
            m.set_func(8, i + 1, bit(i))
        else:
            m.set_func(8, m.size - 15 + i, bit(i))

    for i in range(15):
        if i < 8:
            m.set_func(m.size - i - 1, 8, bit(i))
        elif i < 9:
            m.set_func(15 - i - 1 + 1, 8, bit(i))
        else:
            m.set_func(15 - i - 1, 8, bit(i))

    m.set_func(8, m.size - 8, True)

def _place_data(m: _Matrix, codewords: list[int]):

    bits = []
    for byte in codewords:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)

    index = 0
    x = m.size - 1
    upward = True
    while x > 0:
        if x == 6:
            x -= 1
        rows = range(m.size - 1, -1, -1) if upward else range(m.size)
        for y in rows:
            for dx in (0, -1):
                xx = x + dx
                if m.func[y][xx]:
                    continue
                dark = index < len(bits) and bits[index] == 1
                m.modules[y][xx] = dark
                index += 1
        upward = not upward
        x -= 2

    if index < len(bits):

        raise QrError(f"数据位放不下：还剩 {len(bits) - index} 位")

_MASKS = (
    lambda x, y: (x + y) % 2 == 0,

    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (y // 2 + x // 3) % 2 == 0,
    lambda x, y: (x * y) % 2 + (x * y) % 3 == 0,
    lambda x, y: ((x * y) % 2 + (x * y) % 3) % 2 == 0,

    lambda x, y: ((x * y) % 3 + (x + y) % 2) % 2 == 0,
)

def _apply_mask(m: _Matrix, mask: int) -> _Matrix:

    fn = _MASKS[mask]
    out = _Matrix(m.version)
    for y in range(m.size):
        for x in range(m.size):
            out.func[y][x] = m.func[y][x]
            dark = m.modules[y][x]
            if not m.func[y][x] and fn(x, y):
                dark = not dark
            out.modules[y][x] = dark
    _place_format_bits(out, mask)
    return out

def _penalty(m: _Matrix) -> int:

    size = m.size
    mods = m.modules
    score = 0

    for lines in (mods, [[mods[y][x] for y in range(size)] for x in range(size)]):
        for line in lines:
            run = 1
            for i in range(1, size):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2

    for y in range(size - 1):
        for x in range(size - 1):
            v = mods[y][x]
            if v == mods[y][x + 1] == mods[y + 1][x] == mods[y + 1][x + 1]:
                score += 3

    pattern = [True, False, True, True, True, False, True]
    quiet = [False] * 4
    for lines in (mods, [[mods[y][x] for y in range(size)] for x in range(size)]):
        for line in lines:
            for i in range(size - 6):
                if list(line[i:i + 7]) != pattern:
                    continue
                before = list(line[max(0, i - 4):i])
                after = list(line[i + 7:i + 11])
                if (len(before) == 4 and before == quiet) or (len(after) == 4 and after == quiet):
                    score += 40

    dark = sum(1 for row in mods for v in row if v)
    total = size * size
    deviation = abs(dark * 100 // total - 50) // 5
    score += deviation * 10
    return score

def encode(text: str) -> list[list[bool]]:

    payload = text.encode("utf-8")
    version = _pick_version(len(payload))
    codewords = _interleave(_encode_data(payload, version), version)

    m = _Matrix(version)
    _place_finder(m, 3, 3)
    _place_finder(m, m.size - 4, 3)
    _place_finder(m, 3, m.size - 4)
    _place_alignment(m)
    _place_timing(m)
    _place_version_bits(m)

    _place_format_bits(m, 0)
    _place_data(m, codewords)

    best = None
    best_score = None
    for mask in range(8):
        candidate = _apply_mask(m, mask)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    return best.modules

def to_svg(rows: list[list[bool]], scale: int = 8, quiet: int = 4) -> str:

    n = len(rows)
    total = n + quiet * 2
    parts = []
    for y in range(n):
        for x in range(n):
            if rows[y][x]:
                parts.append(f"M{x + quiet} {y + quiet}h1v1h-1z")
    path = "".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total * scale}" '
        f'height="{total * scale}" viewBox="0 0 {total} {total}" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{total}" height="{total}" fill="#fff"/>'
        f'<path d="{path}" fill="#000"/></svg>'
    )

def _selftest() -> None:

    for n in (1, 10, 44, 66, 100):
        text = "A" * n
        rows = encode(text)
        size = len(rows)
        assert size == len(rows[0]), "矩阵不是正方形"
        assert (size - 17) % 4 == 0, f"尺寸 {size} 不是合法二维码尺寸"

        for ox, oy in ((0, 0), (size - 7, 0), (0, size - 7)):
            assert rows[oy][ox] and rows[oy + 6][ox + 6], "定位图案缺失"
            assert rows[oy + 1][ox + 1] is False, "定位图案内白框错误"
            assert rows[oy + 3][ox + 3], "定位图案中心应实心"

        for i in range(8, size - 8):
            assert rows[6][i] == (i % 2 == 0), f"横向时序线第 {i} 格错误"
            assert rows[i][6] == (i % 2 == 0), f"纵向时序线第 {i} 格错误"

        assert encode(text) == rows, "同一输入产出不同矩阵"

    try:
        import qrcode
        from qrcode.util import QRData, MODE_8BIT_BYTE
    except Exception:
        print("qr._selftest: 跳过逐格比对（未安装 python-qrcode），结构校验通过")
        return

    true_masks = [0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0]

    def read_mask(mods: list[list[bool]]) -> int | None:

        bits = 0
        n = len(mods)
        for i in range(15):
            if i < 6:
                v = mods[i][8]
            elif i < 8:
                v = mods[i + 1][8]
            else:
                v = mods[n - 15 + i][8]
            bits |= (1 if v else 0) << i
        return true_masks.index(bits) if bits in true_masks else None

    checked = 0

    for payload_len in range(1, 214):
        payload = b"A" * payload_len
        version = _pick_version(payload_len)
        codewords = _interleave(_encode_data(payload, version), version)

        base = _Matrix(version)
        _place_finder(base, 3, 3)
        _place_finder(base, base.size - 4, 3)
        _place_finder(base, 3, base.size - 4)
        _place_alignment(base)
        _place_timing(base)
        _place_version_bits(base)
        _place_format_bits(base, 0)
        _place_data(base, codewords)

        ref = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                            border=0, version=version)
        ref.add_data(QRData(payload, mode=MODE_8BIT_BYTE))
        ref.make(fit=False)
        ref_mods = [[bool(x) for x in row] for row in ref.get_matrix()]

        mask = read_mask(ref_mods)
        assert mask is not None, f"len={payload_len}: 读不出参考掩码"
        cand = _apply_mask(base, mask).modules
        n = len(ref_mods)
        for y in range(n):
            for x in range(n):
                assert cand[y][x] == ref_mods[y][x], (
                    f"len={payload_len} v={version} mask={mask} 差异在 ({x},{y})"
                )
        checked += 1

    note = ""
    try:
        import cv2
        import numpy as np

        def _decode(rows: list[list[bool]]) -> str:

            m = np.pad(np.array(rows, dtype=bool), 4, constant_values=False)
            big = np.kron(m.astype(np.uint8) * 255, np.ones((10, 10), dtype=np.uint8))
            img = cv2.cvtColor(255 - big, cv2.COLOR_GRAY2BGR)
            return cv2.QRCodeDetector().detectAndDecode(img)[0]

        for text in (
            "A",
            "https://tools.skycraft.cn/apex/pair/c/ABCDEF",
            "https://tools.skycraft.cn/apex/pair/ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "https://tools.skycraft.cn/apex/pair/evo/ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        ):
            assert _decode(encode(text)) == text, f"扫码往返失败: {text}"
        note = "，扫码往返通过"
    except ImportError:
        note = "，跳过扫码往返（未安装 opencv）"

    print(f"qr._selftest: 结构校验通过，与 python-qrcode 逐格一致（{checked} 组）{note}")

if __name__ == "__main__":
    _selftest()
