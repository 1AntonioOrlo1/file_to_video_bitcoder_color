#!/usr/bin/env python3
"""Density benchmark for the color+FEC mode at typical YouTube geometries.

PAYLOAD is a REAL 2 MB file (a PNG, /tmp/mmn_test.png), not random noise —
it has the structure of what the user actually encodes, so the measured
size and margin are representative.

For every (resolution, M, R, m) cell (k fixed at 8) we encode the PNG,
decode it, and record mp4 size, byte-exactness, the minimum threshold
margin, and the data/parity group counts.

Density model:
  * video size  ~  (n_data + n_strips*m) * R        [stream groups x copies]
  * n_data      ~  filesize / B,  B ~ W*H/M^2       [bytes per group]
  so SMALLER M (more blocks/frame, fatter groups) and SMALLER R (fewer
  copies) both shrink the video. The cost: a smaller M or R leaves less
  averaging headroom, so compression (CRF 23) flips more bits and more
  groups fail their CRC — those become erasures that FEC repairs, as long
  as <= m groups per stripe fail. m is the knob that trades a little size
  (parity overhead) against catching exactly those small-M/R defects.

The summary ranks, per geometry, the passing cells by mp4 size (densest
first); the winner for each geometry becomes a DENSITY_PROFILES entry.

Results are appended to results.json next to this script (resumable across
reboots: cells already recorded for this payload + k are skipped).
"""
import json
import os
import re
import subprocess
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = '/home/mmn/file_to_video_bitcoder/venv/bin/python'
SCRIPT = os.path.join(ROOT, 'VC7030_color.py')
OUT = os.path.join(ROOT, 'results.json')
PROCS = 8
CRF = 23
K = 8                                  # fixed data-groups-per-stripe
# The real test file the user asked for (2 MB RGBA PNG).
PAYLOAD = '/tmp/mmn_test.png'
PAYLOAD_FALLBACK = '/home/mmn/Изображения/GtelhrJb0AAkpnI2.png'

# Typical YouTube geometries; 1080p and 4K are the focus, 720p reference.
RESOLUTIONS = [(1280, 720), (1920, 1080), (3840, 2160)]
M_VALUES = [6, 8, 10, 12]              # block size — the main density lever
R_VALUES = [4, 6, 8, 12]               # copies per group — the other lever
M_PARITY = [2, 4]                      # parity groups — defect-catch knob

CELL = f"{ROOT}/.cell.mp4"             # .mp4 extension: ffmpeg infers muxer


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def ensure_payload():
    """Stage the real PNG payload and clear stale reconstructions."""
    src = PAYLOAD if os.path.exists(PAYLOAD) else PAYLOAD_FALLBACK
    if not os.path.exists(src):
        raise SystemExit(f"payload not found: {src}")
    if not os.path.exists(PAYLOAD) or \
            os.path.getsize(PAYLOAD) != os.path.getsize(src):
        subprocess.run(['cp', src, PAYLOAD], check=True)
    recdir = os.path.join(ROOT, 'reconstructed')
    os.makedirs(recdir, exist_ok=True)
    for old in os.listdir(recdir):
        p = os.path.join(recdir, old)
        if os.path.isfile(p):
            os.remove(p)
    return sha256(PAYLOAD)


def run(args, timeout=1800):
    p = subprocess.run([PY, SCRIPT] + args, cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    return p.returncode, p.stdout.decode('utf-8', 'replace')


def parse_int(log, pattern):
    m = re.search(pattern, log)
    return int(m.group(1)) if m else None


def parse_margin(log):
    m = re.search(r'min threshold margin ([\d.]+)%', log)
    return float(m.group(1)) / 100 if m else None


def main():
    payload_sha = ensure_payload()
    payload_size = os.path.getsize(PAYLOAD)
    print(f"payload: {PAYLOAD} ({payload_size} bytes)", flush=True)

    results = []
    if os.path.exists(OUT):
        results = json.load(open(OUT))
        # Only trust prior results computed for THIS payload + k.
        results = [r for r in results
                   if r.get('sha') == payload_sha and r.get('k') == K]
    seen = {(r['w'], r['h'], r['M'], r['R'], r['k'], r['m']) for r in results}

    cells = [(w, h, M, R, K, m)
             for w, h in RESOLUTIONS
             for M in M_VALUES
             for R in R_VALUES
             for m in M_PARITY]
    t0 = time.time()
    for i, (w, h, M, R, k, m) in enumerate(cells, 1):
        key = (w, h, M, R, k, m)
        if key in seen:
            continue
        rec = dict(w=w, h=h, M=M, R=R, k=k, m=m, sha=payload_sha,
                   ok=None, size=None, margin=None, n_data=None,
                   n_parity=None, enc_s=None, dec_s=None)
        try:
            t1 = time.time()
            rc, log = run(['encode', PAYLOAD, str(M), str(R), str(w), str(h),
                           str(PROCS), '--crf', str(CRF), '--color',
                           '--fec-k', str(k), '--fec-m', str(m), '--out', CELL])
            rec['enc_s'] = round(time.time() - t1, 1)
            rec['n_data'] = parse_int(log, r'(\d+) data \+')
            if rc == 0 and os.path.exists(CELL):
                rec['size'] = os.path.getsize(CELL)
                t1 = time.time()
                rc2, dlog = run(['decode', CELL, str(PROCS)])
                rec['dec_s'] = round(time.time() - t1, 1)
                rec['margin'] = parse_margin(dlog)
                rec['n_parity'] = parse_int(dlog, r'\+ (\d+) parity')
                outp = os.path.join(ROOT, 'reconstructed',
                                    os.path.basename(PAYLOAD))
                rec['ok'] = (rc2 == 0 and os.path.exists(outp)
                             and sha256(outp) == payload_sha)
        except subprocess.TimeoutExpired:
            rec['ok'] = False
            rec['err'] = 'timeout'
        except Exception as e:
            rec['ok'] = False
            rec['err'] = str(e)[:120]
        results.append(rec)
        seen.add(key)
        json.dump(results, open(OUT, 'w'))
        print(f"[{i:3d}/{len(cells)}] {w}x{h} M={M:2d} R={R:2d} m={m} | "
              f"ok={rec['ok']} size={rec['size']} margin={rec['margin']} "
              f"({time.time()-t0:.0f}s)", flush=True)

    # ---- Summary: per geometry, passing cells densest first ----
    print('\n=== SUMMARY (passing cells, densest first, per geometry) ===')
    for w, h in RESOLUTIONS:
        cells = [r for r in results if r['w'] == w and r['h'] == h and r['ok']]
        cells.sort(key=lambda r: r['size'] or 10**15)
        print(f'\n{w}x{h}:')
        for r in cells[:6]:
            mg = f"{r['margin']:.1%}" if r['margin'] is not None else '  -  '
            print(f"  M={r['M']:2d} R={r['R']:2d} m={r['m']}  "
                  f"size={r['size']/1e6:6.2f} MB  margin={mg}  "
                  f"groups={r['n_data']}+{r['n_parity']}  "
                  f"enc={r['enc_s']}s dec={r['dec_s']}s")
    print('\ndone', flush=True)


if __name__ == '__main__':
    main()
