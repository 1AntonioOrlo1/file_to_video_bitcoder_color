#!/usr/bin/env python3
"""Push for MAXIMUM density at M=8 (the DCT-aligned winner).

The mp4 size scales linearly with R (copies per group). At M=8 the
threshold margin was ~80% at R=4, i.e. compression flips no bits — so we
can cut R (fewer frames = smaller video) and lean on FEC to repair the
groups that compression or loss erases. This sweep finds the smallest R
that still roundtrips byte-exact, and the m that makes it safe.

Grid: M=8, k=8, CRF 23, 2 MB PNG. R in {1,2,3,4}, m in {1,2,4}.
Tested at 1080p (size is resolution-independent) and re-verified at 4K.
"""
import json, os, re, subprocess, time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = '/home/mmn/file_to_video_bitcoder/venv/bin/python'
SCRIPT = os.path.join(ROOT, 'VC7030_color.py')
PAYLOAD = '/tmp/mmn_test.png'
CELL = f"{ROOT}/.sweep.mp4"
SHA = "7a654c51e1a589e7b12907b7373e975db9550830788f2a98f0bd7af24366cd5b"
PROCS = 8
CRF = 23


def sha256(p):
    import hashlib
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


def run(args, timeout=1800):
    p = subprocess.run([PY, SCRIPT] + args, cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    return p.returncode, p.stdout.decode('utf-8', 'replace')


def cell(w, h, M, R, k, m):
    rec = dict(w=w, h=h, M=M, R=R, k=k, m=m, ok=False, size=None, margin=None)
    try:
        rc, log = run(['encode', PAYLOAD, str(M), str(R), str(w), str(h),
                       str(PROCS), '--crf', str(CRF), '--color',
                       '--fec-k', str(k), '--fec-m', str(m), '--out', CELL])
        rec['enc_ok'] = rc == 0
        if rc == 0:
            rec['size'] = os.path.getsize(CELL)
            rc2, dlog = run(['decode', CELL, str(PROCS)])
            mm = re.search(r'min threshold margin ([\d.]+)%', dlog)
            rec['margin'] = float(mm.group(1)) / 100 if mm else None
            outp = os.path.join(ROOT, 'reconstructed', os.path.basename(PAYLOAD))
            rec['ok'] = rc2 == 0 and os.path.exists(outp) and sha256(outp) == SHA
    except Exception as e:
        rec['err'] = str(e)[:100]
    return rec


def main():
    rows = []
    for R in [1, 2, 3, 4]:
        for m in [1, 2, 4]:
            rec = cell(1920, 1080, 8, R, 8, m)
            rows.append(rec)
            mg = f"{rec['margin']*100:.1f}%" if rec['margin'] is not None else '-'
            print(f"1080p M=8 R={R} m={m}: ok={rec['ok']} "
                  f"size={rec['size']/1e6 if rec['size'] else 0:6.2f}MB margin={mg}",
                  flush=True)
    # re-verify the best few at 4K
    print("\n--- 4K re-verify of passing cells ---", flush=True)
    passing = sorted([r for r in rows if r['ok']], key=lambda z: z['size'])
    for rec in passing[:4]:
        r4 = cell(3840, 2160, 8, rec['R'], 8, rec['m'])
        mg = f"{r4['margin']*100:.1f}%" if r4['margin'] is not None else '-'
        print(f"4K    M=8 R={rec['R']} m={rec['m']}: ok={r4['ok']} "
              f"size={r4['size']/1e6 if r4['size'] else 0:6.2f}MB margin={mg}",
              flush=True)
    print("\n=== densest passing (1080p) ===")
    for r in passing:
        mg = f"{r['margin']*100:.1f}%" if r['margin'] is not None else '-'
        print(f"  R={r['R']} m={r['m']} size={r['size']/1e6:.2f}MB margin={mg}")
    json.dump(rows, open(os.path.join(ROOT, 'sweep_results.json'), 'w'))


if __name__ == '__main__':
    main()
