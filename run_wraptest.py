"""Wrap-boundary regression: the FEC header seq is 16-bit, so it wraps
every 65536 stream groups. This test encodes a payload large enough to
cross the boundary TWICE (65536 and 131072) and verifies byte-exact
roundtrip. 640x360 M=16 color: B = (40*22*3 - 192)//8 = 306 bytes/group.

Usage: python run_wraptest.py [payload_mb]
"""
import hashlib
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = '/home/mmn/file_to_video_bitcoder/venv/bin/python'
WORK = os.path.join(HERE, 'wraptest')
os.makedirs(WORK, exist_ok=True)

payload_mb = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
payload = os.path.join(WORK, 'wrap_payload.bin')
video = os.path.join(WORK, 'wrap.mp4')

if not os.path.exists(payload) or os.path.getsize(payload) != int(payload_mb * 1024 * 1024):
    print(f'creating {payload_mb}MB random payload ...')
    with open(payload, 'wb') as f:
        left = int(payload_mb * 1024 * 1024)
        while left > 0:
            chunk = os.urandom(min(1 << 20, left))
            f.write(chunk)
            left -= len(chunk)

def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

src_sha = sha256(payload)
print(f'payload sha256: {src_sha}')

# 105000 data groups -> crosses 65536 AND 131072 (with k=127 parity:
# 105000 + 825 parity = 105825 stream groups; wrap #2 is at 131072...
# wait, 105825 < 131072. To cross TWICE we need >=131072 stream groups:
# 105000 data is only ONE crossing. Recompute: 132000 data groups
# (132000*306 = 40.4MB) + 1039 parity = 133039 -> crosses both.
B = 306
n_data = os.path.getsize(payload) // B
total = n_data + (n_data + 126) // 127
print(f'data groups: {n_data}, total stream groups: {total}, '
      f'wraps: {[w for w in (65536, 131072) if w < total]}')
if total < 131072:
    print('WARNING: payload too small to cross the second boundary; '
          're-run with a larger payload_mb')

t0 = time.time()
r = subprocess.run([PY, 'VC7030_color.py', 'encode', payload,
                    '16', '2', '640', '360', '8', '--color',
                    '--fec-k', '127', '--fec-m', '2', '--preset', 'medium',
                    '--out', video], cwd=HERE, capture_output=True, text=True)
enc_s = time.time() - t0
tail = '\n'.join(r.stdout.splitlines()[-3:])
print(f'ENCODE {enc_s:.0f}s rc={r.returncode}\n{tail}')
if r.returncode != 0:
    sys.exit(1)

r = subprocess.run([PY, 'VC7030_color.py', 'decode', video, '8'],
                   cwd=HERE, capture_output=True, text=True)
dec_s = time.time() - t0 - enc_s
tail = '\n'.join(r.stdout.splitlines()[-4:])
print(f'DECODE {dec_s:.0f}s rc={r.returncode}\n{tail}')
if r.returncode != 0:
    sys.exit(1)

# The decoder names output after the embedded filename.
import glob
recon = glob.glob(os.path.join(HERE, 'reconstructed', '*'))
assert len(recon) == 1, f'expected one reconstructed file, got {recon}'
dst = recon[0]
dst_sha = sha256(dst)
print(f'decoded sha256: {dst_sha}')
print('BYTE-EXACT:', dst_sha == src_sha)
sys.exit(0 if dst_sha == src_sha else 1)
