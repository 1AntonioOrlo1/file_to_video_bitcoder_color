#!/usr/bin/env python3
"""FEC stripe-length (k) sweep on the real 2 MB PNG.

Raising k shrinks the parity overhead (m=2: k=8 -> 25.6% of groups,
k=32 -> 7.3%, k=64 -> 3.7%). This measures the real mp4 size, byte-exactness
and margin for each k, for both auto profiles (R=2) and max-dense (R=1),
at 1080p, CRF 23, veryslow (the auto preset). Sequential — no parallel
encodes (master's rule). Resumable: skips cells already in k_sweep.json.
"""
import json, os, subprocess, sys

ROOT = '/home/mmn/file_to_video_bitcoder_color'
PY = '/home/mmn/file_to_video_bitcoder/venv/bin/python'
SCRIPT = os.path.join(ROOT, 'VC7030_color.py')
PAYLOAD = '/tmp/mmn_test.png'
SHA = '7a654c51e1a589e7b12907b7373e975db9550830788f2a98f0bd7af24366cd5b'
OUT = os.path.join(ROOT, 'k_sweep.json')
W, H, PROC = 1920, 1080, 8


def ensure_payload():
    if not os.path.exists(PAYLOAD):
        import shutil
        shutil.copy('/home/mmn/Изображения/GtelhrJb0AAkpnI2.png', PAYLOAD)
    import hashlib
    h = hashlib.sha256(open(PAYLOAD, 'rb').read()).hexdigest()
    assert h == SHA, f'payload sha mismatch: {h}'


def run(args, timeout=1800):
    p = subprocess.run([PY, SCRIPT] + args, cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    return p.returncode, p.stdout.decode('utf-8', 'replace')


def main():
    ensure_payload()
    results = {}
    if os.path.exists(OUT):
        results = json.load(open(OUT))
    cells = []
    for R, tag in ((2, 'auto'), (1, 'max-dense')):
        # GF(256) Cauchy cap: P[i][j]=1/(x_i+y_j), x_i=i, y_j=k+j -> field
        # elements up to 2k+m-2, must be < 256. For m=2 that caps k at 127,
        # for m=1 at 128.
        for k in (8, 16, 32, 64, 127):
            cells.append((R, k, 2, tag))
        # m=1 = absolute floor (halves parity); thinnest protection, only
        # repairs a single-group cut per stripe. Measured for the record.
        for k in (32, 64, 128):
            cells.append((R, k, 1, f'{tag}-m1'))
    for R, k, m, tag in cells:
        key = f'{tag}_k{k}m{m}'
        if key in results:
            print(f'[{key}] cached: {results[key]}', flush=True)
            continue
        rec = os.path.join(ROOT, 'reconstructed', 'mmn_test.png')
        if os.path.exists(rec):
            os.remove(rec)
        mp4 = f'/tmp/ksweep_{tag}_k{k}m{m}.mp4'
        rc, log = run(['encode', PAYLOAD, '8', str(R), str(W), str(H), str(PROC),
                       '--crf', '23', '--color', '--fec-k', str(k), '--fec-m', str(m),
                       '--preset', 'veryslow', '--out', mp4])
        size = os.path.getsize(mp4) if os.path.exists(mp4) else 0
        rc2, dlog = run(['decode', mp4, str(PROC)])
        ok = rc2 == 0 and os.path.exists(rec) and \
            __import__('hashlib').sha256(open(rec, 'rb').read()).hexdigest() == SHA
        import re
        margin = re.search(r'min threshold margin ([\d.]+)%', dlog)
        results[key] = {'size_mb': round(size / 1048576, 2),
                        'byte_exact': ok,
                        'margin': margin.group(1) if margin else None,
                        'enc_rc': rc, 'dec_rc': rc2}
        json.dump(results, open(OUT, 'w'), indent=1)
        print(f'[{key}] size={results[key]["size_mb"]}MB exact={ok} '
              f'margin={results[key]["margin"]}%', flush=True)
    print('\n=== summary (sorted by size) ===')
    for key, r in sorted(results.items(), key=lambda kv: kv[1]['size_mb']):
        print(f'{key:18s}: {r["size_mb"]:>6} MB  exact={r["byte_exact"]}  '
              f'margin={r["margin"]}%')
    bad = [k for k, v in results.items() if not v['byte_exact']]
    print('ALL PASS' if not bad else f'FAILURES: {bad}')
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
