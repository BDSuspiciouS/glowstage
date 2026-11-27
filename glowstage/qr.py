"""A QR code for a short text (the phone remote's link), drawn as SVG for the window.

Byte mode, error correction level M, versions 1 to 10 (up to 213 bytes): the QR standard
(ISO/IEC 18004), written out here so the app needs nothing more for one small code. The pieces
are checked in tests/test_qr.py against the standard's own example and tables, and by reading
every code back.
"""

from __future__ import annotations

# Level M, per version: (error correction codewords per block, data codewords of each block)
BLOCKS = {
    1: (10, [16]), 2: (16, [28]), 3: (26, [44]), 4: (18, [32, 32]), 5: (24, [43, 43]),
    6: (16, [27] * 4), 7: (18, [31] * 4), 8: (22, [38, 38, 39, 39]), 9: (22, [36] * 3 + [37] * 2),
    10: (26, [43] * 4 + [44]),
}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38],
         8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


# ---- Reed-Solomon over GF(256) --------------------------------------------------------------

def _mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _mul(root, 0x02)
    return result


def ecc(data: list[int], degree: int) -> list[int]:
    """The error correction codewords for a block of data codewords."""
    div = _divisor(degree)
    result = [0] * degree
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(div):
            result[i] ^= _mul(coef, factor)
    return result


def format_bits(mask: int) -> int:
    """The 15 format bits for level M (00) and a mask."""
    data = (0b00 << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | (rem & 0x3FF)) ^ 0x5412


def version_bits(version: int) -> int:
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | (rem & 0xFFF)


# ---- the code ---------------------------------------------------------------------------------

def codewords(text: str) -> tuple[int, list[int]]:
    """The version that fits, and its codewords: data and error correction, interleaved."""
    raw = text.encode("utf-8")
    for version, (ec, sizes) in BLOCKS.items():
        count_bits = 8 if version < 10 else 16
        capacity = sum(sizes)
        if 4 + count_bits + 8 * len(raw) <= capacity * 8:
            break
    else:
        raise ValueError("Too long for a QR code here")
    bits = [0, 1, 0, 0] + [(len(raw) >> i) & 1 for i in reversed(range(count_bits))]
    for b in raw:
        bits += [(b >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < capacity:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    blocks, k = [], 0
    for size in sizes:
        blocks.append(data[k:k + size])
        k += size
    eccs = [ecc(b, ec) for b in blocks]
    out = [b[i] for i in range(max(sizes)) for b in blocks if i < len(b)]
    out += [e[i] for i in range(ec) for e in eccs]
    return version, out


class QR:
    def __init__(self, text: str):
        self.version, words = codewords(text)
        self.size = n = self.version * 4 + 17
        self.dark = [[False] * n for _ in range(n)]
        self.fixed = [[False] * n for _ in range(n)]
        self._patterns()
        self._place(words)
        self.mask = min(range(8), key=self._try)
        self._apply(self.mask)
        self._format(self.mask)

    def _set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def _patterns(self) -> None:
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = ALIGN[self.version]
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._format(0)     # (reserves the format areas; drawn for real once the mask is chosen)
        if self.version >= 7:
            bits = version_bits(self.version)
            for i in range(18):
                bit, a, b = bool((bits >> i) & 1), n - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _format(self, mask: int) -> None:
        n, bits = self.size, format_bits(mask)
        bit = [bool((bits >> i) & 1) for i in range(15)]
        for i in range(6):
            self._set(8, i, bit[i])
        self._set(8, 7, bit[6])
        self._set(8, 8, bit[7])
        self._set(7, 8, bit[8])
        for i in range(9, 15):
            self._set(14 - i, 8, bit[i])
        for i in range(8):
            self._set(n - 1 - i, 8, bit[i])
        for i in range(8, 15):
            self._set(8, n - 15 + i, bit[i])
        self._set(8, n - 8, True)       # the dark module

    def order(self):
        """The modules that hold codewords, in the order they're filled."""
        n, right = self.size, self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = ((right + 1) & 2) == 0
            for vert in range(n):
                y = n - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not self.fixed[y][x]:
                        yield x, y
            right -= 2

    def _place(self, words: list[int]) -> None:
        bits = [(w >> (7 - i)) & 1 for w in words for i in range(8)]
        for k, (x, y) in enumerate(self.order()):
            self.dark[y][x] = bool(bits[k]) if k < len(bits) else False   # (remainder bits: light)

    def _apply(self, mask: int) -> None:
        f = MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and f(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def _try(self, mask: int) -> int:
        self._apply(mask)
        self._format(mask)
        score = penalty(self.dark)
        self._apply(mask)                    # (XOR again: undone)
        return score

    def svg(self, quiet: int = 4) -> str:
        """The code as an SVG image: dark modules on white, with the quiet zone around it."""
        n = self.size + 2 * quiet
        parts = []
        for y, row in enumerate(self.dark):     # one rectangle per run of dark modules in a row
            x = 0
            while x < self.size:
                if row[x]:
                    run = 1
                    while x + run < self.size and row[x + run]:
                        run += 1
                    parts.append(f"M{x + quiet},{y + quiet}h{run}v1h-{run}z")
                    x += run
                else:
                    x += 1
        path = "".join(parts)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
                f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')


def penalty(m: list[list[bool]]) -> int:
    """The standard's penalty for patterns that make a code hard to read: the mask that scores
    lowest is used."""
    n, score = len(m), 0
    lines = m + [list(col) for col in zip(*m)]
    for line in lines:
        run, prev = 0, None
        for v in line:                              # runs of five or more of a kind
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run, prev = 1, v
        if run >= 5:
            score += run - 2
        s = "".join("1" if v else "0" for v in line)
        for pat in ("10111010000", "00001011101"):  # look-alikes of the finder patterns
            score += 40 * sum(1 for i in range(n - 10) if s[i:i + 11] == pat)
    for y in range(n - 1):                          # 2x2 blocks of a kind
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))                         # balance of dark and light
    score += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return score
