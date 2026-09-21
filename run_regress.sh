#!/bin/bash
# Byte-exact regression suite for VC7030_color.py.
#
# Self-contained: generates two synthetic payloads (2 MB and 1 MB) and runs
# every encode/decode path, checking the output is byte-identical to the
# input. The two legacy no-FEC scenarios (D, G) are MANDATORY to keep —
# they are the paths that hid the `success` UnboundLocalError (commit
# 06d34a2) for years because every working video used FEC.
#
# Usage:  PY=python3 bash run_regress.sh
#         (python needs numpy + PIL; default = the venv from the main repo)
set -u
PY="${PY:-/home/mmn/file_to_video_bitcoder/venv/bin/python}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d /tmp/bitcoder_regress.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
cd "$ROOT"

P2="$WORK/payload_2mb.bin"
P1="$WORK/payload_1mb.bin"
python3 -c "
import os, sys
for p, n in (('$P2', 2*1024*1024), ('$P1', 1*1024*1024)):
    with open(p, 'wb') as f:
        while n > 0:
            c = os.urandom(min(n, 1 << 20))
            f.write(c); n -= len(c)
print('payloads ready')
" || exit 1

PASS=0; FAIL=0
run() { # name, reference-file, encode-args...
  local name="$1" ref="$2"; shift 2
  local out="$WORK/$name.mp4"
  rm -f reconstructed/*
  if ! "$PY" VC7030_color.py encode "$@" --out "$out" > "$WORK/$name.enc.log" 2>&1; then
    echo "FAIL $name (encode died)"; tail -3 "$WORK/$name.enc.log"; FAIL=$((FAIL+1)); return
  fi
  if ! "$PY" VC7030_color.py decode "$out" 8 > "$WORK/$name.dec.log" 2>&1; then
    echo "FAIL $name (decode died)"; tail -3 "$WORK/$name.dec.log"; FAIL=$((FAIL+1)); return
  fi
  if cmp -s "reconstructed/$(basename "$ref")" "$ref"; then
    echo "PASS $name"; PASS=$((PASS+1))
  else
    echo "FAIL $name (bytes differ)"; FAIL=$((FAIL+1))
  fi
}

# A: 1080p color auto (R=2 k=127 m=2), no tail — the standard upload profile
run a_1080p_auto      "$P2" "$P2" 0 0 1920 1080 8 --auto --color --crf 23
# B: same + reinforced tail (2 stripes)
run b_1080p_auto_tail "$P2" "$P2" 0 0 1920 1080 8 --auto --color --crf 23 --tail-m 5
# C: single-stripe file (1 MB < k data groups at 1080p) + tail — the
#    wrong-matrix bug (06d34a2)
run c_single_stripe   "$P1" "$P1" 0 0 1920 1080 8 --auto --color --crf 23 --tail-m 5
# D: GRAY legacy, NO FEC — mandatory (UnboundLocalError path)
run d_gray_legacy     "$P2" "$P2" 16 4 1920 1080 8 --crf 23
# E: 60 fps R=2 + reinforced tail — the verified YouTube 60fps recipe
run e_60fps_tail15    "$P2" "$P2" 0 0 1920 1080 8 --auto --color --crf 23 --fps 60 --tail-m 15
# F: 4K max-dense single stripe + tail
run f_4k_maxdense     "$P2" "$P2" 0 0 3840 2160 8 --auto --max-dense --color --crf 23 --tail-m 5
# G: COLOR legacy, NO FEC, smaller canvas — mandatory (UnboundLocalError path)
run g_color_legacy    "$P1" "$P1" 16 4 1280 720 8 --crf 23 --color

echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
