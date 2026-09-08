# file_to_video_bitcoder (color fork) 🎥 bit-level data embedding in video

A fast tool for embedding files into H.264 video frames. A fork of
[1AntonioOrlo1/file_to_video_bitcoder](https://github.com/1AntonioOrlo1/file_to_video_bitcoder),
an AI-assisted analog of [fvid](https://github.com/AlfredoSequeida/fvid),
designed for high-speed steganography using bit-level manipulations in video
pixels.

This fork adds **truecolor mode** (8-corner RGB palette) and **systematic MDS
forward error correction** (including a full color-FEC path), on top of the
upstream grayscale + FEC core.

## ✨ Features

- **Bit-level embedding** — file data is stored in $M \times M$ pixel blocks
  across video frames; the video looks like ordinary static.
- **Self-documenting** — JSON metadata (filename, size, all parameters) is
  redundantly embedded in the first frames, so decoding needs no arguments.
- **Color mode (`--color`)** — an 8-corner RGB palette (black, red, green,
  blue, magenta, cyan, yellow, white) stores **3 bits per block**, one
  independent threshold per channel. Grayscale stays a strict superset:
  without `--color` the output and bitstream are byte-identical to upstream.
  `M` must be even (yuv420p chroma alignment).
- **FEC (`--fec-k`, `--fec-m`)** — systematic MDS erasure coding
  (Cauchy matrix over GF(256)) over whole frame groups: any `k` of the
  `k + m` groups in a stripe suffice, so up to `m` groups may be lost.
  Works in **both** grayscale and color modes — in color the 8-byte group
  header is drawn chroma-neutral (black/white on all three channels) in the
  first 64 blocks, the payload follows at 3 bits/block.
- **Multithreaded** — frame workers on encode, a 16-thread parallel FEC group
  walker with a sliding decode window and resync on decode.
- **Resilient** — redundant frame copies ($R$) and block averaging survive
  lossy H.264 compression (upstream measured perfect round-trips up to CRF 63
  at $R = 30$).
- **Stall-safe decoding** — a watchdog aborts with a clear error instead of
  hanging when the frame stream ends unexpectedly; incomplete reconstructions
  are removed on failure.
- **No resampling** — the decoder reads frames at the video's native size,
  exactly as encoded. If a host re-encoded the video at a different
  resolution, the decoder reports a clear geometry error instead of guessing.

## 🚀 Quick Start

### Prerequisites

1. **FFmpeg** in `PATH` (libx264 encoder + `ffprobe`).
2. **Python 3.10+**

### Installation

```bash
git clone https://github.com/1AntonioOrlo1/file_to_video_bitcoder_color
cd file_to_video_bitcoder_color
python -m venv venv
./venv/bin/pip install -r requirements.txt
```

### CLI

```
encode FILE M R WIDTH HEIGHT PROCESSES [--crf N] [--out PATH]
       [--preset NAME] [--fec-k K] [--fec-m M] [--color]
decode VIDEO PROCESSES
```

| Argument | Meaning |
|---|---|
| `M` | block size, $M \times M$ pixels (must be even for `--color`) |
| `R` | redundancy: each data frame is written $R$ times back-to-back |
| `WIDTH HEIGHT` | video geometry (even values) |
| `PROCESSES` | worker count (16 is a good default) |
| `--crf N` | x264 quality (default 23) |
| `--preset NAME` | x264 preset `ultrafast`…`veryslow` (default `medium`) |
| `--out PATH` | output video (default `encoded/encoded_video.mp4`) |
| `--fec-k K --fec-m M` | FEC stripe: `K` data + `M` parity groups (omit = no FEC) |
| `--color` | color mode (default: grayscale) |

### Examples

Grayscale:

```bash
./venv/bin/python VC7030_color.py encode tiny_input.bin 16 30 640 480 4 --crf 23
```

Color (3 bits/block — about 3x more capacity, smaller/faster video):

```bash
./venv/bin/python VC7030_color.py encode tiny_input.bin 16 30 640 480 4 --crf 23 --color
```

Error-correcting color, 720p, 16 workers, tolerates 4 lost groups per stripe
of 8:

```bash
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
carries the stream hash). Very small canvases cannot hold it — e.g. 640x360
with `M = 16` gives only 880 blocks vs ~1184 bits needed; the encoder raises
a clear hint. 720p and up is comfortable.

## 📊 Benchmarks

Verified end-to-end (encode → decode → SHA-256 byte-exact), 16 workers,
x264, CRF 23:

| Source | Geometry | Mode | Encode | Decode |
|---|---|---|---|---|
| 16 MiB | 640x360 – 3840x2160 | gray, color | seconds | seconds |
| 16 MiB | 1280x720 – 1920x1080 | color + FEC (k4/m2, k8/m4) | 14–67 s | 13–43 s |
| 1 GiB | 3840x2160 | gray | 976 s | 373 s |
| 1 GiB | 3840x2160 | color | 509 s | 529 s |
| 1 GiB | 3840x2160 | color + FEC (k8/m4, 8289 groups) | 923 s | 477 s |

FEC erasure testing (720p, color, k4/m2): cutting 1 whole group → repaired;
cutting 2 (the maximum) → repaired; cutting 3 from one stripe → correctly
fails.

**Memory note:** a 1 GiB 4K encode peaks at ~5–9 GB of RAM (rgb24 reader +
frame pool). On a machine without headroom, expect the OS to kill or stall it.

## 🧪 Verification

The fork is exercised by an external mega-test matrix (16 threads) covering:
$M \times R \times$ mode at 720p; a CRF 18–30 sweep; resolutions 360p→4K;
edge cases (empty file, exact-frame boundaries, odd $M$ rejected, odd
dimensions rejected, $M \in \{4,6,8,12,16\}$, process-count determinism);
FEC cross-compatibility with the upstream program; rescaled-video clean
failure; and the 1 GiB 4K gray/color/FEC finals. All cells round-trip
byte-exact (the only excluded cell: 360p + FEC, below the geometry floor).

## 📄 License

This project is released under the MIT License.
