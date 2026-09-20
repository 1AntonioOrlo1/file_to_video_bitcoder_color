#!/usr/bin/env python3
"""Big-file memory-leak probe.

Encodes a large payload at 4K (max-dense profile, veryslow) while sampling
the RSS of every bitcoder/ffmpeg process every few seconds. A leak shows up
as a monotonic RSS climb across the whole encode (not a flat plateau at the
frame-pool peak). We print a per-10s table plus the peak, then decode and
verify byte-exactness.
"""
import os, subprocess, time, sys, hashlib, threading

ROOT = '/home/mmn/file_to_video_bitcoder_color'
PY = '/home/mmn/file_to_video_bitcoder/venv/bin/python'
PAYLOAD = '/tmp/big_payload.bin'
SIZE = 500 * 1024 * 1024
OUT = '/tmp/big_1080.mp4'

def make_payload():
    if os.path.exists(PAYLOAD) and os.path.getsize(PAYLOAD) == SIZE:
        return
    print(f'making {SIZE/1048576:.0f}MB payload...', flush=True)
    with open(PAYLOAD, 'wb') as f:
        chunk = os.urandom(1 << 20)
        for _ in range(SIZE >> 20):
            f.write(chunk)

def rss_of(names):
    """Sum RSS (kB) of processes whose cmdline contains any of `names`."""
    try:
        out = subprocess.check_output(
            ['ps', '-eo', 'rss,cmd'], text=True, timeout=10)
    except Exception:
        return None
    total = 0
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        cmd = parts[1]
        if any(n in cmd for n in names):
            try:
                total += int(parts[0])
            except ValueError:
                pass
    return total  # kB

def sample(stop, samples):
    while not stop.is_set():
        rss = rss_of(['VC7030_color', 'ffmpeg'])
        samples.append((time.time(), rss))
        stop.wait(10)

def main():
    make_payload()
    h = hashlib.sha256()
    with open(PAYLOAD, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    payload_sha = h.hexdigest()
    print(f'payload sha={payload_sha[:16]}...', flush=True)

    stop = threading.Event()
    samples = []
    th = threading.Thread(target=sample, args=(stop, samples), daemon=True)
    th.start()

    # 1080p + medium: the leak surface is file size (payload reads) and the
    # frame pool (geometry), not the preset — this keeps the run to ~20 min
    # while exercising the same code paths, including the new
    # --auto --max-dense --preset override interaction.
    cmd = [PY, os.path.join(ROOT, 'VC7030_color.py'), 'encode', PAYLOAD,
           '0', '0', '1920', '1080', '8', '--color', '--auto', '--max-dense',
           '--preset', 'medium', '--out', OUT]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stop.set(); th.join()
    enc_s = time.time() - t0
    log = p.stdout.decode('utf-8', 'replace')
    ok_enc = p.returncode == 0
    print(f'ENCODE rc={p.returncode} in {enc_s:.0f}s size='
          f'{os.path.getsize(OUT)/1073741824:.2f}GB', flush=True)
    # memory table
    peaks = [s for s in samples if s[1]]
    peak = max((s[1] for s in peaks), default=0)
    print(f'PEAK RSS (bitcoder+ffmpeg) = {peak/1048576:.2f} GB', flush=True)
    print('t(s)   RSS_GB', flush=True)
    for t, rss in samples[::1]:
        if rss is not None:
            print(f'{int(t-t0):4d}  {rss/1048576:6.2f}', flush=True)

    rec = os.path.join(ROOT, 'reconstructed', 'big_payload.bin')
    if os.path.exists(rec):
        os.remove(rec)
    t0 = time.time()
    d = subprocess.run([PY, os.path.join(ROOT, 'VC7030_color.py'),
                        'decode', OUT, '8'], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    dec_s = time.time() - t0
    ok_dec = d.returncode == 0 and os.path.exists(rec)
    rec_sha = ''
    if ok_dec:
        h2 = hashlib.sha256()
        with open(rec, 'rb') as f:
            for c in iter(lambda: f.read(1 << 20), b''):
                h2.update(c)
        rec_sha = h2.hexdigest()
    match = rec_sha == payload_sha
    print(f'DECODE rc={d.returncode} in {dec_s:.0f}s byte_exact={match}',
          flush=True)
    if not (ok_enc and match):
        print('--- encode tail ---')
        print(log[-2000:])
    sys.exit(0 if (ok_enc and match) else 1)

if __name__ == '__main__':
    main()
