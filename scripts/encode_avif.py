#!/usr/bin/env python3
"""AVIF encoding permutation engine.

Invokes avifenc (from libavif) with a matrix of parameter combinations
against every source image. Deduplicates outputs by content hash, writes
files into a sharded directory tree, and records results for manifest assembly.

avifenc reads PNG and Y4M input. PPM/PGM sources are converted to PNG
via ImageMagick before encoding.

Output layout:
    <output_dir>/avif/<encoder-id>/<hash[0:2]>/<hash>.avif

Deduplication: SHA-256 of file content. Identical outputs from different
parameter combos are stored once; the manifest records all parameter combos
that produced each hash.
"""

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Import shared types from the JPEG encoder module
sys.path.insert(0, str(Path(__file__).parent))
from encode_jpeg import EncoderTask, EncoderResult, env_bin, _env_warned


# ── Encoder definitions ────────────────────────────────────────────────────

def build_avifenc_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """avifenc (libavif + libaom) parameter permutations.

    avifenc is SLOW, especially at low speed settings. Speed 0 can take
    minutes per image. We limit image sizes for slow speeds and use a
    generous timeout.
    """
    binary = env_bin("AVIFENC")
    if not binary:
        return []

    encoder_id = "avifenc-libavif"
    tasks = []
    is_gray = source["channels"] == 1
    w, h = source.get("w", 0), source.get("h", 0)
    pixels = w * h

    # ── Parameter axes ──────────────────────────────────────────────
    # Quality: avifenc -q uses 0-100 scale (0=worst, 100=best/lossless)
    qualities = [20, 40, 60, 80, 95] if not quick else [40, 80]
    # Speed: 0=slowest/best .. 10=fastest
    speeds = [0, 4, 6, 8, 10] if not quick else [6, 10]
    # YUV format: 420, 444 (400 for grayscale)
    yuv_formats_rgb = ["420", "444"] if not quick else ["420"]
    # Bit depth
    depths = [8, 10] if not quick else [8]
    # Lossless mode (only in full matrix)
    do_lossless = [False, True] if not quick else [False]
    # Tiling (skip for small images)
    tile_configs = [(0, 0)]  # (tilerowslog2, tilecolslog2)
    if not quick and pixels >= 256 * 256:
        tile_configs.append((1, 1))  # 2x2 tiles

    yuv_formats = ["400"] if is_gray else yuv_formats_rgb

    for lossless in do_lossless:
        if lossless:
            # Lossless: fixed quality, only 444 (or 400 for gray), depth 8 or 10
            lossless_depths = [8, 10] if not quick else [8]
            lossless_speeds = [4, 10] if not quick else [10]
            lossless_yuv = ["400"] if is_gray else ["444"]
            for depth in lossless_depths:
                for speed in lossless_speeds:
                    # Skip very slow speeds on large images
                    if speed <= 2 and pixels > 64 * 64:
                        continue
                    for yuv in lossless_yuv:
                        cmd = [binary]
                        cmd += ["--lossless"]
                        cmd += ["-s", str(speed)]
                        cmd += ["-d", str(depth)]
                        cmd += ["--yuv", yuv]
                        cmd += ["--codec", "aom"]
                        cmd += ["{input}", "{output}"]

                        params = {
                            "lossless": True,
                            "speed": speed,
                            "depth": depth,
                            "yuv": yuv,
                            "tilerowslog2": 0,
                            "tilecolslog2": 0,
                        }

                        tasks.append(EncoderTask(
                            encoder_id=encoder_id,
                            binary=binary,
                            source_name=source["name"],
                            source_path=source["path"],
                            source_channels=source["channels"],
                            params=params,
                            cmd=cmd,
                            needs_png_input=True,
                        ))
        else:
            # Lossy mode: full parameter matrix
            for q in qualities:
                for speed in speeds:
                    # Skip very slow speeds on large images to keep runtime bounded
                    if speed <= 2 and pixels > 64 * 64:
                        continue
                    if speed == 0 and pixels > 32 * 32:
                        continue
                    for yuv in yuv_formats:
                        for depth in depths:
                            for tile_rows, tile_cols in tile_configs:
                                cmd = [binary]
                                cmd += ["-q", str(q)]
                                cmd += ["-s", str(speed)]
                                cmd += ["-d", str(depth)]
                                cmd += ["--yuv", yuv]
                                cmd += ["--codec", "aom"]
                                if tile_rows > 0:
                                    cmd += ["--tilerowslog2", str(tile_rows)]
                                if tile_cols > 0:
                                    cmd += ["--tilecolslog2", str(tile_cols)]
                                cmd += ["{input}", "{output}"]

                                params = {
                                    "quality": q,
                                    "speed": speed,
                                    "depth": depth,
                                    "yuv": yuv if not is_gray else "400",
                                    "lossless": False,
                                    "tilerowslog2": tile_rows,
                                    "tilecolslog2": tile_cols,
                                }

                                tasks.append(EncoderTask(
                                    encoder_id=encoder_id,
                                    binary=binary,
                                    source_name=source["name"],
                                    source_path=source["path"],
                                    source_channels=source["channels"],
                                    params=params,
                                    cmd=cmd,
                                    needs_png_input=True,
                                ))

    return tasks


# ── Task execution ─────────────────────────────────────────────────────────

def _validate_avif(data: bytes) -> str | None:
    """Validate AVIF/HEIF container.

    An ISOBMFF file starts with a box: 4-byte big-endian size followed by
    4-byte box type. For AVIF files, the first box is always "ftyp".

    Returns None if valid, or an error string if invalid.
    """
    if len(data) < 8:
        return f"Too small for AVIF: {len(data)} bytes"

    box_size = struct.unpack(">I", data[:4])[0]
    box_type = data[4:8]

    if box_type != b"ftyp":
        return f"First box is {box_type!r}, expected b'ftyp'"

    if box_size < 8:
        return f"Invalid ftyp box size: {box_size}"

    return None


def run_task_avif(task: EncoderTask, output_dir: Path) -> EncoderResult:
    """Execute a single AVIF encoding task."""
    fd, tmp_path = tempfile.mkstemp(suffix=".avif")
    os.close(fd)
    tmp_png = None

    try:
        # avifenc reads PNG, not PPM/PGM — convert if needed
        input_path = task.source_path
        if task.needs_png_input and task.source_path.endswith((".ppm", ".pgm")):
            tmp_png = tmp_path + ".png"
            conv = subprocess.run(
                ["convert", task.source_path, tmp_png],
                capture_output=True, timeout=30,
            )
            if conv.returncode != 0:
                return EncoderResult(
                    encoder_id=task.encoder_id,
                    source_name=task.source_name,
                    params=task.params,
                    success=False,
                    error=f"PNG conversion failed: {conv.stderr.decode()[:200]}",
                )
            input_path = tmp_png

        # Substitute {output} and {input} placeholders in command
        cmd = []
        for arg in task.cmd:
            if arg == "{output}":
                cmd.append(tmp_path)
            elif arg == "{input}":
                cmd.append(input_path)
            else:
                cmd.append(arg)

        # Use per-encoder environment if set
        env = dict(os.environ)
        env.update(task.env_override)

        # AVIF encoding is slow — generous 600s timeout
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=600,
            env=env,
        )

        if result.returncode != 0:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=result.stderr.decode("utf-8", errors="replace")[:500],
            )

        # Read output and validate
        with open(tmp_path, "rb") as f:
            data = f.read()

        validation_error = _validate_avif(data)
        if validation_error:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=validation_error,
            )

        content_hash = hashlib.sha256(data).hexdigest()[:16]

        # Shard into <encoder>/<hash[0:2]>/<hash>.avif
        encoder_dir = output_dir / "avif" / task.encoder_id
        shard_dir = encoder_dir / content_hash[:2]
        shard_dir.mkdir(parents=True, exist_ok=True)

        final_path = shard_dir / f"{content_hash}.avif"
        if not final_path.exists():
            with open(final_path, "wb") as f:
                f.write(data)

        rel_path = str(final_path.relative_to(output_dir))

        return EncoderResult(
            encoder_id=task.encoder_id,
            source_name=task.source_name,
            params=task.params,
            success=True,
            output_hash=content_hash,
            output_bytes=len(data),
            output_path=rel_path,
        )

    except subprocess.TimeoutExpired:
        return EncoderResult(
            encoder_id=task.encoder_id,
            source_name=task.source_name,
            params=task.params,
            success=False,
            error="Timeout (600s)",
        )
    except Exception as e:
        return EncoderResult(
            encoder_id=task.encoder_id,
            source_name=task.source_name,
            params=task.params,
            success=False,
            error=str(e)[:500],
        )
    finally:
        for p in [tmp_path, tmp_png]:
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ── Orchestration ──────────────────────────────────────────────────────────

ENCODER_METADATA = {
    "avifenc-libavif": {
        "name": "avifenc (libavif + libaom)",
        "version": "libavif",
        "binary": "avifenc",
        "source_url": "https://github.com/AOMediaCodec/libavif",
        "compile_flags": ["AVIF_CODEC_AOM=ON"],
    },
}

TASK_BUILDERS = [
    build_avifenc_tasks,
]


def build_all_tasks(sources: list[dict], quick: bool) -> list[EncoderTask]:
    """Build the full task matrix from all AVIF encoders x all sources."""
    tasks = []
    for source in sources:
        for builder in TASK_BUILDERS:
            tasks.extend(builder(source, quick))
    return tasks


def run_all(sources: list[dict], output_dir: Path, quick: bool = False,
            workers: int = 0) -> list[EncoderResult]:
    """Run all AVIF encoding tasks in parallel."""
    tasks = build_all_tasks(sources, quick)
    print(f"Built {len(tasks)} AVIF encoding tasks across {len(sources)} sources")

    if not tasks:
        print("  No AVIF tasks to run (AVIFENC not set?)")
        return []

    if workers <= 0:
        # AVIF encoding is CPU-heavy; use fewer workers than JPEG
        workers = min(os.cpu_count() or 4, 8)

    results = []
    completed = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_task_avif, task, output_dir): task
            for task in tasks
        }

        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            if not result.success:
                failed += 1
            if completed % 100 == 0 or completed == len(tasks):
                print(f"  [{completed}/{len(tasks)}] "
                      f"{failed} failed, "
                      f"{completed - failed} ok")

    # Deduplicate: collect unique hashes
    unique_hashes = set()
    for r in results:
        if r.success:
            unique_hashes.add(r.output_hash)

    print(f"\nAVIF encoding complete: {completed - failed} ok, {failed} failed, "
          f"{len(unique_hashes)} unique files")

    return results


# ── Standalone entry point ─────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="AVIF encoding permutations")
    parser.add_argument("--sources", "-s", type=Path, required=True,
                        help="Path to sources.json from generate_sources.py")
    parser.add_argument("--output", "-o", type=Path, required=True,
                        help="Output directory for encoded files")
    parser.add_argument("--quick", action="store_true",
                        help="Reduced parameter matrix for quick testing")
    parser.add_argument("--workers", "-j", type=int, default=0,
                        help="Number of parallel workers (0 = auto)")
    args = parser.parse_args()

    with open(args.sources) as f:
        sources = json.load(f)

    print(f"Loaded {len(sources)} source images")
    results = run_all(sources, args.output, quick=args.quick,
                      workers=args.workers)

    # Save raw results for manifest assembly
    results_path = args.output / "avif_encoding_results.json"
    with open(results_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"Results saved to {results_path}")


if __name__ == "__main__":
    main()
