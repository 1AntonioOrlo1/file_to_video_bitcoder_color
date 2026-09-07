# file_to_video_bitcoder 🎥 bit-level data embedding in video

A fast and efficient tool for encoding files into video frames. This project is an AI-assisted analog of [fvid](https://github.com/AlfredoSequeida/fvid), designed for high-speed steganography using bit-level manipulations in video pixels.

## ✨ Features

- **Bit-level Embedding:** Encodes file data into blocks of $M \times M$ pixels across the video frames.
- **Self-Documenting:** Embeds essential metadata (filename, size, parameters) in JSON into the first $R$ frames of the video for automatic decoding.
- **Multithreaded:** Optimized for performance using Python's `multiprocessing` (separate frame-worker processes).
- **Hardware Accelerated:** A specialized NVIDIA **NVENC** variant (`VC7030_nvenc_final_fixed.py`) is available for maximum encoding speed.
- **Resilient:** Uses redundant frame copies ($R$, default 30) and block averaging to survive lossy H.264 compression.
- **Stall-safe decoding:** the decoder loop has a watchdog that aborts with a clear error instead of hanging when the frame stream ends unexpectedly; incomplete reconstructions are removed on failure.
- **No resampling:** the decoder reads frames at the video's native size, exactly as encoded. If a host re-encoded the video at a different resolution, the decoder reports a clear error instead of guessing — decode the original to recover the file.
- **Color mode (`--color`):** besides the fast grayscale superset, the fork can embed truecolor data using an 8-corner RGB palette (black, red, green, blue, magenta, cyan, yellow, white) — 3 bits per $M \times M$ block, one independent threshold per channel. `M` must be even. The mode is self-describing: the decoder detects it from the embedded metadata.

## 🚀 Quick Start

### Prerequisites

1. **FFmpeg:** must be installed and available in your `PATH` (CPU build uses libx264; the NVENC build requires the `h264_nvenc` encoder).
2. **Python 3.10+**

### Installation

```bash
git clone <repo_url>
cd file_to_video_bitcoder
python -m venv venv
./venv/bin/pip install -r requirements.txt
```

### Usage

#### Encoding a file (CPU / libx264)

```bash
./venv/bin/python VC7030_color.py encode <input_file> <M> <R> <width> <height> <processes> [--crf N] [--color]
```

Example (5 KB file, 16x16 blocks, 30x redundancy, 640x480, 4 workers):

```bash
./venv/bin/python VC7030_color.py encode tiny_input.bin 16 30 640 480 4 --crf 23
```

Color mode (8-corner RGB palette, 3 bits/block, even `M` required):

```bash
./venv/bin/python VC7030_color.py encode tiny_input.bin 16 30 640 480 4 --crf 23 --color
```

The encoded video is written to `encoded/encoded_video.mp4` relative to the working directory.

#### Encoding a file (NVIDIA NVENC)

The NVENC variant uses the same CLI as the CPU version:

```bash
./venv/bin/python VC7030_nvenc_final_fixed.py encode <input_file> <M> <R> <width> <height> <processes> [--crf N]
./venv/bin/python VC7030_nvenc_final_fixed.py decode <video_path> <processes>
```

Without `--crf` it uses the round-trip-verified CBR 23M mode; with `--crf N` it
uses NVENC VBR constant quality (`-cq N`). Verified on a real NVIDIA GPU with
`h264_nvenc` (H.264 High): both modes return a byte-identical file after
decode, and the NVENC and CPU variants are mutually compatible (they decode
each other's output).

#### Decoding

```bash
./venv/bin/python VC7030_color.py decode <video_path> <processes>
```

```bash
./venv/bin/python VC7030_color.py decode encoded/encoded_video.mp4 4
```

Parameters are read from the embedded metadata, so no extra arguments are needed. The reconstructed file is written to `reconstructed/<original_filename>` relative to the working directory.

## 🛠 Technical Details

- **Metadata Redundancy:** the first $R$ frames (default $R = 30$) all carry the full JSON metadata block; decoding averages the copies before thresholding.
- **Block Size ($M$):** each $M \times M$ pixel block stores one bit (bright = 1, dark = 0); $R$ copies of each data frame are written back-to-back.
- **CRF:** use a low CRF / high bitrate when re-encoding. Measured resilience at $R = 30$: **CRF up to at least 63 (the practical CRF ceiling) decodes with a perfect SHA-256 match**, verified at 640x480 and the target 1280x720 up to 54 (see `TEST_REPORT.md`).
- **Bit-rate & pixel format:** frames are encoded as `yuv420p` at 30 fps with libx264 (`-preset medium`) by default; NVENC build uses `h264_nvenc`.

## 🧪 Resilience Testing

```bash
./venv/bin/python end_to_end_stress_test.py
```

Runs the full pipeline (encode at CRF 23 -> transcode at CRF 26..38 -> decode -> SHA-256 compare) with a per-step timeout. The `run_stress_test` helper accepts an arbitrary CRF range for extended sweeps. Results and conclusions are kept in `TEST_REPORT.md`.

## 📄 License

This project is released under the MIT License.
