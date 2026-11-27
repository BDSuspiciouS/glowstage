import unittest

from glowstage import qr


def read_back(code: qr.QR) -> str:
    """Read a code the way a phone does, from the modules alone: format, mask, codewords, error
    correction, data."""
    n, m = code.size, code.dark
    first = [m[0][8], m[1][8], m[2][8], m[3][8], m[4][8], m[5][8], m[7][8], m[8][8], m[8][7],
             m[8][5], m[8][4], m[8][3], m[8][2], m[8][1], m[8][0]]
    second = [m[8][n - 1 - i] for i in range(8)] + [m[n - 15 + i][8] for i in range(8, 15)]
    fmt = sum(int(b) << i for i, b in enumerate(first))
    assert fmt == sum(int(b) << i for i, b in enumerate(second)), "the two copies of the format differ"
    mask = next(k for k in range(8) if qr.format_bits(k) == fmt)
    bits = [int(m[y][x] ^ qr.MASKS[mask](x, y)) for x, y in code.order()]
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits) - 7, 8)]
    ec, sizes = qr.BLOCKS[code.version]
    blocks = [[] for _ in sizes]
    k = 0
    for i in range(max(sizes)):
        for b, size in enumerate(sizes):
            if i < size:
                blocks[b].append(words[k])
                k += 1
    for i in range(ec):
        for b in range(len(sizes)):
            blocks[b].append(words[k])
            k += 1
    for b, size in zip(blocks, sizes):
        assert qr.ecc(b[:size], ec) == b[size:], "error correction doesn't match"
    data = [w for b, size in zip(blocks, sizes) for w in b[:size]]
    stream = "".join(f"{w:08b}" for w in data)
    assert stream[:4] == "0100", "not byte mode"
    count_bits = 8 if code.version < 10 else 16
    length = int(stream[4:4 + count_bits], 2)
    start = 4 + count_bits
    return bytes(int(stream[start + 8 * i:start + 8 * i + 8], 2) for i in range(length)).decode("utf-8")


class Standard(unittest.TestCase):
    def test_reed_solomon_example(self):
        # The standard's walk-through: HELLO WORLD at 1-M.
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qr.ecc(data, 10), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_format_bits(self):
        table = ["101010000010010", "101000100100101", "101111001111100", "101101101001011",
                 "100010111111001", "100000011001110", "100111110010111", "100101010100000"]
        self.assertEqual([f"{qr.format_bits(k):015b}" for k in range(8)], table)

    def test_version_bits(self):
        self.assertEqual(qr.version_bits(7), 0x07C94)
        self.assertEqual(qr.version_bits(8), 0x085BC)


class Codes(unittest.TestCase):
    def test_read_back_at_every_size(self):
        for length in (1, 10, 14, 26, 42, 62, 84, 106, 122, 152, 180, 213):
            text = ("http://192.168.178.23:49152/?k=" + "AbC-d_9" * 40)[:length]
            code = qr.QR(text)
            self.assertEqual(read_back(code), text, (length, code.version))

    def test_versions_and_sizes(self):
        self.assertEqual((qr.QR("x").version, qr.QR("x").size), (1, 21))
        link = "http://192.168.178.23:49152/?k=" + "A" * 22
        code = qr.QR(link)
        self.assertEqual(code.version, 4)       # the phone remote's link: 33 x 33 modules
        self.assertEqual(code.size, 33)

    def test_finder_and_timing_patterns(self):
        code = qr.QR("hello")
        n, m = code.size, code.dark
        for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)):
            self.assertTrue(all(m[y0][x0 + i] for i in range(7)))
            self.assertFalse(m[y0 + 1][x0 + 1])
            self.assertTrue(m[y0 + 3][x0 + 3])
        self.assertEqual([m[6][x] for x in range(8, n - 8)], [x % 2 == 0 for x in range(8, n - 8)])
        self.assertTrue(m[n - 8][8])                # the dark module

    def test_too_long(self):
        with self.assertRaises(ValueError):
            qr.QR("x" * 300)

    def test_svg(self):
        svg = qr.QR("hi").svg()
        self.assertTrue(svg.startswith("<svg") and 'viewBox="0 0 29 29"' in svg)


if __name__ == "__main__":
    unittest.main()
