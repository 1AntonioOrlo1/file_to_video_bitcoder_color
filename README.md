# bitcoder — hide any file inside an MP4 video (video steganography with error correction)

**bitcoder** is a Python + FFmpeg tool for **video steganography**: it embeds an
arbitrary file (a PNG, a zip, a video, a database — anything, up to gigabytes)
into a standard, playable **H.264/MP4** video, and extracts it back
**byte-exact** (SHA-256 verified). Unlike most steganography tools it survives
lossy compression and can **repair lost or corrupted segments** with forward
error correction (FEC) — no retransmission, no password exchange, no container
metadata tricks.

A fork of [1AntonioOrlo1/file_to_video_bitcoder](https://github.com/1AntonioOrlo1/file_to_video_bitcoder),
itself an analog of [fvid](https://github.com/AlfredoSequeida/fvid). This fork
adds **truecolor mode** (8-corner RGB palette, ~3× capacity), a full
**color-FEC path**, DCT-aligned density profiles, and a measured
maximum-density pipeline.

## How it compares to other video steganography tools

| Tool | Where the data lives | Capacity | Survives re-encode | Self-repair (FEC) |
|---|---|---|---|---|
| [fvid](https://github.com/AlfredoSequeida/fvid) | 1-bit pixel frames (this project's ancestor) | 1 bit/px, no FEC | partially — threshold decoding survives mild re-compression, but any lost frame is unrecoverable | no |
| [OpenPuff](https://en.wikipedia.org/wiki/OpenPuff) | MP4 *container* (null-space of metadata) | MB-scale | no — any re-mux/re-encode destroys it | no |
| [videostego](https://github.com/JavDomGom/videostego) | MP4 container bits | KB–MB | no | no |
| [TwoPixels](https://github.com/anandbaburajan/TwoPixels), [Video-Steganography (LSB)](https://github.com/itxKAE/Video-Steganography) | pixel LSB of real footage | low (imperceptibility-first) | no — CRF 23 already flips LSBs | no |
| [video-in-video](https://github.com/Amritaryal44/Video-Steganography), [StegoVideoDemo](https://github.com/mightymoogle/StegoVideoDemo) | raw pixel overwrite | high | no — lossy compression corrupts it | no (watermarking focus) |
| **bitcoder (this)** | **pixel blocks of a noise video** | **~⅓ of the video's bytes are payload** (2 MB → ~6 MB) | **yes — CRF-23 H.264 roundtrip, byte-exact** | **yes — MDS GF(256), repairs ≤m groups per stripe** |

Container-level tools (OpenPuff, videostego) hide bytes in the MP4 file
structure — clever, but a single re-mux or platform re-encode destroys the
payload, and published research (e.g. *"Steganalysis of OpenPuff through atomic
concatenation of MP4 flags"*) already describes how to detect them.
Imperceptibility-first pixel-LSB tools keep real footage looking natural, but
their payload is a few kilobytes and any lossy re-encode flips it. bitcoder
trades naturalness for **capacity + robustness**: the cover video is TV
static, but it carries hundreds of times more data, round-trips through
platform compression **bit-perfect**, and can even heal cut/lost segments.
Use it when you need to *move* data, not when the footage must stay
recognizable.

## ✨ Features

- **Bit-level embedding** — file data is stored in $M \times M$ pixel blocks
  across video frames; the video looks like ordinary static and plays anywhere.
- **Byte-exact roundtrip** — decode reproduces the original file bit-for-bit
  (SHA-256 verified end-to-end), measured on files from KB to GB.
- **Color mode (`--color`)** — an 8-corner RGB palette (black, red, green,
  blue, magenta, cyan, yellow, white) stores **3 bits per block** — ~3× the
  capacity of grayscale at the same size. Grayscale stays a strict superset:
  without `--color` the output is byte-identical to upstream.
- **FEC (`--fec-k K --fec-m M`)** — systematic MDS erasure coding
  (Cauchy matrix over GF(256)) over whole frame groups: any `k` of the
  `k + m` groups in a stripe suffice, so **up to `m` groups may be lost,
  cut, or corrupted and are still recovered**. Works in both gray and color
  modes; group headers (magic + sequence + CRC-32) are drawn
  chroma-neutral so compression can never fake or break them.
- **`--auto` density profiles** — pass `0 0 --auto` and the tool picks the
  measured-best recipe (M, R, k, m, x264 preset) for your geometry.
  `--auto --max-dense` picks the absolute-densest one.
- **Self-documenting** — JSON metadata (filename, size, all parameters,
  payload hash) is redundantly embedded in the first frames; decoding needs
  no arguments, and the embedded hash proves the reconstruction.
- **Multithreaded** — frame workers on encode; a parallel FEC group walker
  with sliding decode window, sequence resync, and a stall watchdog.
- **Memory-bounded** — a 500 MB @ 1080p encode+decode held a flat ~2.2 GB RSS
  (shared-memory frame pool, one file handle per worker); no leak.

## 🚀 Quick Start

### Prerequisites

1. **FFmpeg** in `PATH` (libx264 + `ffprobe`).
2. **Python 3.10+**

### Installation

```bash
git clone https://github.com/1AntonioOrlo1/file_to_video_bitcoder_color
cd file_to_video_bitcoder_color
python -m venv venv
./venv/bin/pip install -r requirements.txt
```

### Recommended recipes (1080p / 4K)

The measured-best recipe for both geometries is the same: **color mode,
M=8, k=127/m=2 FEC, veryslow preset** — `--auto` picks exactly that.
`--auto` (R=2) is the default: ~24% larger than the absolute-densest
variant but keeps copy-averaging for platform re-encode robustness.
`--auto --max-dense` (R=1) is the smallest video and still repairs up to
2 whole-group losses per stripe — use it for files that will not be
re-encoded again (local HDD → same machine).

**1080p (1920×1080)** — ~6 MB of video per 2 MB of payload:

```bash
# balanced (default): ~6 MB out per 2 MB in
./venv/bin/python VC7030_color.py encode my_file.bin 0 0 1920 1080 8 --color --auto
# densest: ~5.6 MB out per 2 MB in
./venv/bin/python VC7030_color.py encode my_file.bin 0 0 1920 1080 8 --color --auto --max-dense
```

**4K (3840×2160)** — same recipe, same density (stream volume is
`~filesize·R·M²`, resolution-independent); the bigger canvas just fits
more blocks per frame:

```bash
./venv/bin/python VC7030_color.py encode my_file.bin 0 0 3840 2160 8 --color --auto
./venv/bin/python VC7030_color.py encode my_file.bin 0 0 3840 2160 8 --color --auto --max-dense
```

Decode is always the same two commands — parameters come from the
embedded metadata, no flags needed:

```bash
./venv/bin/python VC7030_color.py decode encoded/encoded_video.mp4 16
# result: reconstructed/<original_filename>
```

Notes:

- **PROCESSES** = worker count (8 is the tested default; match your core
  count).
- **4K memory:** the frame pool is resolution-dependent — a multi-GB
  4K encode peaks at several GB of RAM. On a tight machine (or next to
  a GPU LLM server), stop the other consumer or use 1080p; the payload
  size of the output is the same either way.
- **Large files:** 1080p is the sweet spot for multi-GB payloads — a
  6.94 GB file encoded in ~3 h and decoded byte-exact (verified).
  `--max-dense` halves the encode time vs `--auto` (R=1 vs R=2).

### CLI

```
encode FILE M R WIDTH HEIGHT PROCESSES [--crf N] [--out PATH]
       [--preset NAME] [--fec-k K] [--fec-m M] [--color] [--auto]
       [--max-dense]
decode VIDEO PROCESSES
```

| Argument | Meaning |
|---|---|
| `M` | block size, $M \times M$ pixels (must be even for `--color`; `0` = let `--auto` pick) |
| `R` | redundancy: each data frame is written $R$ times back-to-back (`0` = let `--auto` pick) |
| `WIDTH HEIGHT` | video geometry (even values) |
| `PROCESSES` | worker count |
| `--crf N` | x264 quality (default 23) |
| `--preset NAME` | x264 preset `ultrafast`…`veryslow`. Default: from the `--auto` profile (`veryslow` for color), or `medium` for explicit M/R encodes |
| `--out PATH` | output video (default `encoded/encoded_video.mp4`) |
| `--fec-k K --fec-m M` | FEC stripe: `K` data + `M` parity groups (omit = no FEC; GF(256) cap `2k+m-2 ≤ 255`) |
| `--color` | color mode (default: grayscale) |
| `--auto` | pick `M`, `R`, `k`/`m` and the x264 preset from the built-in density profile for the geometry — pass `0 0` and `--auto` (default = M=8 R=2, k=127, veryslow — ~6 MB for 2 MB) |
| `--max-dense` | with `--auto`: the absolute-densest profile (M=8 R=1, k=127, veryslow, ~5.6 MB for 2 MB) — thinnest protection, best for whole-group drops/cuts |

### Examples

The recommended one-liners (`--auto` picks everything):

```bash
# densest balanced (M=8 R=2, k=127, veryslow, ~6 MB for 2 MB):
./venv/bin/python VC7030_color.py encode input.bin 0 0 1920 1080 8 --color --auto
# absolute-densest (M=8 R=1, k=127, veryslow, ~5.6 MB for 2 MB):
./venv/bin/python VC7030_color.py encode input.bin 0 0 1920 1080 8 --color --auto --max-dense
```

Explicit recipes:

```bash
# Grayscale:
./venv/bin/python VC7030_color.py encode tiny_input.bin 16 30 640 480 4 --crf 23
# Color (3 bits/block):
./venv/bin/python VC7030_color.py encode tiny_input.bin 16 30 640 480 4 --crf 23 --color
# Error-correcting color, 720p, 16 workers, tolerates 4 lost groups per stripe:
./venv/bin/python VC7030_color.py encode big_input.bin 8 4 1280 720 16 \
    --crf 23 --color --fec-k 8 --fec-m 4
```

Decode (parameters come from the embedded metadata — no flags needed):

```bash
./venv/bin/python VC7030_color.py decode encoded/encoded_video.mp4 16
```

The reconstructed file is written to `reconstructed/<original_filename>`
relative to the working directory.

## 🧩 How It Works

**Stream layout.** The video starts with 30 identical metadata frames (each
carrying the full JSON block; the decoder averages the copies before
thresholding), then the data: consecutive frames are grouped, each group is
written $R$ times back-to-back.

- *Grayscale:* 1 bit per block (bright = 1, dark = 0).
- *Color:* 3 bits per block, interleaved per block as `(R, G, B)`; each
  channel is thresholded at 128.
- *FEC:* every group (data or parity) carries an 8-byte header — magic,
  group sequence number, CRC-32 of the payload, spare — plus its payload.
  Parity groups are GF(256) Cauchy combinations of the stripe's data groups.
  The decoder walks the stream in parallel, matches groups by sequence
  number, repairs each stripe from any `k` survivors, and verifies the
  whole payload against the embedded hash.

**Geometry floor for FEC.** FEC metadata is larger than plain metadata (it
carries the stream hash). The metadata canvas now adapts: `meta_block_size()`
shrinks the meta block from 16 down to 4 (recorded in the JSON as `mb`)
until it fits, so the old 720p+ minimum is gone — 640×360 with FEC works.
The decoder probes the same candidate sizes and re-reads at the declared `mb`.

## 🎚️ Density profiles (color + FEC, typical YouTube sizes)

`run_profiles.py` sweeps (geometry × M × R × m) on a real 2 MB PNG at CRF 23,
recording mp4 size, byte-exactness, and the **minimum threshold margin** the
decoder reports. `run_ksweep.py` sweeps the **stripe length k** (the last
density lever): parity overhead is `m·ceil(n_data/k)/n_data`, so k=8 = 25.6%
of groups, k=64 = 3.7%, **k=127 = 2.4%** — the GF(256) Cauchy cap for m=2
(`2k+m-2 ≤ 255`). The winner is **M=8 at every geometry**, k=127, veryslow:

| Geometry | M | R | k/m | preset | size (2 MB file) |
|---|---|---|---|---|---|
| 1280×720 | 8 | 2 | 127/2 | veryslow | ~5–6 MB |
| 1920×1080 | 8 | 2 | 127/2 | veryslow | ~6 MB |
| 3840×2160 | 8 | 2 | 127/2 | veryslow | ~6 MB |

One long stripe (k=127) covers almost the whole file, so losses in
*different places* are repaired, not just adjacent pairs — and m=2 costs
only ~1.3% over m=1 at this k, so the double protection is nearly free.
Slow preset: the flat DCT-aligned 0/255 blocks compress ~2× denser than
medium (color R=1: 12 MB medium → 6 MB veryslow).

**Maximum-density option (`--auto --max-dense`):** M=8 **R=1** k=127 m=2,
veryslow → ~5.6 MB (1080p), byte-exact, and it repairs real group cuts
(1 and 2 lost groups → repaired, 10/10 as expected incl. the short last
stripe). The trick: because M=8 is DCT-aligned, the threshold margin stays
~73–78% **even with a single copy** — compression flips no bits, so any
damage (a whole-group loss *or* a bit-flipped frame) shows up as a failed
group CRC and is repaired as an erasure, as long as ≤ m per stripe. That is
the densest point where the FEC actually earns its keep.

**Why the size is resolution-independent:** the stream volume is
`~filesize · R · M²`, not tied to W×H. A bigger canvas just fits more blocks
per frame; the number of *groups* needed to carry the file is set by the
bytes-per-group, which scales with `M²`.

**Why M=8 and not smaller:** its block edges land on x264's 8×8 DCT grid, so
a solid white 0/255 block encodes in ~1 bit. A misaligned M (6 / 10 / 12)
smears the hard edge across DCT blocks and costs **4–6× in bitrate** —
1080p, R=4, m=2: M=4→23 MB, M=6→71, **M=8→17.9**, M=10→108, M=12→73.5,
M=16→65.6, M=24→90.9 MB. M=8 is the smallest aligned size and keeps the
margin high, so compression flips no bits and the `m` parity groups are
pure whole-group-erasure insurance. `--auto` applies the table; the trade
knobs are `m` (size for erasure tolerance) and `R` (size for margin).

**Why 8 colors and not more:** multi-level palettes (16/64-color) put their
decision thresholds at the midpoint between levels, and 4:2:0 chroma
subsampling averages block-boundary pixels *exactly onto* those thresholds —
16-color fails byte-exact even at CRF 0 (`proto_16color.py` documents the
experiment). The binary 8-corner palette's single per-channel threshold (128)
is maximally distant from both levels; it is the only palette that survives.

## 📊 Benchmarks

Verified end-to-end (encode → decode → SHA-256 byte-exact), 8 workers,
x264, CRF 23:

| Source | Geometry | Mode | Result |
|---|---|---|---|
| 2 MB | 720p / 1080p / 4K | color + FEC, `--auto` (M=8 R=2 k=127 m=2, veryslow) | **~6 MB**, byte-exact |
| 2 MB | 1080p | color + FEC, `--auto --max-dense` (R=1) | **~5.6 MB**, byte-exact |
| 2 MB | 1080p | color + FEC, `--auto --max-dense`, loss test | 1–2 group cuts repaired, 3 fails (10/10 as expected) |
| 500 MB | 1080p | color + FEC (R=1, medium) | enc 993 s / dec 278 s, byte-exact, **flat ~2.2 GB RSS** (no leak) |
| 1 GiB | 3840×2160 | gray | enc 976 s / dec 373 s |
| 1 GiB | 3840×2160 | color | enc 509 s / dec 529 s |
| 1 GiB | 3840×2160 | color + FEC (k8/m4, 8289 groups) | enc 923 s / dec 477 s (~13.5 GB mp4) |

FEC erasure testing (1080p, color, k=127/m=2): cutting 1 whole group →
repaired; 2 (the maximum) → repaired; 3 from one stripe → correctly fails.
Same verdict at k=8/m=2. The short last stripe (k_s < k) also repairs.

**Memory note:** the frame pool lives in `/dev/shm` and is shared by all
workers (only slot indices cross the IPC boundary); a 500 MB 1080p run held
~2.2 GB RSS end-to-end. A 1 GiB **4K** run is the heavy case (rgb24 reader +
pool): expect several GB; on a tight machine, lower the geometry or add swap.

## 🧪 Verification

The fork is exercised by an external mega-test matrix (16 threads) covering:
$M \times R \times$ mode at 720p; a CRF 18–30 sweep; resolutions 360p→4K;
edge cases (empty file, exact-frame boundaries, odd $M$ rejected, odd
dimensions rejected, $M \in \{4,6,8,12,16\}$, process-count determinism);
FEC cross-compatibility with the upstream program; rescaled-video clean
failure; and the 1 GiB 4K gray/color/FEC finals. All cells round-trip
byte-exact. Helper harnesses in this repo: `run_profiles.py` (density),
`run_ksweep.py` (stripe length), `run_sweep.py` (R/m), `run_loss.py`
(FEC erasure injection), `run_memtest.py` (big-file RSS sampling),
`proto_16color.py` (palette experiment).

## 📄 License

This project is released under the MIT License.
