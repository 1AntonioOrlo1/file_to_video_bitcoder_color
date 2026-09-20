#!/usr/bin/env python3
"""Prototype: 16-color palette (2 bits/channel, 4 levels) for higher density.

Compares against the shipped 8-color (3 bits/block) path. The question: does
a 4-level-per-channel palette (0/85/170/255 -> 6 bits/block) survive CRF 23
byte-exact, and at what mp4 size? If yes, it's ~2x denser than 8-color.
If the levels smear, it proves why 8-color (binary, max margin) is optimal.

Self-contained: does not touch VC7030_color.py. Uses the same 2 MB PNG.
"""
import os, subprocess, hashlib, numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = '/tmp/mmn_test.png'
W, H, M = 1920, 1080, 8
CRF = 23
SHA = "7a654c51e1a589e7b12907b7373e975db9550830788f2a98f0bd7af24366cd5b"
LEVELS = np.array([0, 85, 170, 255], dtype=np.uint8)  # 4 levels/channel
BPP = 6  # bits per block (2 per channel)

def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()

def gen_frame(idx, data, total_bits, bpf):
    """Render stream frame idx: BPP file-bits per block, 2 bits/channel."""
    start = idx * bpf
    end = min(start + bpf, total_bits)
    n = end - start
    bx, by = W // M, H // M
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder='big')
    chunk = bits[start:end]
    bit_array = np.zeros(bpf, dtype=np.uint8)
    bit_array[:min(n, chunk.size)] = chunk[:min(n, chunk.size)]
    # 6 bits/block, MSB-first: R_hi, R_lo, G_hi, G_lo, B_hi, B_lo — exactly
    # the order the decoder reads (by, bx, 3 channels, 2 bits hi/lo).
    rgb = bit_array[:6 * bx * by].reshape(by, bx, 3, 2)
    lvl = (rgb[:, :, :, 0] << 1) | rgb[:, :, :, 1]   # (by,bx,3) level index
    img = np.zeros((H, W, 3), dtype=np.uint8)
    blk = LEVELS[np.clip(lvl, 0, 3)]                   # (by,bx,3) actual levels
    img[:by * M, :bx * M] = np.repeat(np.repeat(blk, M, axis=0), M, axis=1)
    return img.tobytes()

def encode(data, total_bits, bpf, R, out):
    bx, by = W // M, H // M
    bpf = bx * by * BPP
    nframes = (total_bits + bpf - 1) // bpf
    cmd = ['ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo',
           '-s', f'{W}x{H}', '-pix_fmt', 'rgb24', '-r', '30', '-i', '-',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', str(CRF),
           '-preset', 'medium', out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for f in range(nframes):
        for _ in range(R):
            p.stdin.write(gen_frame(f, data, total_bits, bpf))
    p.stdin.close(); p.wait()
    return os.path.getsize(out) if p.returncode == 0 else None

def decode(out, R, bpf, total_bits):
    bx, by = W // M, H // M
    nframes = (total_bits + bpf - 1) // bpf
    cmd = ['ffmpeg', '-i', out, '-f', 'rawvideo', '-pix_fmt', 'rgb24',
           '-v', 'error', '-']
    p = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=1800)
    raw = p.stdout
    fs = W * H * 3
    out_bits = np.zeros(total_bits, dtype=np.uint8)
    min_margin = 1.0
    for f in range(nframes):
        acc = np.zeros((by, bx, 3), dtype=np.int64)
        for r in range(R):
            off = (f * R + r) * fs
            arr = np.frombuffer(raw, dtype=np.uint8)[off:off + fs].reshape(H, W, 3)
            acc += arr[:by * M, :bx * M].reshape(by, M, bx, M, 3).sum(
                axis=(1, 3), dtype=np.int64)
        # nearest level per channel
        mean = acc / R
        # thresholds between levels
        thr = (LEVELS[:-1].astype(np.int64) + LEVELS[1:].astype(np.int64)) // 2
        idx = np.zeros((by, bx, 3), dtype=np.int64)
        for t in thr:
            idx += (mean >= t).astype(np.int64)
        # margin: distance to nearest threshold, relative
        for t in thr:
            min_margin = min(min_margin, float(np.min(np.abs(mean - t)) / 127.5))
        lvl = idx  # 0..3 per channel
        # unpack to 2 bits/channel, MSB-first: high bit then low bit
        high = (lvl >> 1).astype(np.uint8)
        low = (lvl & 1).astype(np.uint8)
        flat = np.stack([high, low], axis=-1).reshape(-1)  # interleaved hi,lo per ch
        # reorder to R_hi,R_lo,G_hi,G_lo,B_hi,B_lo
        flat = flat.reshape(by, bx, 3, 2).reshape(-1)
        start = f * bpf
        end = min(start + bpf, total_bits)
        out_bits[start:end] = flat[:end - start]
    # bits -> bytes
    nb = (total_bits + 7) // 8
    padded = np.zeros(nb * 8, dtype=np.uint8)
    padded[:total_bits] = out_bits
    w8 = np.array([128, 64, 32, 16, 8, 4, 2, 1], dtype=np.uint8)
    return (padded.reshape(-1, 8) * w8).sum(axis=1, dtype=np.uint8).tobytes(), min_margin

def main():
    data = open(PAYLOAD, 'rb').read()
    total_bits = len(data) * 8
    bx, by = W // M, H // M
    bpf = bx * by * BPP
    for R in [1, 2]:
        out = f'/tmp/proto16_R{R}.mp4'
        size = encode(data, total_bits, bpf, R, out)
        if size is None:
            print(f"R={R}: encode FAIL"); continue
        rec, margin = decode(out, R, bpf, total_bits)
        ok = hashlib.sha256(rec).hexdigest() == SHA
        print(f"16-color M=8 R={R}: size={size/1e6:.2f}MB margin={margin*100:.1f}% byte-exact={ok}")

if __name__ == '__main__':
    main()
