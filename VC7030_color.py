import os
import json
import multiprocessing
import argparse
import numpy as np
from PIL import Image
import logging
import time
import subprocess
from queue import Empty, Queue
from multiprocessing.shared_memory import SharedMemory
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor

# FEC: systematic MDS erasure code over GF(256) — recovers up to m lost
# frame groups per stripe of k (any k of the k+m stripe groups suffice).
from bitcoder_fec import (HEADER_BYTES, HEADER_BITS, FecError, cauchy_matrix,
                          fec_decode, fec_encode, hash64_file, header_crc,
                          pack_header, render_payload, stripe_plan,
                          unpack_header)

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Constants
M_META = 16
R_META = 30  # Number of metadata copies

def get_output_directory():
    directory = os.path.join(os.getcwd(), 'encoded')
    os.makedirs(directory, exist_ok=True)
    return directory

def get_reconstructed_directory():
    directory = os.path.join(os.getcwd(), 'reconstructed')
    os.makedirs(directory, exist_ok=True)
    return directory

def create_meta_image(meta_data, width, height):
    try:
        # Minimal metadata
        minimal_meta = {
            'fn': os.path.basename(meta_data['filename']),
            'fs': meta_data['file_size'],
            'M': meta_data['M'],
            'R': meta_data['R'],
            'w': width,
            'h': height,
            'tb': meta_data['total_bits']
        }
        # FEC stream parameters (only present when the stream was FEC-coded).
        # The decoder keys off meta['fec']; without these it falls back to the
        # legacy in-order path and silently mis-decodes a FEC video.
        for key in ('fec', 'k', 'm', 'pd', 'B', 'gh'):
            if key in meta_data:
                minimal_meta[key] = meta_data[key]

        meta_json = json.dumps(minimal_meta, separators=(',', ':'))
        meta_bytes = meta_json.encode('utf-8')
        total_meta_bits = len(meta_bytes) * 8

        # Calculate only whole blocks
        blocks_x = width // M_META
        blocks_y = height // M_META
        total_blocks = blocks_x * blocks_y

        if total_blocks < total_meta_bits:
            raise ValueError(f"Metadata does not fit. Required: {total_meta_bits} bits, available: {total_blocks} blocks")

        # Create an array of metadata bits
        bit_array = np.zeros(total_blocks, dtype=bool)
        for i in range(total_meta_bits):
            byte_idx = i // 8
            bit_in_byte = i % 8
            if byte_idx < len(meta_bytes):
                byte_val = meta_bytes[byte_idx]
                bit_array[i] = (byte_val >> (7 - bit_in_byte)) & 1

        # Convert to 2D block matrix
        bit_matrix = bit_array[:blocks_x*blocks_y].reshape(blocks_y, blocks_x)

        # Expand each bit to an M_META x M_META block
        expanded = bit_matrix.repeat(M_META, axis=0).repeat(M_META, axis=1)

        # Create full image
        full_image = np.zeros((height, width), dtype=bool)
        full_image[:blocks_y*M_META, :blocks_x*M_META] = expanded

        # Convert to PIL image
        img = Image.fromarray(full_image)
        return img.convert('1')  # Convert to 1-bit format

    except Exception as e:
        logging.error(f"Error creating meta-image: {str(e)}")
        raise

def generate_data_frame(frame_idx, file_path, M, width, height, bits_per_frame, total_bits):
    try:
        start_bit = frame_idx * bits_per_frame
        end_bit = min(start_bit + bits_per_frame, total_bits)
        num_bits = end_bit - start_bit

        # Calculate only whole blocks
        blocks_x = width // M
        blocks_y = height // M

        # Read only the necessary part of the file
        start_byte = start_bit // 8
        end_byte = (end_bit + 7) // 8
        byte_count = end_byte - start_byte

        with open(file_path, 'rb') as f:
            f.seek(start_byte)
            chunk = f.read(byte_count)

        # Vectorized bit unpack (big-endian, same bit order as the old loop).
        bits = np.unpackbits(np.frombuffer(chunk, dtype=np.uint8), bitorder='big')
        real = bits[start_bit % 8:]
        bit_array = np.zeros(bits_per_frame, dtype=np.uint8)
        n_real = min(num_bits, real.size)
        bit_array[:n_real] = real[:n_real]

        # Convert to 2D block matrix. uint8 (not bool) so the x255 below stays
        # 8-bit instead of promoting to a 66 MB int64 temporary per frame.
        bit_matrix = bit_array.reshape(blocks_y, blocks_x)

        # Expand each bit to an M x M block
        expanded = bit_matrix.repeat(M, axis=0).repeat(M, axis=1)

        # Create full image (gray, 0/255). Return raw bytes directly so the
        # main process can stream them to ffmpeg without any serialized PIL
        # conversion in the hot loop.
        full_image = np.zeros((height, width), dtype=np.uint8)
        full_image[:blocks_y*M, :blocks_x*M] = expanded * 255
        return full_image.tobytes()

    except Exception as e:
        logging.error(f"Error generating data frame {frame_idx}: {str(e)}")
        return None

def _make_frame_pool(n_slots, frame_bytes):
    """Create a shared-memory pool of n_slots frames plus one free-slot
    semaphore per slot. Returns (name, shm, buffer, sems).

    The old design moved every generated/decoded frame (up to 8.3 MB at
    4K) through a multiprocessing.Manager queue, i.e. pickled over a
    socket on the hot path. Now the frame bytes live in /dev/shm and only
    tiny index pairs cross the IPC boundary.
    """
    name = f"bitcoder_{os.getpid()}_{time.monotonic_ns()}"
    shm = SharedMemory(name=name, create=True, size=n_slots * frame_bytes)
    buf = shm.buf
    if buf is None:
        shm.close()
        raise RuntimeError("shared-memory buffer unavailable")
    sems = [multiprocessing.Semaphore(1) for _ in range(n_slots)]
    return name, shm, buf, sems


def _close_pool(shm):
    if shm is None:
        return
    try:
        shm.close()
    except Exception:
        pass
    try:
        shm.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _read_group_payload(frame_idx, file_path, pd, group_bytes, total_bits):
    """Byte-aligned payload of data group `frame_idx`: exactly group_bytes
    bytes (the last group is zero-padded to a full byte count so that every
    row of a stripe has the same length — GF(256) operates on equal-length
    byte vectors)."""
    start_bit = frame_idx * pd  # pd is a multiple of 8, so this is byte-aligned
    end_bit = min(start_bit + pd, total_bits)
    n_bytes = max(0, (end_bit + 7) // 8 - start_bit // 8)
    with open(file_path, 'rb') as f:
        f.seek(start_bit // 8)
        chunk = f.read(n_bytes)
    return chunk.ljust(group_bytes, b'\x00')


def _fec_group_frame(frame_idx, file_path, M, width, height, pd,
                     group_bytes, k, P, df, total_bits):
    """Render stream group `frame_idx` for an FEC stream.

    Stream layout per stripe (size k + m): k data groups, then m parity
    groups. A group is a data group iff its within-stripe index < k.
    Data groups read (and byte-align) their payload from the file; parity
    groups recompute their stripe's data payloads and encode over GF(256).
    """
    m = P.shape[1]
    # The final stripe may be short (k_s < k): its stream stride is
    # k_s + m, so map stream index -> (stripe, within-stripe index) by
    # carving it off from the end.
    n_strips = (df + k - 1) // k
    k_last = df - (n_strips - 1) * k
    last_ss = df + n_strips * m - (k_last + m)
    if frame_idx >= last_ss:
        s, idx_in = n_strips - 1, frame_idx - last_ss
    else:
        s, idx_in = divmod(frame_idx, k + m)
    ds = s * k
    k_s = min(k, df - ds)  # data groups in this stripe (last may be short)
    if idx_in < k_s:
        payload = _read_group_payload(ds + idx_in, file_path, pd,
                                      group_bytes, total_bits)
    else:
        data = np.stack([np.frombuffer(_read_group_payload(ds + i, file_path,
                                                           pd, group_bytes,
                                                           total_bits),
                                       dtype=np.uint8)
                         for i in range(k_s)])
        # P is sized (k, m); a short last stripe only needs its first k_s rows
        parity = fec_encode(data, P[:k_s])
        payload = parity[idx_in - k_s].tobytes()
    try:
        # 8-byte group header (magic, seq, crc32(payload), spare) in the first
        # 64 blocks; the decode-side packer uses it to verify placement and
        # detect/resync exact-group-boundary cuts.
        header = pack_header(frame_idx, payload)
        return render_payload(payload, M, width, height, header=header)
    except ValueError as e:
        logging.error(f"FEC group {frame_idx} render failed: {e}")
        return None


def _shm_encode_worker(task_queue, out_queue, sems, pool_name, n_slots, frame_bytes,
                       file_path, M, width, height, bits_per_frame, total_bits,
                       P=None, k_fec=0, pd=0, group_bytes=0, df=0):
    """Encode worker: renders frame `frame_idx` straight into its pool slot
    (frame_idx % n_slots) and reports (frame_idx, slot) on the out queue.
    `slot == -1` signals a generation error. The semaphore guards the slot
    until the main process has read the previous occupant.

    With FEC (k_fec > 0): `frame_idx` counts stream groups (data groups
    first within each stripe, then m parity groups); the worker renders a
    data group or recomputes its parity group from the stripe's data
    payloads read back from the file."""
    shm = None
    try:
        shm = SharedMemory(name=pool_name)
        mv = memoryview(shm.buf)
        while True:
            try:
                frame_idx = task_queue.get(timeout=1)
            except Empty:
                continue
            if frame_idx is None:
                break
            if k_fec > 0:
                img = _fec_group_frame(frame_idx, file_path, M, width, height,
                                       pd, group_bytes, k_fec, P, df,
                                       total_bits)
            else:
                img = generate_data_frame(frame_idx, file_path, M, width, height,
                                          bits_per_frame, total_bits)
            if img is None:
                out_queue.put((frame_idx, -1))
                continue
            slot = frame_idx % n_slots
            sems[slot].acquire()
            mv[slot * frame_bytes:(slot + 1) * frame_bytes] = img
            out_queue.put((frame_idx, slot))
    except Exception as e:
        logging.error(f"Error in worker process: {str(e)}")
    finally:
        if shm is not None:
            try:
                shm.close()
            except Exception:
                pass

def encode_file_to_video(file_path, M, R, width, height, num_processes, crf=23,
                         out_path=None, fec_k=0, fec_m=0, preset='medium'):
    try:
        start_time = time.time()
        output_dir = get_output_directory()
        if out_path:
            output_video_path = out_path
        else:
            output_video_path = os.path.join(output_dir, "encoded_video.mp4")

        if not os.path.exists(file_path):
            logging.error(f"File not found: {file_path}")
            return False

        # Validate geometry up front: yuv420p needs even dimensions, and the
        # payload needs at least one M x M block. Failing here with a clear
        # message beats a cryptic ffmpeg pixel-format error mid-stream.
        if width % 2 or height % 2:
            logging.error(f"Width and height must be even for yuv420p (got {width}x{height})")
            return False

        file_size = os.path.getsize(file_path)
        filename = os.path.basename(file_path)

        # Calculate only whole blocks
        blocks_x = width // M
        blocks_y = height // M
        bits_per_frame = blocks_x * blocks_y

        if bits_per_frame == 0:
            logging.error(f"Need at least one {M}x{M} block; "
                          f"{width}x{height} is too small for M={M}")
            return False

        total_bits = file_size * 8

        # --- FEC setup ---
        # A group frame holds an 8-byte header in its first 64 blocks plus
        # B = (bits_per_frame - 64)//8 whole payload bytes (the trailing
        # non-byte blocks are left unused, which keeps stripe rows uniform).
        # The stream is: n_data data groups + m parity groups per stripe of k
        # data groups; any k of a stripe's k+m groups recover the stripe.
        use_fec = 0
        k_fec = m_fec = 0
        B = max(0, (bits_per_frame - HEADER_BITS) // 8)
        P = None
        n_data = 0
        if fec_k > 0:
            if B < 1:
                logging.error(f"FEC needs >= {HEADER_BITS + 8} blocks/frame "
                              f"(bits_per_frame={bits_per_frame}); use smaller M")
                return False
            if fec_k + fec_m < 2 or fec_k + fec_m > 255:
                logging.error(f"Invalid FEC stripe k+m={fec_k + fec_m} (need 2..255)")
                return False
            use_fec = 1
            k_fec, m_fec = fec_k, fec_m
            n_data = (file_size + B - 1) // B      # data groups
            P = cauchy_matrix(k_fec, m_fec)
            total_frames = n_data + len(stripe_plan(n_data, k_fec, m_fec)) * m_fec
            logging.info(f"FEC enabled: k={k_fec}, m={m_fec}, B={B} bytes/group, "
                         f"{n_data} data + {len(stripe_plan(n_data, k_fec, m_fec)) * m_fec} "
                         f"parity groups = {total_frames} stream groups")
        else:
            total_frames = (total_bits + bits_per_frame - 1) // bits_per_frame

        # Create metadata
        meta = {
            'filename': filename,
            'file_size': file_size,
            'M': M,
            'R': R,
            'width': width,
            'height': height,
            'total_bits': total_bits,
        }
        if use_fec:
            with open(file_path, 'rb') as _f:
                _gh = hash64_file(_f)
            meta.update({'fec': 1, 'k': k_fec, 'm': m_fec, 'pd': n_data,
                         'B': B, 'gh': _gh})

        # Start ffmpeg for video encoding
        ffmpeg_command = [
            'ffmpeg',
            '-y',                   # Overwrite existing files
            '-f', 'rawvideo',       # Input format
            '-vcodec', 'rawvideo',
            '-s', f'{width}x{height}',  # Frame size
            '-pix_fmt', 'gray',     # Input format: 8-bit gray
            '-r', '30',             # Frame rate
            '-i', '-',              # Read from stdin
            '-c:v', 'libx264',      # Codec
            '-pix_fmt', 'yuv420p',  # Pixel format
            '-crf', str(crf),       # Quality
            '-preset', preset,      # x264 preset (speed vs size)
            output_video_path
        ]

        ffmpeg_process = subprocess.Popen(
            ffmpeg_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Create meta-image and send to ffmpeg
        meta_img = create_meta_image(meta, width, height)
        meta_img = meta_img.convert('L')  # Convert to 8-bit format
        meta_bytes = meta_img.tobytes()

        for r in range(R_META):
            ffmpeg_process.stdin.write(meta_bytes)
        logging.info(f"Written {R_META} copies of metadata")

        # Queues for interprocess communication. The task queue carries
        # frame indices and the result queue carries (frame_idx, slot)
        # pairs — small ints only. The heavy frame bytes travel through the
        # shared-memory pool instead of a manager queue, so nothing big is
        # pickled over a socket on the hot path.
        task_queue = multiprocessing.Queue()
        out_queue = multiprocessing.Queue()

        frame_size = width * height
        pool_n = max(num_processes, 2)
        pool_name, pool_shm, pool_buf, pool_sems = _make_frame_pool(pool_n, frame_size)

        # Start worker processes
        workers = []
        for _ in range(num_processes):
            p = multiprocessing.Process(
                target=_shm_encode_worker,
                args=(task_queue, out_queue, pool_sems, pool_name, pool_n, frame_size,
                      file_path, M, width, height, bits_per_frame, total_bits,
                      P, k_fec, B * 8, B, n_data)
            )
            p.start()
            workers.append(p)

        # Dispatch all frame tasks (small ints) plus one poison pill per
        # worker so they exit once the queue is drained.
        for idx in range(total_frames):
            task_queue.put(idx)
        for _ in range(num_processes):
            task_queue.put(None)

        # Main loop: collect (frame_idx, slot) results and stream the
        # finished frames to ffmpeg in order.
        next_write = 0
        buffer = {}

        try:
            while next_write < total_frames:
                # Write the next frame to ffmpeg if it is ready.
                if next_write in buffer:
                    slot = buffer.pop(next_write)
                    if slot < 0:
                        raise RuntimeError(f"Error generating frame {next_write}")

                    # Write R copies (raw gray bytes straight from the pool).
                    base = slot * frame_size
                    data = bytes(pool_buf[base:base + frame_size])
                    for _ in range(R):
                        ffmpeg_process.stdin.write(data)
                    pool_sems[slot].release()

                    next_write += 1
                    if next_write % 10 == 0 or next_write == total_frames:
                        elapsed = time.time() - start_time
                        speed = next_write / max(elapsed, 0.001)
                        progress = next_write / total_frames * 100
                        logging.info(f"Encoding: {next_write}/{total_frames} frames ({progress:.1f}%) | Speed: {speed:.1f} fps")
                    continue

                # Wait for the next result.
                frame_idx, slot = out_queue.get(timeout=30)
                buffer[frame_idx] = slot

        except Exception as e:
            logging.error(f"Error processing frames: {str(e)}")
            ffmpeg_process.terminate()
            return False

        finally:
            # Reap workers and release the pool. (Workers already received
            # their poison pills above; terminate is the safety net.)
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join()
            _close_pool(pool_shm)

        # Close ffmpeg
        ffmpeg_process.stdin.close()
        ffmpeg_process.wait()

        if ffmpeg_process.returncode != 0:
            logging.error(f"ffmpeg error: return code {ffmpeg_process.returncode}")
            return False

        logging.info(f"Video successfully created: {output_video_path}")
        return True

    except Exception as e:
        logging.error(f"Critical error during encoding: {str(e)}")
        return False

def decode_meta_frames(meta_frames, width, height):
    try:
        # Calculate cropped dimensions, multiples of M_META
        cropped_width = (width // M_META) * M_META
        cropped_height = (height // M_META) * M_META
        blocks_x = cropped_width // M_META
        blocks_y = cropped_height // M_META
        total_blocks = blocks_x * blocks_y

        # Convert frames into cropped numpy arrays
        meta_arrays = []
        for frame in meta_frames:
            arr = np.frombuffer(frame, dtype=np.uint8).reshape(height, width)
            # Crop to dimensions that are multiples of the metadata block
            cropped_arr = arr[:cropped_height, :cropped_width]
            meta_arrays.append(cropped_arr)

        # Vectorized processing
        stacked = np.stack(meta_arrays)
        reshaped = stacked.reshape(
            stacked.shape[0],
            blocks_y,
            M_META,
            blocks_x,
            M_META
        )

        # Averaging by block pixels
        block_avgs = reshaped.mean(axis=(2, 4))

        # Averaging by copies
        avg_bits = block_avgs.mean(axis=0)

        # Threshold processing
        bits = (avg_bits >= 128).ravel()[:total_blocks].astype(np.uint8)

        # Conversion to bytes
        byte_array = bytearray()
        for i in range(0, len(bits), 8):
            byte_val = 0
            bits_left = min(8, len(bits) - i)
            for j in range(bits_left):
                byte_val = (byte_val << 1) | bits[i + j]
            if bits_left < 8:
                byte_val <<= (8 - bits_left)
            byte_array.append(byte_val)

        # Parsing JSON
        json_str = byte_array.decode('utf-8', errors='ignore')
        end_pos = json_str.rfind('}')
        if end_pos != -1:
            json_str = json_str[:end_pos+1]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            logging.error("Error decoding JSON metadata")
            return None
    except Exception as e:
        logging.error(f"Error decoding metadata: {str(e)}")
        return None

def _bits_from_frames(frames, M, R, width, height):
    """Sum each M x M block's R copies as integers, threshold -> bits (0/1).

    bit = 1 iff the block's sum >= 128 * R * M * M, which is exactly the
    mean >= 128 test without a float. Integer accumulation into one small
    (blocks_y, blocks_x) int64 array avoids stacking the R frames (248 MB
    at 4K) and the transpose, cutting peak memory and passes. Shared by the
    legacy and FEC group decoders (same channel, same layout)."""
    cropped_width = (width // M) * M
    cropped_height = (height // M) * M
    blocks_x = cropped_width // M
    blocks_y = cropped_height // M
    acc = np.zeros((blocks_y, blocks_x), dtype=np.int64)
    for frame in frames:
        arr = np.frombuffer(frame, dtype=np.uint8).reshape(height, width)
        b = arr[:cropped_height, :cropped_width].reshape(blocks_y, M, blocks_x, M)
        acc += b.sum(axis=3, dtype=np.int64).sum(axis=1, dtype=np.int64)
    bits_array = (acc >= 128 * R * M * M).ravel()
    return bits_array, blocks_x * blocks_y


def decode_data_frame(frames, M, R, width, height, frame_idx, meta):
    try:
        bits_array, total_blocks = _bits_from_frames(frames, M, R, width, height)
        start_bit = frame_idx * total_blocks
        end_bit = min(start_bit + total_blocks, meta['tb'])
        num_bits = end_bit - start_bit
        # Crop to the required number of bits
        bits = bits_array[:num_bits].astype(np.uint8)

        # Vectorized big-endian bit packing (the old python bit loop was
        # ~10 ms/group at 4K — fine, but this is 100x cheaper and the
        # trailing partial byte keeps the same shift-left semantics: the
        # leftover zero bits in `padded` shift the byte left exactly as
        # many places the old loop did). Sized by num_bits so the final
        # partial frame emits only its real bytes.
        n_bytes = (num_bits + 7) // 8
        padded = np.zeros(n_bytes * 8, dtype=np.uint8)
        padded[:num_bits] = bits
        weights = np.array([128, 64, 32, 16, 8, 4, 2, 1], dtype=np.uint8)
        byte_data = (padded.reshape(-1, 8) * weights).sum(
            axis=1, dtype=np.uint8).tobytes()

        return frame_idx, bytes(byte_data), num_bits
    except Exception as e:
        logging.error(f"Error decoding frame {frame_idx}: {str(e)}")
        # Signal failure to the main loop (n_bits < 0) so it aborts fast
        # instead of writing an empty frame and only noticing the size
        # mismatch at the very end of the stream.
        return frame_idx, None, -1


def decode_fec_group(frames, M, R, width, height):
    """Decode ONE stream group (R copies) of an FEC stream.

    The group frame holds an 8-byte header (first 64 blocks) then B payload
    bytes. Returns (header8, payload_bytes) on success or None on failure.
    The walker cross-checks the header (magic/seq/crc) before using the
    payload; a group whose copies are missing is simply never yielded.
    """
    try:
        bits_array, total_blocks = _bits_from_frames(frames, M, R, width, height)
        n_bytes = max(0, (total_blocks - HEADER_BITS) // 8)  # payload bytes B
        total_bits = HEADER_BITS + n_bytes * 8
        # Vectorized big-endian packing of header + payload bits.
        padded = np.zeros(total_bits, dtype=np.uint8)
        padded[:total_bits] = bits_array[:total_bits].astype(np.uint8)
        weights = np.array([128, 64, 32, 16, 8, 4, 2, 1], dtype=np.uint8)
        blob = (padded.reshape(-1, 8) * weights).sum(axis=1, dtype=np.uint8)
        header8 = bytes(blob[:HEADER_BYTES])
        payload = bytes(blob[HEADER_BYTES:])
        return (header8, payload)
    except Exception as e:
        logging.error(f"Error decoding FEC group: {str(e)}")
        return None





def _fec_walk_groups(frame_queue, R, M, width, height, total_groups,
                     ffmpeg_process, reader_thread, stop_event, frame_size,
                     n_threads=16):
    """Yield (g, payload) for every intact group in an FEC stream.

    Steady state is aligned to group boundaries and whole R-frame windows
    are decoded by a thread pool: the numpy block sum releases the GIL, so
    the walk runs ~n_threads groups/s instead of one. A window that fails
    header/CRC verification (a cut, or a damaged group) drops the walk
    into a serial one-frame slide that re-aligns on the next intact
    boundary; the header carries the group's ORIGINAL stream index, so a
    cut of whole groups shows up as an index jump the MDS assembler in the
    caller repairs from the surviving parity. Raw frames are buffered up
    to RAW_BUDGET bytes ahead of the cursor, so memory scales with
    resolution, not with the thread count.
    """
    RAW_BUDGET = 2 * 1024 ** 3

    def _get_frame():
        while True:
            try:
                d = frame_queue.get(timeout=5)
            except Empty:
                if stop_event is not None and stop_event.is_set():
                    return None
                # ffmpeg finished AND the reader drained the pipe; the queue
                # may still hold a few frames before true EOF.
                if ffmpeg_process is not None and ffmpeg_process.poll() is not None \
                        and (reader_thread is None or not reader_thread.is_alive()):
                    try:
                        return frame_queue.get_nowait()
                    except Empty:
                        return None
                continue
            if len(d) != frame_size:
                return None  # truncated tail frame
            return d

    def _check(res):
        """(g, payload) if the decoded window verifies, else None."""
        if res is None:
            return None
        h8, payload = res
        g, h_ok = unpack_header(h8)
        if h_ok and g is not None and 0 <= g < total_groups \
                and zlib.crc32(payload) == header_crc(h8):
            return (g, payload)
        return None

    base = 0    # absolute stream-frame number of buf[0]
    buf = []    # raw frames read so far (covers [base, base + len(buf)))
    cursor = 0  # absolute stream-frame number of the next unconsumed frame
    eof = False
    # Cap in-flight windows by the thread count AND by RAW_BUDGET so the
    # raw-frame buffer stays bounded (it scales with resolution, not threads).
    max_windows = max(1, min(n_threads, 16, (RAW_BUDGET // frame_size) // R))
    pool = ThreadPoolExecutor(max_workers=max(1, min(n_threads, 16)))

    def _fill(upto):
        """Read frames until buf covers [base, upto) or the stream ends."""
        nonlocal eof
        while base + len(buf) < upto and not eof:
            d = _get_frame()
            if d is None:
                eof = True
                break
            buf.append(d)

    def _compact(new_cursor):
        # Frames before new_cursor are dead: every in-flight window was
        # sliced at submit time and starts at >= new_cursor.
        nonlocal base
        drop = new_cursor - base
        if drop >= R:
            del buf[:drop]
            base += drop

    def _resync(start):
        """Serial one-frame slide from `start` to the next intact boundary.
        Returns (g, payload, new_cursor) or (None, None, None) at EOF."""
        off = 0
        while True:
            _fill(start + off + R)
            if base + len(buf) < start + off + R:
                return None, None, None
            res = decode_fec_group(buf[start + off - base:start + off - base + R],
                                   M, R, width, height)
            ok = _check(res)
            if ok is not None:
                return ok[0], ok[1], start + off + R
            off += 1

    try:
        pending = []  # [(wstart, future)] in stream order, decoded in the pool
        while True:
            # Queue whole R-frame windows ahead of the cursor while there is
            # room in the buffer budget and the pool isn't saturated.
            while len(pending) < max_windows:
                next_start = cursor + len(pending) * R
                _fill(next_start + R)
                if base + len(buf) < next_start + R:
                    break  # EOF: no full window left
                pending.append((next_start, pool.submit(
                    decode_fec_group,
                    buf[next_start - base:next_start - base + R],
                    M, R, width, height)))
            if not pending:
                return  # EOF
            wstart, fut = pending.pop(0)
            ok = _check(fut.result())
            if ok is not None:
                yield ok
                cursor = wstart + R
                _compact(cursor)
            else:
                g, payload, cursor = _resync(wstart)
                if g is None:
                    return
                yield (g, payload)
                _compact(cursor)
                # The in-flight windows were aligned to the old boundary;
                # after a cut they may straddle the new one, so resubmit.
                pending = []
    finally:
        pool.shutdown(wait=False)


def _shm_decode_worker(task_queue, output_queue, pool_sems, pool_name,
                       group_size, frame_size, M, R, width, height, meta):
    """Decode worker attached to the shared-memory group pool.

    `task_queue` carries (group_seq, slot) pairs; the worker reads the R
    frame copies in place from /dev/shm (zero-copy memoryviews), decodes,
    reports the small (frame_idx, bytes, n_bits) result, and then releases
    the slot so the reader can overwrite it.
    """
    shm = None
    try:
        shm = SharedMemory(name=pool_name)
        mv = memoryview(shm.buf)
        while True:
            task = task_queue.get(timeout=1)
            if task is None:
                break
            seq, slot = task
            base = slot * group_size
            frames = [mv[base + i * frame_size: base + (i + 1) * frame_size]
                      for i in range(R)]
            result = decode_data_frame(frames, M, R, width, height, seq, meta)
            output_queue.put(result)
            pool_sems[slot].release()
    except Empty:
        pass
    except Exception as e:
        logging.error(f"Error in worker decoding process: {str(e)}")
    finally:
        if shm is not None:
            try:
                shm.close()
            except Exception:
                pass


def ffmpeg_reader_process(ffmpeg_process, frame_size, frame_queue, stop_event):
    try:
        while not stop_event.is_set():
            frame_data = ffmpeg_process.stdout.read(frame_size)
            if not frame_data or len(frame_data) != frame_size:
                break
            frame_queue.put(frame_data)
    except Exception as e:
        logging.error(f"Error reading frames from ffmpeg: {str(e)}")
    finally:
        ffmpeg_process.stdout.close()
        logging.debug("ffmpeg read thread finished")


def _cleanup_decode(ffmpeg_process, reader_thread, stop_event, frame_queue):
    """Tear down ffmpeg and the reader thread after a failed decode.

    Without this, a late failure (e.g. bad metadata) leaves the non-daemon
    reader thread producing into a bounded queue: it fills up, blocks on
    put(), ffmpeg blocks on a full stdout pipe, and the process can never
    exit — the harness timeout then leaves orphans behind.
    """
    if stop_event is not None:
        try:
            stop_event.set()
        except Exception:
            pass
    if ffmpeg_process is not None:
        try:
            ffmpeg_process.terminate()
            ffmpeg_process.wait(timeout=5)
        except Exception:
            pass
    if frame_queue is not None:
        # Unblock the reader thread if it is parked on a full queue.
        try:
            while True:
                frame_queue.get_nowait()
        except Exception:
            pass
    if reader_thread is not None and reader_thread.is_alive():
        reader_thread.join(timeout=5)

def _spawn_reader(video_path, width, height):
    """Start ffmpeg raw-frame extraction (resampled to width x height) plus the
    reader thread. Returns (ffmpeg_process, frame_queue, stop_event,
    reader_thread, frame_size)."""
    frame_size = width * height
    ffmpeg_command = [
        'ffmpeg',
        '-i', video_path,
        '-vf', f'scale={width}:{height}',
        '-f', 'rawvideo',
        '-pix_fmt', 'gray',
        '-v', 'error',
        '-'
    ]
    ffmpeg_process = subprocess.Popen(
        ffmpeg_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    frame_queue = Queue(maxsize=100)
    stop_event = threading.Event()
    reader_thread = threading.Thread(
        target=ffmpeg_reader_process,
        args=(ffmpeg_process, frame_size, frame_queue, stop_event),
        daemon=True,
    )
    reader_thread.start()
    return ffmpeg_process, frame_queue, stop_event, reader_thread, frame_size

def _packer_thread(frame_queue, dispatch_queue, pool_sems, pool_mv,
                   group_size, frame_size, R, total_groups, stop_event):
    """Pack exactly total_groups groups of R raw frames from the reader's
    frame_queue into pool slots (off the main process, so the main loop only
    moves small ints), announcing each finished group's seq on dispatch_queue.
    Stopping at a fixed count avoids a 60 s stall waiting on EOF: once all
    groups are packed the packer is done regardless of what ffmpeg emits.
    On any error or premature EOF it puts None and exits."""
    seq = 0
    try:
        while seq < total_groups and not stop_event.is_set():
            slot = seq % len(pool_sems)
            pool_sems[slot].acquire()
            base = slot * group_size
            try:
                for i in range(R):
                    d = frame_queue.get(timeout=60)
                    if len(d) != frame_size:
                        raise ValueError(
                            f"incomplete frame from ffmpeg ({len(d)}/{frame_size})")
                    pool_mv[base + i * frame_size: base + (i + 1) * frame_size] = d
            except (Empty, ValueError):
                pool_sems[slot].release()
                logging.warning("Packer: frame stream ended (seq=%d)", seq)
                break
            dispatch_queue.put(seq)
            seq += 1
    except Exception as e:
        logging.error(f"Packer error: {str(e)}")
    finally:
        try:
            dispatch_queue.put(None)
        except Exception:
            pass
        stop_event.set()


def decode_video_to_file(video_path, num_processes):
    stop_event = None
    reader_thread = None
    ffmpeg_process = None
    frame_queue = None
    try:
        start_time = time.time()
        recon_dir = get_reconstructed_directory()

        if not os.path.exists(video_path):
            logging.error(f"Video file not found: {video_path}")
            return False

        # 1. Get video dimensions using ffprobe robustly
        probe_command = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0',
            video_path
        ]
        
        probe_output = subprocess.check_output(probe_command).decode().strip()
        if not probe_output:
             raise ValueError("ffprobe returned empty output")
        width, height = map(int, probe_output.split(','))
        frame_size = width * height
        logging.info(f"Video size: {width}x{height}, frame size: {frame_size} bytes")

        # 2-3. Start ffmpeg + reader thread (resampled to the probe size)
        (ffmpeg_process, frame_queue, stop_event,
         reader_thread, frame_size) = _spawn_reader(video_path, width, height)

        # 4. Read metadata
        meta_frames = []
        for _ in range(R_META):
            try:
                frame_data = frame_queue.get(timeout=30)
                if len(frame_data) != frame_size:
                    raise ValueError("Incomplete metadata frame")
                meta_frames.append(frame_data)
            except Empty:
                raise TimeoutError("Timeout reading metadata")

        meta = decode_meta_frames(meta_frames, width, height)

        # 4b. Geometry realignment: the video may have been (re)encoded at a
        # different size than the payload was embedded at (re-hosting,
        # downscale). The block layout is geometry-bound, so rescan the
        # metadata at candidate sizes until the embedded JSON parses.
        # Candidates are the video size scaled by common re-hosting ratios and
        # Candidates come in two forms, per re-hosting ratio:
        #   * the EXACT product (width*f, height*f) — matches sources whose
        #     size is NOT on the 16-block grid, e.g. 1080p (height 1080 is
        #     67.5 blocks); the exact product lands on 1920x1080 cleanly;
        #   * the same product SNAPPED to the 16-block grid (_to16) — absorbs
        #     slightly inexact platform ratios, e.g. 426x240 * 3 = 1278 -> 1280.
        # The metadata reader crops to the 16-grid itself, so a candidate need
        # not be a multiple of 16 (only >= 16). The strict acceptance check
        # (JSON parses AND its w/h == the probe size) rejects misaligned probes.
        if not (meta and meta.get('w') == width and meta.get('h') == height):
            _cleanup_decode(ffmpeg_process, reader_thread, stop_event, frame_queue)
            def _to16(n):
                return max(16, int(round(n / 16.0)) * 16)
            # most common re-hosting ratios first
            factors = (1.5, 2.0, 4.0 / 3.0, 3.0, 4.0, 4.5, 6.0,
                       8.0 / 3.0, 9.0 / 4.0, 16.0 / 5.0,
                       2.25, 2.5, 3.5, 5.0, 1.75, 1.25, 0.5)
            candidates = []
            if meta and meta.get('w') and meta.get('h'):
                candidates.append((int(meta['w']), int(meta['h'])))
                candidates.append((_to16(meta['w']), _to16(meta['h'])))
            # per factor: exact product first (off-grid sizes like 1080p),
            # then the 16-snapped form (inexact platform ratios)
            for factor in factors:
                candidates.append((int(round(width * factor)),
                                   int(round(height * factor))))
                candidates.append((_to16(width * factor),
                                   _to16(height * factor)))
            seen = set()
            ordered = []
            for cw, ch in candidates:
                if cw < 16 or ch < 16:
                    continue
                # skip absurdly large probes (no realistic source > 8K)
                if cw > 8192 or ch > 8192:
                    continue
                if (cw, ch) == (width, height) or (cw, ch) in seen:
                    continue
                seen.add((cw, ch))
                ordered.append((cw, ch))
            for cw, ch in ordered:
                logging.info(f"Geometry probe: rescan metadata at {cw}x{ch}")
                (ffmpeg_process, frame_queue, stop_event,
                 reader_thread, frame_size) = _spawn_reader(video_path, cw, ch)
                mf = []
                complete = True
                for _ in range(R_META):
                    try:
                        fd = frame_queue.get(timeout=30)
                    except Empty:
                        complete = False
                        break
                    if len(fd) != frame_size:
                        complete = False
                        break
                    mf.append(fd)
                if not complete:
                    _cleanup_decode(ffmpeg_process, reader_thread, stop_event, frame_queue)
                    continue
                cand_meta = decode_meta_frames(mf, cw, ch)
                if cand_meta and cand_meta.get('w') == cw and cand_meta.get('h') == ch:
                    meta = cand_meta
                    width, height = cw, ch
                    logging.info(f"Geometry realigned to {width}x{height}")
                    break
                _cleanup_decode(ffmpeg_process, reader_thread, stop_event, frame_queue)
            if meta is None:
                raise ValueError("Failed to decode metadata (video geometry may not match the embedded layout)")
            if meta.get('w') != width or meta.get('h') != height:
                raise ValueError(
                    f"Metadata geometry {meta.get('w')}x{meta.get('h')} != video "
                    f"{width}x{height}; no candidate layout matched"
                )

        filename = meta['fn']
        M = meta['M']
        R = meta['R']
        total_bits = meta['tb']
        file_size = meta['fs']

        # 4c. FEC stream (meta carries fec/k/m/pd/B/gh). Each group frame's
        # header carries the group's ORIGINAL stream index, so a cut of whole
        # groups shows up as a jump in that index; the walker collects every
        # intact group (data and parity) and the Cauchy MDS code below
        # repairs the erased ones. Returns on its own; the shared finally
        # block below tears the reader/ffmpeg down.
        if meta.get('fec'):
            k_fec = int(meta['k'])
            m_fec = int(meta['m'])
            n_data = int(meta['pd'])
            gh = meta.get('gh', '')
            P = cauchy_matrix(k_fec, m_fec)
            n_strips = (n_data + k_fec - 1) // k_fec
            n_parity = n_strips * m_fec
            total_groups = n_data + n_parity
            # The final stripe may be short: carve it off from the end, as
            # the encoder does (its stride is k_last + m, not k + m).
            k_last = n_data - (n_strips - 1) * k_fec
            last_ss = total_groups - (k_last + m_fec)
            logging.info(f"FEC decode: k={k_fec}, m={m_fec}, {n_data} data + "
                         f"{n_parity} parity = {total_groups} groups")

            def _stream_of(s, idx_in):
                if s < n_strips - 1:
                    return s * (k_fec + m_fec) + idx_in
                return last_ss + idx_in

            groups_q = Queue()

            def _walker():
                try:
                    for g, payload in _fec_walk_groups(
                            frame_queue, R, M, width, height, total_groups,
                            ffmpeg_process, reader_thread, stop_event,
                            frame_size):
                        groups_q.put((g, payload))
                except Exception as e:
                    logging.error(f"FEC walker error: {str(e)}")
                finally:
                    groups_q.put(None)

            walker = threading.Thread(target=_walker, daemon=True)
            walker.start()

            output_path = os.path.join(recon_dir, filename)
            received_map = {}
            success = False
            try:
                while True:
                    rec = groups_q.get(timeout=60)
                    if rec is None:
                        break
                    g, payload = rec
                    if g in received_map:
                        logging.warning(f"FEC group {g} delivered twice; ignoring")
                        continue
                    received_map[g] = payload
                out = bytearray()
                repaired = 0
                for s in range(n_strips):
                    ds = s * k_fec
                    k_s = min(k_fec, n_data - ds)
                    recv = [received_map.get(_stream_of(s, i))
                            for i in range(k_s + m_fec)]
                    n_lost = k_s + m_fec - sum(1 for x in recv if x is not None)
                    if n_lost > m_fec:
                        raise FecError(
                            f"stripe {s}: {n_lost} of {k_s + m_fec} groups lost "
                            f"({m_fec} correctable)")
                    data = fec_decode(recv, k_s, P)
                    for i in range(k_s):
                        if recv[i] is None:
                            repaired += 1
                        out.extend(data[i])
                if len(out) < file_size:
                    raise ValueError(
                        f"FEC: {len(out)} payload bytes available, need {file_size}")
                with open(output_path, 'wb') as _of:
                    _of.write(bytes(out[:file_size]))
                if gh:
                    with open(output_path, 'rb') as _f:
                        actual = hash64_file(_f)
                    if actual != gh:
                        logging.error(f"FEC hash mismatch: {actual} != {gh}")
                        raise FecError("payload hash mismatch")
                logging.info(f"Successfully reconstructed {file_size} bytes "
                             f"(FEC, {repaired} groups repaired from erasures)")
                success = True
            except Empty:
                logging.error(f"FEC decode stalled: no group for 60s "
                              f"({len(received_map)} collected)")
            except Exception as e:
                logging.error(f"FEC decode error: {str(e)}")
            finally:
                if not success and os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                        logging.info("Removed incomplete reconstruction file")
                    except OSError:
                        pass
                # The FEC branch owns the reader/ffmpeg (no shared pool), so
                # tear them down here the way the legacy finally does.
                _cleanup_decode(ffmpeg_process, reader_thread, stop_event,
                                frame_queue)
            return success

        # 5. Process data
        cropped_width = (width // M) * M
        cropped_height = (height // M) * M
        blocks_x = cropped_width // M
        blocks_y = cropped_height // M
        bits_per_frame = blocks_x * blocks_y
        total_frames = (total_bits + bits_per_frame - 1) // bits_per_frame

        logging.info(f"Starting decoding: file '{filename}', size {file_size} bytes")
        logging.info(f"Parameters: M={M}, R={R}, data frames: {total_frames}")

        output_path = os.path.join(recon_dir, filename)
        output_file = open(output_path, 'wb')

        # Data phase: shared-memory group pool. Each pool slot holds one
        # group of R raw frame copies (group_size bytes). The reader thread
        # (already running, past the metadata) fills frame_queue; a packer
        # thread copies each group into a pool slot (the heavy memcpy runs
        # off the main process); the main process only moves small ints —
        # it dispatches (seq, slot) pairs to workers, which decode in place
        # from /dev/shm and release the slot. No group is ever pickled.
        group_size = R * frame_size
        # Keep the pool small (a few groups, not one per worker): workers
        # decode a 4K group in ~0.3 s, so 2-4 groups in flight is plenty of
        # overlap, and a smaller pool means less shared memory to page-fault
        # on first touch (the copy rate drops ~7x on unfaulted pages).
        # Still capped so huge group sizes can't blow up /dev/shm.
        POOL_BUDGET = 2 * 1024 ** 3
        pool_n = max(2, min(num_processes, 4, POOL_BUDGET // group_size))
        pool_name, pool_shm, pool_buf, pool_sems = _make_frame_pool(pool_n, group_size)
        pool_mv = memoryview(pool_buf)

        dispatch_queue = Queue()  # packer -> main: packed group seqs (or None)
        task_queue = multiprocessing.Queue()
        output_queue = multiprocessing.Queue()
        packer_stop = threading.Event()

        packer = threading.Thread(
            target=_packer_thread,
            args=(frame_queue, dispatch_queue, pool_sems, pool_mv,
                  group_size, frame_size, R, total_frames, packer_stop),
            daemon=True,
        )
        packer.start()

        workers = []
        for _ in range(num_processes):
            p = multiprocessing.Process(
                target=_shm_decode_worker,
                args=(task_queue, output_queue, pool_sems, pool_name, group_size,
                      frame_size, M, R, width, height, meta)
            )
            p.daemon = True
            p.start()
            workers.append(p)

        next_frame = 0
        buffer_results = {}
        sent_frames = 0
        processed_frames = 0
        bits_written = 0
        last_progress_time = time.time()
        STALL_TIMEOUT = 15.0  # max seconds without a new decoded frame before giving up
        packer_done = False

        try:
            while processed_frames < total_frames:
                # Dispatch every group the packer has finished.
                while sent_frames < total_frames and not dispatch_queue.empty():
                    seq = dispatch_queue.get()
                    if seq is None:
                        packer_done = True
                        break
                    task_queue.put((seq, seq % pool_n))
                    sent_frames += 1

                # Write finished frames in order.
                wrote_frame = False
                while next_frame in buffer_results:
                    f_data, n_bits = buffer_results.pop(next_frame)
                    output_file.write(f_data)
                    bits_written += n_bits
                    processed_frames += 1
                    next_frame += 1
                    wrote_frame = True

                    if processed_frames % 10 == 0 or processed_frames == total_frames:
                        elapsed = time.time() - start_time
                        speed = processed_frames / max(elapsed, 0.001)
                        progress = bits_written / total_bits * 100
                        logging.info(f"Decoding: {processed_frames}/{total_frames} frames ({progress:.1f}%) | Speed: {speed:.1f} fps")

                if wrote_frame:
                    last_progress_time = time.time()

                if processed_frames >= total_frames:
                    break

                # Fetch the next completed result (blocking, with a stall guard).
                try:
                    idx, f_data, n_bits = output_queue.get(timeout=0.5)
                    if n_bits < 0:
                        raise ValueError(f"Frame {idx} failed to decode (see worker error log)")
                    buffer_results[idx] = (f_data, n_bits)
                    last_progress_time = time.time()
                except Empty:
                    if packer_done and sent_frames == processed_frames and not buffer_results:
                        if ffmpeg_process.poll() is not None:
                            raise ValueError("ffmpeg frame stream ended before all frames were read")
                    if time.time() - last_progress_time > STALL_TIMEOUT:
                        raise TimeoutError(
                            f"Decode stalled: no new frames for {STALL_TIMEOUT:.0f}s "
                            f"(processed {processed_frames}/{total_frames}, sent {sent_frames}/{total_frames})"
                        )

            logging.info("All frames processed")

        except Exception as e:
            logging.error(f"Error during decoding: {str(e)}")
            if os.path.exists(output_path) and os.path.getsize(output_path) != file_size:
                try:
                    os.remove(output_path)
                    logging.info("Removed incomplete reconstruction file")
                except OSError:
                    pass
            return False

        finally:
            # Poison the task queue and reap the decode workers.
            for _ in range(num_processes):
                try:
                    task_queue.put(None)
                except Exception:
                    pass
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join()

            # Release any slots still held (workers that were mid-group) so
            # the pool can be torn down cleanly.
            for s in pool_sems:
                try:
                    s.release()
                except Exception:
                    pass
            _close_pool(pool_shm)

            if ffmpeg_process and ffmpeg_process.stdin:
                try:
                    ffmpeg_process.stdin.close()
                except Exception:
                    pass

            if ffmpeg_process:
                try:
                    ffmpeg_process.wait()
                except Exception:
                    pass

            if stop_event:
                stop_event.set()

            if reader_thread and reader_thread.is_alive():
                reader_thread.join(timeout=2)

            if output_file:
                output_file.close()

        reconstructed_size = os.path.getsize(output_path)
        success = reconstructed_size == file_size

        if success:
            logging.info(f"Successfully reconstructed {reconstructed_size} bytes")
        else:
            logging.warning(f"Size mismatch: reconstructed {reconstructed_size}/{file_size} bytes")

        return success

    except Exception as e:
        logging.error(f"Critical error during decoding: {str(e)}")
        _cleanup_decode(ffmpeg_process, reader_thread, stop_event, frame_queue)
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="file_to_video_bitcoder CLI")
    subparsers = parser.add_subparsers(dest="mode", help="Operation mode")

    encode_parser = subparsers.add_parser("encode", help="Encode a file to video")
    encode_parser.add_argument("file_path", type=str, help="Path to the input file")
    encode_parser.add_argument("M", type=int, help="Block size (M)")
    encode_parser.add_argument("R", type=int, help="Repetition coefficient (R)")
    encode_parser.add_argument("width", type=int, help="Image width")
    encode_parser.add_argument("height", type=int, help="Image height")
    encode_parser.add_argument("processes", type=int, help="Number of processes")
    encode_parser.add_argument("--crf", type=int, default=23, help="Video quality (CRF)")
    encode_parser.add_argument("--out", type=str, default=None,
                               help="Output video path (default: encoded/encoded_video.mp4)")
    encode_parser.add_argument("--preset", type=str, default="medium",
                               help="x264 preset: ultrafast..veryslow (default medium)")
    encode_parser.add_argument("--fec-k", type=int, default=0,
                               help="FEC stripe: k data groups (0 = no FEC)")
    encode_parser.add_argument("--fec-m", type=int, default=0,
                               help="FEC stripe: m parity groups (0 = no FEC)")

    decode_parser = subparsers.add_parser("decode", help="Decode a video to file")
    decode_parser.add_argument("video_path", type=str, help="Video file path")
    decode_parser.add_argument("processes", type=int, help="Number of processes")

    args = parser.parse_args()

    if args.mode == "encode":
        start_time = time.time()
        if encode_file_to_video(args.file_path, args.M, args.R, args.width, args.height, args.processes, crf=args.crf, out_path=args.out, fec_k=args.fec_k, fec_m=args.fec_m, preset=args.preset):
            elapsed = time.time() - start_time
            print(f"Encoding completed successfully in {elapsed:.2f} sec")
        else:
            print("Encoding completed with errors")

    elif args.mode == "decode":
        start_time = time.time()
        if decode_video_to_file(args.video_path, args.processes):
            elapsed = time.time() - start_time
            print(f"Decoding completed successfully in {elapsed:.2f} sec")
        else:
            print("Decoding completed with errors")
    else:
        parser.print_help()
