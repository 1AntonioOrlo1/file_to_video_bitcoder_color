"""FEC core for bitcoder: systematic MDS erasure coding over GF(256).

Why this code: the dominant unhandled damage to an encoded video is the
loss of whole frame groups (transport damage, re-encode cuts, dropped
frame ranges), not per-bit noise — that axis is already covered by the
R-copy averaging. A systematic Cauchy MDS code over GF(256) recovers a
stripe of k data frames from ANY k of its k+m frames, so up to m lost
groups per stripe are corrected with zero residual error.

Stripe layout in the video stream: [k data payloads][m parity payloads],
parity placed right after its own data. Parity payloads are rendered and
repeated (R copies) exactly like data frames. The payload is byte-aligned
(pd bits/frame, pd a multiple of 8) because GF(256) operates on bytes.
"""

import hashlib

import numpy as np


class FecError(Exception):
    pass


def _build_tables():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D  # reduce by x^8+x^4+x^3+x^2+1 (classic RS polynomial)
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP_LIST, _LOG_LIST = _build_tables()
_EXP = np.array(_EXP_LIST, dtype=np.uint8)      # 512 entries, EXP[a+b] for a+b<512
_LOG = np.array(_LOG_LIST, dtype=np.uint8)
_INV = np.array([0] + [int(_EXP_LIST[(255 - _LOG_LIST[i]) % 255]) for i in range(1, 256)],
                dtype=np.uint8)


def gf_inv(a):
    return int(_INV[a])


def _gf_mul_scalar(a, b):
    if a == 0 or b == 0:
        return 0
    # int() both: _LOG is uint8, and log(a)+log(b) overflows uint8
    return int(_EXP[(int(_LOG[a]) + int(_LOG[b])) % 255])


def _gf_mul_vec(a, b):
    """Byte-wise GF(256) multiply of array a by scalar b (vectorized)."""
    if b == 0:
        return np.zeros_like(a)
    if b == 1:
        return a
    out = _EXP[(_LOG[a].astype(np.int16) + _LOG[b]) % 255]
    if (a == 0).any():
        out = out.copy()
        out[a == 0] = 0
    return out


def cauchy_matrix(k, m):
    """P: (k, m) systematic parity matrix.

    With H = [I_k; P], P[i][j] = 1/(x_i + y_j), x_i = i, y_j = k + j:
    x_i are distinct, y_j are distinct, x_i + y_j != 0 (i < k <= k + j),
    so every k x k submatrix of H is invertible (Cauchy determinant) —
    any k of the k+m stripe symbols recover the whole stripe.
    """
    if k < 1 or m < 1 or k + m > 255:
        raise FecError(f"invalid stripe size k={k}, m={m}")
    P = np.zeros((k, m), dtype=np.uint8)
    for i in range(k):
        for j in range(m):
            P[i, j] = _INV[i + k + j]
    return P


def fec_encode(data, P):
    """data: (k, p) uint8; P: (k, m) (or a (k', m) prefix of it).
    Returns (m, p) parity rows: parity[j] = XOR_i P[i][j] * data[i]."""
    k, p = data.shape
    m = P.shape[1]
    parity = np.zeros((m, p), dtype=np.uint8)
    for j in range(m):
        acc = np.zeros(p, dtype=np.uint8)
        col = P[:, j]
        for i in np.nonzero(col)[0]:
            acc ^= _gf_mul_vec(data[i], int(col[i]))
        parity[j] = acc
    return parity


def _gf_invert(A, k):
    """Gaussian elimination over GF(256); returns A^-1 (k x k uint8)."""
    M = [list(map(int, row)) for row in A]
    I = [[1 if i == j else 0 for j in range(k)] for i in range(k)]
    for col in range(k):
        piv = next((r for r in range(col, k) if M[r][col] != 0), None)
        if piv is None:
            raise FecError("singular matrix in FEC decode (impossible for MDS selection)")
        if piv != col:
            M[col], M[piv] = M[piv], M[col]
            I[col], I[piv] = I[piv], I[col]
        inv_p = _INV[M[col][col]]
        M[col] = [_gf_mul_scalar(v, inv_p) for v in M[col]]
        I[col] = [_gf_mul_scalar(v, inv_p) for v in I[col]]
        for r in range(k):
            if r != col and M[r][col]:
                f = M[r][col]
                M[r] = [M[r][c] ^ _gf_mul_scalar(M[col][c], f) for c in range(k)]
                I[r] = [I[r][c] ^ _gf_mul_scalar(I[col][c], f) for c in range(k)]
    return np.array(I, dtype=np.uint8)


def fec_decode(received, k, P):
    """received: length (k+m) list of (p,) uint8 arrays or None (erasure).
    k = data rows of THIS stripe (may be shorter than P.shape[0] for the
    final, partial stripe — the parity rows were computed with P[:k]).
    Returns (k, p) data rows. Raises FecError when more than m symbols
    are missing."""
    m = P.shape[1]
    n = len(received)
    if n != k + m or k > P.shape[0]:
        raise FecError(f"stripe shape mismatch: {n} symbols, k={k}, P={P.shape}")
    sel = [i for i in range(k + m) if received[i] is not None]
    lost = k + m - len(sel)
    if lost > m:
        raise FecError(f"stripe lost {lost} of {k + m} groups ({m} correctable)")
    sel = sel[:k]
    A = np.zeros((k, k), dtype=np.uint8)
    for r, i in enumerate(sel):
        if i < k:
            A[r, i] = 1
        else:
            A[r, :] = P[:k, i - k]
    def _as_u8(x):
        # bytes/bytearray decode to a byte array; a bare np array is used as-is.
        if isinstance(x, (bytes, bytearray, memoryview)):
            return np.frombuffer(x, dtype=np.uint8)
        return np.ascontiguousarray(x, dtype=np.uint8)
    B = np.stack([_as_u8(received[i]) for i in sel])
    Ainv = _gf_invert(A, k)
    out = np.zeros((k, B.shape[1]), dtype=np.uint8)
    for i in range(k):
        acc = np.zeros(B.shape[1], dtype=np.uint8)
        row = Ainv[i]
        for j in np.nonzero(row)[0]:
            acc ^= _gf_mul_vec(B[j], int(row[j]))
        out[i] = acc
    return out


# ---------------------------------------------------------------------------
# Per-group header (8 bytes, drawn into the first 64 M-blocks of the frame).
# Lets the decode-side packer verify group placement and RESYNC after a cut:
#   [8b magic 0x5A][16b group seq][32b crc32(data part)][8b spare 0x55]
# A cut that removes exactly d groups makes the next surviving group carry
# seq = pos + d, so the packer can detect the gap (and, with d <= m, let the
# MDS code repair it). An unaligned cut never yields a valid header, so the
# packer exhausts its resync attempts and aborts with a clear error.
HEADER_BYTES = 8
HEADER_BITS = 64
MAGIC = 0x5A
SPARE = 0x55


def pack_header(g, data_part):
    """8-byte header for stream group g over its data_part bytes."""
    import zlib
    crc = zlib.crc32(data_part) & 0xFFFFFFFF
    return (bytes([MAGIC])
            + ((g & 0xFFFF)).to_bytes(2, 'big')
            + crc.to_bytes(4, 'big')
            + bytes([SPARE]))


def unpack_header(h8):
    """Validate an 8-byte header. Returns (g, ok)."""
    if len(h8) < 8:
        return None, False
    if h8[0] != MAGIC or h8[7] != SPARE:
        return None, False
    g = int.from_bytes(h8[1:3], 'big')
    import zlib
    # crc is checked by the caller against the payload's data part
    return g, True


def header_crc(h8):
    return int.from_bytes(h8[3:7], 'big')


def bits64_to_bytes(bits):
    """First 64 bits (iterable of 0/1) -> 8 bytes, MSB-first."""
    b = 0
    for i in range(64):
        b = (b << 1) | (1 if bits[i] else 0)
    return b.to_bytes(8, 'big')


def stripe_plan(df, k, m):
    """Stripe geometry for df data frames: list of (data_start, k_in_stripe).
    Total stream groups = df + len(strips) * m; stripe s occupies stream
    indices [ds .. ds + k_s + m - 1] (parity right after its data)."""
    strips = []
    s = 0
    while s * k < df:
        k_s = min(k, df - s * k)
        strips.append((s * k, k_s))
        s += 1
    return strips


def render_payload(payload, M, width, height, header=None):
    """Render payload bytes to a gray frame (0/255), one bit per M x M
    block, row-major, MSB-first — identical layout to data frames.
    `header` (8 bytes) is drawn FIRST (the first 64 blocks), the payload
    after it; blocks beyond (header+payload) bits stay black. Requires at
    least 64 blocks for the header."""
    blocks_x = width // M
    blocks_y = height // M
    total = blocks_x * blocks_y
    if header is not None and total < HEADER_BITS:
        raise ValueError(f"need >= {HEADER_BITS} blocks for the group header "
                         f"(have {total})")
    blob = (header or b'') + payload
    n_bits = len(blob) * 8
    if n_bits > total:
        raise ValueError(f"payload {n_bits} bits does not fit {total} blocks")
    raw = np.frombuffer(blob, dtype=np.uint8)
    shift = np.array([7, 6, 5, 4, 3, 2, 1, 0], dtype=np.uint8)
    bits = ((raw[:, None] >> shift) & 1).reshape(-1)[:n_bits].astype(bool)
    full = np.zeros(total, dtype=bool)
    full[:n_bits] = bits
    matrix = full.reshape(blocks_y, blocks_x)
    expanded = matrix.repeat(M, axis=0).repeat(M, axis=1)
    img = np.zeros((height, width), dtype=np.uint8)
    img[:blocks_y * M, :blocks_x * M] = expanded * 255
    return img.tobytes()


def hash64_file(f):
    """First 16 hex chars (64 bits) of SHA-256 of the stream at f's pos."""
    h = hashlib.sha256()
    while True:
        chunk = f.read(1 << 20)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()[:16]
