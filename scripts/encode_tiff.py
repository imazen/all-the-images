#!/usr/bin/env python3
"""TIFF and HEIC encoding permutation engine.

Invokes ImageMagick convert, libtiff tiffcp, and libheif heif-enc with a
matrix of parameter combinations against every source image. Deduplicates
outputs by content hash, writes files into sharded directory trees, and
records results for manifest assembly.

TIFF encoders:
  - ImageMagick convert (compression, depth, color type, endianness)
  - tiffcp (re-compression from baseline TIFF, strip vs tile layout)

HEIC encoder:
  - heif-enc from libheif (lossy quality, lossless, bit depth)

Output layout:
    <output_dir>/tiff/<encoder-id>/<hash[0:2]>/<hash>.tiff
    <output_dir>/heic/<encoder-id>/<hash[0:2]>/<hash>.heic

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
from dataclasses import asdict
from pathlib import Path

from encode_jpeg import EncoderTask, EncoderResult, env_bin, _env_warned


# ── Validation helpers ────────────────────────────────────────────────────

def _validate_tiff(data: bytes) -> str | None:
    """Validate TIFF header. Returns error string or None if valid."""
    if len(data) < 8:
        return f"Output too small: {len(data)} bytes"

    byte_order = data[0:2]
    if byte_order == b"II":
        # Little-endian: magic 42 at bytes 2-3 as uint16 LE
        magic = struct.unpack_from("<H", data, 2)[0]
    elif byte_order == b"MM":
        # Big-endian: magic 42 at bytes 2-3 as uint16 BE
        magic = struct.unpack_from(">H", data, 2)[0]
    else:
        return f"Invalid TIFF byte order: {data[0:2].hex()}"

    if magic != 42:
        return f"Invalid TIFF magic: {magic} (expected 42)"

    return None


def _validate_heic(data: bytes) -> str | None:
    """Validate HEIC/ISOBMFF header. Returns error string or None if valid."""
    if len(data) < 12:
        return f"Output too small: {len(data)} bytes"

    # ISOBMFF: bytes 4-8 should be "ftyp"
    if data[4:8] != b"ftyp":
        return f"Invalid HEIC header: expected 'ftyp' at bytes 4-8, got {data[4:8].hex()}"

    return None


# ── TIFF encoder definitions ─────────────────────────────────────────────

def build_convert_tiff_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """ImageMagick convert — TIFF creation with compression/depth/endianness.

    convert reads PPM/PGM directly and writes TIFF with the requested
    compression, bit depth, color type, and byte order.
    """
    binary = "convert"
    encoder_id = "imagemagick-tiff"
    tasks = []
    is_gray = source["channels"] == 1

    if quick:
        compressions = ["None", "LZW", "Zip", "JPEG"]
        depths = [8]
        endians = ["LSB"]
        jpeg_qualities = [85]
    else:
        compressions = ["None", "LZW", "Zip", "JPEG", "Fax", "Group4",
                        "PackBits", "LZMA"]
        depths = [8, 16]
        endians = ["LSB", "MSB"]
        jpeg_qualities = [50, 85, 95]

    for compression in compressions:
        for depth in depths:
            # JPEG-in-TIFF only works with 8-bit RGB
            if compression == "JPEG" and (depth != 8 or is_gray):
                continue
            # Fax/Group4 are bilevel only — we convert to bilevel below
            if compression in ("Fax", "Group4") and depth != 8:
                continue

            for endian in endians:
                if compression == "JPEG":
                    q_list = jpeg_qualities
                else:
                    q_list = [None]

                for jpeg_q in q_list:
                    # Determine color type
                    if compression in ("Fax", "Group4"):
                        # Bilevel — force to grayscale bilevel
                        color_type = "Bilevel"
                    elif is_gray:
                        color_type = "Grayscale"
                    else:
                        color_type = "TrueColor"

                    cmd = [binary]
                    cmd += [source["path"]]

                    # For Fax/Group4: threshold to bilevel first
                    if compression in ("Fax", "Group4"):
                        cmd += ["-threshold", "50%"]
                        cmd += ["-type", "Bilevel"]
                    elif is_gray:
                        cmd += ["-type", "Grayscale"]
                    else:
                        cmd += ["-type", "TrueColor"]

                    cmd += ["-depth", str(depth)]
                    cmd += ["-compress", compression]
                    cmd += ["-endian", endian]

                    if jpeg_q is not None:
                        cmd += ["-quality", str(jpeg_q)]

                    cmd += ["{output}"]

                    params = {
                        "compression": compression,
                        "depth": depth,
                        "color_type": color_type,
                        "endian": endian,
                    }
                    if jpeg_q is not None:
                        params["jpeg_quality"] = jpeg_q

                    tasks.append(EncoderTask(
                        encoder_id=encoder_id,
                        binary=binary,
                        source_name=source["name"],
                        source_path=source["path"],
                        source_channels=source["channels"],
                        params=params,
                        cmd=cmd,
                    ))

    return tasks


def build_tiffcp_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """tiffcp — re-compression and layout permutations from baseline TIFF.

    tiffcp reads TIFF input only, so we mark needs_tiff_input=True to signal
    run_task_tiff to first create a baseline uncompressed TIFF via convert,
    then feed it to tiffcp.

    We store needs_tiff_input in task.env_override as a flag since
    EncoderTask does not have a dedicated field for this.
    """
    binary = env_bin("TIFFCP")
    if not binary:
        return []

    encoder_id = "libtiff-tiffcp"
    tasks = []
    is_gray = source["channels"] == 1

    if quick:
        compressions = ["lzw", "zip", "jpeg"]
        layouts = [
            {"tiled": False},
            {"tiled": True, "tile_w": 256, "tile_l": 256},
        ]
    else:
        compressions = ["none", "lzw", "zip", "jpeg", "packbits", "g3", "g4"]
        layouts = [
            {"tiled": False},
            {"tiled": False, "rows_per_strip": 1},
            {"tiled": False, "rows_per_strip": 16},
            {"tiled": True, "tile_w": 64, "tile_l": 64},
            {"tiled": True, "tile_w": 256, "tile_l": 256},
        ]

    for compression in compressions:
        # JPEG-in-TIFF only works with 8-bit RGB
        if compression == "jpeg" and is_gray:
            continue
        # Group3/Group4 require bilevel
        if compression in ("g3", "g4"):
            continue  # skip: tiffcp can't threshold to bilevel

        for layout in layouts:
            cmd = [binary]
            cmd += ["-c", compression]

            if layout.get("tiled"):
                cmd += ["-t"]
                cmd += ["-w", str(layout["tile_w"])]
                cmd += ["-l", str(layout["tile_l"])]
            elif "rows_per_strip" in layout:
                cmd += ["-r", str(layout["rows_per_strip"])]

            cmd += ["{input}", "{output}"]

            params = {
                "compression": compression,
                "tiled": layout.get("tiled", False),
            }
            if layout.get("tiled"):
                params["tile_width"] = layout["tile_w"]
                params["tile_length"] = layout["tile_l"]
            elif "rows_per_strip" in layout:
                params["rows_per_strip"] = layout["rows_per_strip"]

            tasks.append(EncoderTask(
                encoder_id=encoder_id,
                binary=binary,
                source_name=source["name"],
                source_path=source["path"],
                source_channels=source["channels"],
                params=params,
                cmd=cmd,
                env_override={"_NEEDS_TIFF_INPUT": "1"},
            ))

    return tasks


# ── HEIC encoder definitions ─────────────────────────────────────────────

def build_heif_enc_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """heif-enc from libheif — HEIC encoding with x265.

    heif-enc reads PNG input most reliably, so we set needs_png_input=True.
    """
    binary = env_bin("HEIF_ENC")
    if not binary:
        return []

    # Only encode RGB — heif-enc does not handle grayscale well
    if source["channels"] != 3:
        return []

    encoder_id = "libheif-x265"
    tasks = []

    if quick:
        qualities = [30, 70]
        lossless_opts = [False]
        bit_depths = [8]
    else:
        qualities = [10, 30, 50, 70, 85, 100]
        lossless_opts = [False, True]
        bit_depths = [8, 10]

    for lossless in lossless_opts:
        if lossless:
            # Lossless mode: quality is irrelevant, test each bit depth
            for bd in bit_depths:
                cmd = [binary]
                cmd += ["--lossless"]
                cmd += ["--bit-depth", str(bd)]
                cmd += ["{input}"]
                cmd += ["-o", "{output}"]

                params = {
                    "lossless": True,
                    "bit_depth": bd,
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
            # Lossy mode: vary quality and bit depth
            for q in qualities:
                for bd in bit_depths:
                    cmd = [binary]
                    cmd += ["-q", str(q)]
                    cmd += ["--bit-depth", str(bd)]
                    cmd += ["{input}"]
                    cmd += ["-o", "{output}"]

                    params = {
                        "lossless": False,
                        "quality": q,
                        "bit_depth": bd,
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

def run_task_tiff(task: EncoderTask, output_dir: Path) -> EncoderResult:
    """Execute a single TIFF encoding task."""
    fd, tmp_path = tempfile.mkstemp(suffix=".tiff")
    os.close(fd)
    tmp_baseline = None

    try:
        # tiffcp needs a baseline TIFF as input — create one from PPM
        needs_tiff_input = task.env_override.get("_NEEDS_TIFF_INPUT") == "1"
        input_path = task.source_path

        if needs_tiff_input and task.source_path.endswith((".ppm", ".pgm")):
            tmp_baseline = tmp_path + ".baseline.tiff"
            conv = subprocess.run(
                ["convert", task.source_path, "-compress", "None", tmp_baseline],
                capture_output=True, timeout=30,
            )
            if conv.returncode != 0:
                return EncoderResult(
                    encoder_id=task.encoder_id,
                    source_name=task.source_name,
                    params=task.params,
                    success=False,
                    error=f"Baseline TIFF conversion failed: {conv.stderr.decode()[:200]}",
                )
            input_path = tmp_baseline

        # Build the real env (strip our internal flags)
        real_env_override = {k: v for k, v in task.env_override.items()
                            if not k.startswith("_")}

        # Substitute {output} and {input} placeholders in command
        cmd = []
        for arg in task.cmd:
            if arg == "{output}":
                cmd.append(tmp_path)
            elif arg == "{input}":
                cmd.append(input_path)
            else:
                cmd.append(arg)

        env = dict(os.environ)
        env.update(real_env_override)

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,
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

        err = _validate_tiff(data)
        if err:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=err,
            )

        content_hash = hashlib.sha256(data).hexdigest()[:16]

        # Shard into tiff/<encoder>/<hash[0:2]>/<hash>.tiff
        encoder_dir = output_dir / "tiff" / task.encoder_id
        shard_dir = encoder_dir / content_hash[:2]
        shard_dir.mkdir(parents=True, exist_ok=True)

        final_path = shard_dir / f"{content_hash}.tiff"
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
            error="Timeout (300s)",
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
        for p in [tmp_path, tmp_baseline]:
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def run_task_heic(task: EncoderTask, output_dir: Path) -> EncoderResult:
    """Execute a single HEIC encoding task."""
    fd, tmp_path = tempfile.mkstemp(suffix=".heic")
    os.close(fd)
    tmp_png = None

    try:
        # heif-enc works best with PNG input
        input_path = task.source_path
        if task.needs_png_input and task.source_path.endswith((".ppm", ".pgm")):
            tmp_png = tmp_path + ".input.png"
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

        env = dict(os.environ)
        env.update(task.env_override)

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,
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

        err = _validate_heic(data)
        if err:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=err,
            )

        content_hash = hashlib.sha256(data).hexdigest()[:16]

        # Shard into heic/<encoder>/<hash[0:2]>/<hash>.heic
        encoder_dir = output_dir / "heic" / task.encoder_id
        shard_dir = encoder_dir / content_hash[:2]
        shard_dir.mkdir(parents=True, exist_ok=True)

        final_path = shard_dir / f"{content_hash}.heic"
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
            error="Timeout (300s)",
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
    "imagemagick-tiff": {
        "name": "ImageMagick convert (TIFF)",
        "version": "system",
        "binary": "convert",
        "format": "tiff",
        "source_url": "https://imagemagick.org/",
        "compile_flags": [],
    },
    "libtiff-tiffcp": {
        "name": "libtiff tiffcp",
        "version": "system",
        "binary": "tiffcp",
        "format": "tiff",
        "source_url": "http://www.libtiff.org/",
        "compile_flags": [],
    },
    "libheif-x265": {
        "name": "heif-enc (libheif + x265)",
        "version": "system",
        "binary": "heif-enc",
        "format": "heic",
        "source_url": "https://github.com/nicostruct/libheif",
        "compile_flags": [],
    },
}

TIFF_TASK_BUILDERS = [
    build_convert_tiff_tasks,
    build_tiffcp_tasks,
]

HEIC_TASK_BUILDERS = [
    build_heif_enc_tasks,
]

TASK_BUILDERS = TIFF_TASK_BUILDERS + HEIC_TASK_BUILDERS


def build_all_tasks(sources: list[dict], quick: bool) -> list[EncoderTask]:
    """Build the full task matrix from all encoders x all sources."""
    tasks = []
    for source in sources:
        for builder in TASK_BUILDERS:
            tasks.extend(builder(source, quick))
    return tasks


def _run_task_dispatch(task: EncoderTask, output_dir: Path) -> EncoderResult:
    """Dispatch a task to the appropriate runner based on encoder_id."""
    if task.encoder_id == "libheif-x265":
        return run_task_heic(task, output_dir)
    else:
        return run_task_tiff(task, output_dir)


def run_all(sources: list[dict], output_dir: Path, quick: bool = False,
            workers: int = 0) -> list[EncoderResult]:
    """Run all TIFF and HEIC encoding tasks in parallel."""
    tasks = build_all_tasks(sources, quick)
    print(f"Built {len(tasks)} TIFF/HEIC encoding tasks across "
          f"{len(sources)} sources")

    if workers <= 0:
        workers = min(os.cpu_count() or 4, 16)

    results = []
    completed = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_task_dispatch, task, output_dir): task
            for task in tasks
        }

        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1
            if not result.success:
                failed += 1
            if completed % 500 == 0 or completed == len(tasks):
                print(f"  [{completed}/{len(tasks)}] "
                      f"{failed} failed, "
                      f"{completed - failed} ok")

    # Deduplicate: collect unique hashes per format
    tiff_hashes = set()
    heic_hashes = set()
    for r in results:
        if r.success:
            if r.output_path.startswith("tiff/"):
                tiff_hashes.add(r.output_hash)
            elif r.output_path.startswith("heic/"):
                heic_hashes.add(r.output_hash)

    print(f"\nTIFF/HEIC encoding complete: {completed - failed} ok, "
          f"{failed} failed, "
          f"{len(tiff_hashes)} unique TIFF, {len(heic_hashes)} unique HEIC")

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="TIFF and HEIC encoding permutations")
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
    results_path = args.output / "tiff_heic_encoding_results.json"
    with open(results_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"Results saved to {results_path}")


if __name__ == "__main__":
    main()
