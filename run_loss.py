#!/usr/bin/env python3
"""FEC loss-injection harness for the color mode.

For a given geometry + (M, R, k, m) profile: encode the payload, then for
each loss scenario (cut d consecutive whole stream groups at a chosen
offset, where d in 1..m) re-decode and verify byte-exactness plus the
repaired-group count. Cuts start AFTER the metadata (R_META frames) and at
group boundaries, so they remove whole groups (the axis FEC covers).

A cut of d groups inside one stripe must be repaired; a cut of m+1 groups
inside one stripe must FAIL (reported, not fatal).

Scenarios:
  - 'data'  : the cut starts at a data-group boundary, aligned to the
              stripe (groups 0..k-1 of stripe s), d consecutive.
  - 'tail'  : the cut starts at the stripe's parity section start (data
              intact, d parity groups removed).
  - 'mid'   : the cut starts k//2 groups into the stripe (straddling).

Usage:
  python run_loss.py W H M R K M_ [CRF] [N_GROUPS_PAYLOAD]
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = '/home/mmn/file_to_video_bitcoder/venv/bin/python'
SCRIPT = os.path.join(ROOT, 'VC7030_color.py')
R_META = 30
PAYLOAD = '/tmp/dens_payload.bin'


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def run(args, timeout=1800):
    p = subprocess.run([PY, SCRIPT] + args, cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    return p.returncode, p.stdout.decode('utf-8', 'replace')


def cut_groups(src, dst, start_group, n_groups, R, fps=30):
    """Cut n_groups whole stream groups (R frames each) starting at
    stream-group start_group (0 = first group after the metadata).

    Re-encodes the surviving frames with CRF 0 (lossless) so the cut
    itself introduces no extra distortion — only the group loss is the
    variable under test. Returns the frame count of the cut file."""
    start_frame = R_META + start_group * R
    end_frame = start_frame + n_groups * R
    cmd = ['ffmpeg', '-y', '-v', 'error', '-i', src,
           '-vf', f'select=if(between(n\\,{start_frame}\\,{end_frame - 1})\\,0\\,1),setpts=N/{fps}/TB',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '0',
           '-an', dst]
    subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    n = subprocess.run(['ffprobe', '-v', 'error', '-count_frames',
                        '-select_streams', 'v:0',
                        '-show_entries', 'stream=nb_read_frames',
                        '-of', 'csv=p=0', dst],
                       check=True, capture_output=True,
                       timeout=900).stdout.decode().strip()
    return int(n)


def frame_count(path):
    n = subprocess.run(['ffprobe', '-v', 'error', '-count_frames',
                        '-select_streams', 'v:0',
                        '-show_entries', 'stream=nb_read_frames',
                        '-of', 'csv=p=0', path],
                       check=True, capture_output=True,
                       timeout=900).stdout.decode().strip()
    return int(n)


def main():
    (w, h, M, R, k, m, crf) = [int(x) for x in sys.argv[1:8]]
    os.makedirs(os.path.join(ROOT, 'reconstructed'), exist_ok=True)
    rec = os.path.join(ROOT, 'reconstructed', 'dens_payload.bin')
    if os.path.exists(rec):
        os.remove(rec)
    if not os.path.exists(PAYLOAD):
        with open(PAYLOAD, 'wb') as f:
            f.write(os.urandom(256 * 1024))
    payload_sha = sha256(PAYLOAD)
    size = os.path.getsize(PAYLOAD)

    base = f'/tmp/loss_{w}x{h}_M{M}R{R}_k{k}m{m}.mp4'
    t1 = time.time()
    rc, log = run(['encode', PAYLOAD, str(M), str(R), str(w), str(h), '8',
                   '--crf', str(crf), '--color', '--fec-k', str(k),
                   '--fec-m', str(m), '--out', base])
    if rc != 0:
        print('ENCODE FAILED')
        print(log[-1500:])
        sys.exit(1)
    print(f'encoded {os.path.getsize(base)} bytes in {time.time()-t1:.1f}s')

    import re
    bmatch = re.search(r'B=(\d+) bytes/group, (\d+) data', log)
    B = int(bmatch.group(1)) if bmatch else 0
    n_strips = (size + B - 1) // B if B else 0
    base_frames = frame_count(base)

    scenarios = []
    # d groups lost per stripe: 1..m must repair; m+1 must fail (if it
    # fits inside one stripe).
    for d in list(range(1, m + 1)) + ([m + 1] if m + 1 <= k else []):
        scenarios.append(('data', 0, d))          # stripe 0, data head
        if m + 1 <= k:
            scenarios.append(('mid', 0, d))       # stripe 0, mid-stripe
        scenarios.append(('tail', 0, d))          # stripe 0, parity tail
    # and one in a LATER stripe if there is one
    if n_strips > 1:
        scenarios.append(('data', k, min(2, m)))

    results = []
    for kind, s_off, d in scenarios:
        # map (stripe offset, within) -> stream group index
        if kind == 'data':
            start_g = s_off
        elif kind == 'tail':
            start_g = k + m - d  # last d groups of stripe 0 = its parity tail
        else:
            start_g = k // 2
        cut = f'/tmp/loss_cut_{kind}{d}_{w}.mp4'
        try:
            nframes = cut_groups(base, cut, start_g, d, R)
            if nframes != base_frames - d * R:
                print(f'{kind} d={d}: CUT LENGTH WRONG '
                      f'({nframes} != {base_frames - d * R}), skipping')
                continue
        except subprocess.CalledProcessError as e:
            print(f'{kind} d={d}: CUT FAILED {e.stderr[-300:]}')
            continue
        if os.path.exists(rec):
            os.remove(rec)
        t1 = time.time()
        rc2, dlog = run(['decode', cut, '8'])
        ok = rc2 == 0 and os.path.exists(rec) and sha256(rec) == payload_sha
        repaired = re.search(r'(\d+) groups repaired', dlog)
        margin = re.search(r'margin ([\d.]+)%', dlog)
        expect_ok = d <= m
        verdict = 'OK' if ok == expect_ok else 'UNEXPECTED'
        results.append((verdict, kind, start_g, d, ok,
                        repaired.group(1) if repaired else '?',
                        margin.group(1) if margin else '?'))
        print(f'[{verdict}] {kind:4s} cut@{start_g:3d} d={d} -> '
              f'ok={ok} (expect {expect_ok}), repaired={results[-1][5]}, '
              f'margin={results[-1][6]}%  ({time.time()-t1:.0f}s)', flush=True)

    bad = [r for r in results if r[0] == 'UNEXPECTED']
    print(f'\n{len(results) - len(bad)}/{len(results)} as expected')
    if bad:
        print('UNEXPECTED RESULTS:', bad)
        sys.exit(2)


if __name__ == '__main__':
    main()
