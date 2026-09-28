"""Generador de códigos QR en Python puro (sin dependencias: el runtime portable no tiene site-packages).

Solo lo que hace falta para el emparejamiento con MovilDeep: modo byte (UTF-8), nivel de corrección M, versiones 1 a
40, y la máscara elegida por la penalización del estándar (ISO/IEC 18004). Sigue el algoritmo de referencia de Project
Nayuki (licencia MIT).

    m = encode("movildeep:{...}")    # lista de filas; True = módulo oscuro
"""

# códigos de corrección por bloque y cantidad de bloques, índice = versión (el 0 no se usa); nivel M
_ECC_PER_BLOCK_M = (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28,
                    28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28)
_NUM_BLOCKS_M = (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28,
                 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49)
_ECL_M_BITS = 0          # los dos bits del nivel M en la información de formato


def _raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        k = ver // 7 + 2
        n -= (25 * k - 10) * k - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK_M[ver] * _NUM_BLOCKS_M[ver]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver):
    if ver == 1:
        return []
    num = ver // 7 + 2
    step = (ver * 8 + num * 3 + 5) // (num * 4 - 4) * 2
    result = [ver * 4 + 17 - 7 - i * step for i in range(num - 1)] + [6]
    return list(reversed(result))


class _Qr:
    def __init__(self, ver, codewords, mask):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]
        self._function_patterns()
        self._draw_codewords(self._add_ecc(codewords))
        if mask is None:
            best, best_pen = 0, None
            for m in range(8):
                self._apply_mask(m)
                self._format_bits(m)
                pen = self._penalty()
                if best_pen is None or pen < best_pen:
                    best, best_pen = m, pen
                self._apply_mask(m)          # la máscara es XOR: aplicarla de nuevo la quita
            mask = best
        self.mask = mask
        self._apply_mask(mask)
        self._format_bits(mask)

    def _set_fn(self, x, y, dark):
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def _function_patterns(self):
        s = self.size
        for i in range(s):
            self._set_fn(6, i, i % 2 == 0)
            self._set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        d = max(abs(dx), abs(dy))
                        self._set_fn(x, y, d not in (2, 4))
        pos = _alignment_positions(self.ver)
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set_fn(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self._format_bits(0)          # reserva el lugar; se reescribe con la máscara elegida
        self._version_bits()

    def _format_bits(self, mask):
        data = _ECL_M_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        s = self.size
        for i in range(0, 6):
            self._set_fn(8, i, (bits >> i) & 1 != 0)
        self._set_fn(8, 7, (bits >> 6) & 1 != 0)
        self._set_fn(8, 8, (bits >> 7) & 1 != 0)
        self._set_fn(7, 8, (bits >> 8) & 1 != 0)
        for i in range(9, 15):
            self._set_fn(14 - i, 8, (bits >> i) & 1 != 0)
        for i in range(0, 8):
            self._set_fn(s - 1 - i, 8, (bits >> i) & 1 != 0)
        for i in range(8, 15):
            self._set_fn(8, s - 15 + i, (bits >> i) & 1 != 0)
        self._set_fn(8, s - 8, True)       # el módulo oscuro fijo

    def _version_bits(self):
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.ver << 12 | rem
        for i in range(18):
            bit = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self._set_fn(a, b, bit)
            self._set_fn(b, a, bit)

    def _add_ecc(self, data):
        ver = self.ver
        nblocks, ecc_len = _NUM_BLOCKS_M[ver], _ECC_PER_BLOCK_M[ver]
        raw = _raw_modules(ver) // 8
        short_blocks = nblocks - raw % nblocks
        short_len = raw // nblocks
        div = _rs_divisor(ecc_len)
        blocks, k = [], 0
        for i in range(nblocks):
            dat = data[k:k + short_len - ecc_len + (0 if i < short_blocks else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, div)
            if i < short_blocks:
                dat = dat + [0]
            blocks.append(dat + ecc)
        result = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != short_len - ecc_len or j >= short_blocks:
                    result.append(blk[i])
        return result

    def _draw_codewords(self, data):
        s = self.size
        i = 0
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = s - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.mod[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1
            right -= 2

    def _apply_mask(self, m):
        f = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)[m]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.mod[y][x] = not self.mod[y][x]

    def _penalty(self):
        s, mod = self.size, self.mod
        pen = 0
        for lines in (mod, [list(c) for c in zip(*mod)]):
            for line in lines:
                run_color, run = False, 0
                hist = [0] * 7
                for x in range(s):
                    if line[x] == run_color:
                        run += 1
                        if run == 5:
                            pen += 3
                        elif run > 5:
                            pen += 1
                    else:
                        self._hist_add(run, hist)
                        if not run_color:
                            pen += self._finder_count(hist) * 40
                        run_color, run = line[x], 1
                pen += self._finder_terminate(run_color, run, hist) * 40
        for y in range(s - 1):
            for x in range(s - 1):
                c = mod[y][x]
                if c == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                    pen += 3
        dark = sum(sum(1 for c in row if c) for row in mod)
        total = s * s
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        pen += k * 10
        return pen

    def _hist_add(self, run, hist):
        if hist[0] == 0:
            run += self.size
        hist.pop()
        hist.insert(0, run)

    def _finder_count(self, h):
        n = h[1]
        core = n > 0 and h[2] == h[4] == h[5] == n and h[3] == n * 3
        return (1 if core and h[0] >= n * 4 and h[6] >= n else 0) + (1 if core and h[6] >= n * 4 and h[0] >= n else 0)

    def _finder_terminate(self, run_color, run, hist):
        if run_color:
            self._hist_add(run, hist)
            run = 0
        run += self.size
        self._hist_add(run, hist)
        return self._finder_count(hist)


def encode(text, mask=None):
    """Matriz del QR de text (UTF-8, modo byte, nivel M): lista de filas de booleanos, sin zona de silencio."""
    data = text.encode("utf-8")
    for ver in range(1, 41):
        cap = _data_codewords(ver) * 8
        count_bits = 8 if ver <= 9 else 16
        used = 4 + count_bits + len(data) * 8
        if used <= cap:
            break
    else:
        raise ValueError("el texto no entra en un código QR")
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))
    put(0x4, 4)
    put(len(data), count_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    return _Qr(ver, codewords, mask).mod
