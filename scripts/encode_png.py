#!/usr/bin/env python3
"""PNG encoding permutation engine.

Invokes each PNG encoder/optimizer with a matrix of parameter combinations
against every source image. Deduplicates outputs by content hash, writes files
into a sharded directory tree per encoder, and records results for manifest
assembly.

Encoder binaries come from environment variables (set in the Dockerfile).
ImageMagick `convert` is assumed to be on PATH.

Output layout:
    <output_dir>/png/<encoder-id>/<hash[0:2]>/<hash>.png

Deduplication: SHA-256 of file content. Identical outputs from different
parameter combos are stored once; the manifest records all parameter combos
that produced each hash.

Encoders:
  - ImageMagick convert (baseline PNG creation with depth/color/interlace/quality)
  - optipng (optimization levels, interlace, strip)
  - pngcrush (brute-force, method, filter, chunk removal)
  - zopflipng (iterations, filter strategies, lossy modes)
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from encode_jpeg import EncoderTask, EncoderResult, env_bin


# PNG signature: 8 bytes
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# ── Encoder definitions ────────────────────────────────────────────────────

# Encoder metadata for build_manifest.py integration
ENCODER_METADATA = {
    "imagemagick-convert": {
        "name": "ImageMagick convert",
        "version": "system",
        "binary": "convert",
        "source_url": "https://imagemagick.org/",
        "compile_flags": [],
    },
    "optipng": {
        "name": "OptiPNG",
        "version": "system",
        "binary": "optipng",
        "source_url": "https://optipng.sourceforge.net/",
        "compile_flags": [],
    },
    "pngcrush": {
        "name": "pngcrush",
        "version": "system",
        "binary": "pngcrush",
        "source_url": "https://pmt.sourceforge.io/pngcrush/",
        "compile_flags": [],
    },
    "zopflipng": {
        "name": "zopflipng",
        "version": "system",
        "binary": "zopflipng",
        "source_url": "https://github.com/google/zopfli",
        "compile_flags": [],
    },
}


def build_imagemagick_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """ImageMagick convert — baseline PNG creation with explicit settings.

    convert reads PPM/PGM directly, no intermediate PNG needed.
    Parameters: bit depth, color type, interlace, compression quality (zlib level).
    """
    binary = "convert"
    encoder_id = "imagemagick-convert"
    tasks = []
    is_gray = source["channels"] == 1

    depths = [8, 16] if quick else [1, 2, 4, 8, 16]
    qualities = [0, 50, 100] if quick else [0, 10, 50, 75, 100]
    interlaces = ["None", "Line"] if quick else ["None", "Line", "Plane"]

    if is_gray:
        color_types = ["Grayscale"] if quick else ["Grayscale"]
    else:
        color_types = ["TrueColor", "Palette"] if quick else [
            "TrueColor", "Palette",
        ]

    for depth in depths:
        for color_type in color_types:
            # Palette mode only makes sense at depth <= 8
            if color_type == "Palette" and depth > 8:
                continue
            # Depth 1/2/4 only work with Grayscale or Palette
            if depth < 8 and color_type not in ("Grayscale", "Palette"):
                continue
            # 16-bit palette is not valid in PNG
            if color_type == "Palette" and depth == 16:
                continue

            for quality_val in qualities:
                for interlace in interlaces:
                    cmd = [binary]
                    cmd += [source["path"]]
                    cmd += ["-depth", str(depth)]
                    cmd += ["-type", color_type]
                    cmd += ["-interlace", interlace]
                    # ImageMagick PNG quality: tens digit = zlib level,
                    # units digit = filter type (0-5). We vary both via
                    # the single -quality parameter.
                    cmd += ["-quality", str(quality_val)]
                    cmd += ["{output}"]

                    params = {
                        "depth": depth,
                        "color_type": color_type,
                        "interlace": interlace,
                        "quality": quality_val,
                    }

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


def build_optipng_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """OptiPNG optimization permutations.

    optipng reads PNG input, so needs_png_input=True for PPM sources.
    Parameters: optimization level (-o0 to -o7), interlace, strip metadata.
    """
    binary = env_bin("OPTIPNG")
    if not binary:
        return []

    encoder_id = "optipng"
    tasks = []

    opt_levels = [1, 3, 7] if quick else [0, 1, 2, 3, 4, 5, 6, 7]
    interlaces = [False] if quick else [False, True]
    strips = [True] if quick else [False, True]

    for level in opt_levels:
        for interlace in interlaces:
            for strip in strips:
                cmd = [binary]
                cmd += [f"-o{level}"]
                if interlace:
                    cmd += ["-i", "1"]
                else:
                    cmd += ["-i", "0"]
                if strip:
                    cmd += ["-strip", "all"]
                # optipng overwrites in place by default; use -out for output
                cmd += ["-out", "{output}"]
                cmd += ["-force"]  # overwrite output if exists
                cmd += ["{input}"]

                params = {
                    "optimization_level": level,
                    "interlace": interlace,
                    "strip": strip,
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


def build_pngcrush_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """pngcrush optimization permutations.

    pngcrush reads PNG input, so needs_png_input=True for PPM sources.
    Parameters: brute-force, method, filter, text chunk removal.
    """
    binary = env_bin("PNGCRUSH")
    if not binary:
        return []

    encoder_id = "pngcrush"
    tasks = []

    if quick:
        # Quick mode: just a few representative combos
        combos = [
            {"method": 1, "filter": None, "brute": False, "rem_text": True},
            {"method": 7, "filter": None, "brute": False, "rem_text": True},
            {"method": None, "filter": None, "brute": True, "rem_text": True},
        ]
    else:
        # Full mode: method x filter matrix, plus brute-force
        combos = []
        methods = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        filters = [0, 1, 2, 3, 4, 5]
        rem_texts = [False, True]

        # Method/filter grid (skip filter variation for brute-force)
        for method in methods:
            for filt in filters:
                for rem_text in rem_texts:
                    combos.append({
                        "method": method,
                        "filter": filt,
                        "brute": False,
                        "rem_text": rem_text,
                    })

        # Brute-force mode (tries all methods/filters internally)
        for rem_text in rem_texts:
            combos.append({
                "method": None,
                "filter": None,
                "brute": True,
                "rem_text": rem_text,
            })

    for combo in combos:
        cmd = [binary]
        if combo["brute"]:
            cmd += ["-brute"]
        else:
            if combo["method"] is not None:
                cmd += ["-m", str(combo["method"])]
            if combo["filter"] is not None:
                cmd += ["-f", str(combo["filter"])]
        if combo["rem_text"]:
            cmd += ["-rem", "text"]
        # pngcrush: input output (positional)
        cmd += ["{input}", "{output}"]

        params = {
            "brute": combo["brute"],
            "method": combo["method"],
            "filter": combo["filter"],
            "rem_text": combo["rem_text"],
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


def build_zopflipng_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """zopflipng optimization permutations.

    zopflipng reads PNG input, so needs_png_input=True for PPM sources.
    Parameters: iterations, filter strategies, lossy modes.
    """
    binary = env_bin("ZOPFLIPNG")
    if not binary:
        return []

    encoder_id = "zopflipng"
    tasks = []

    iterations_list = [1, 5] if quick else [1, 5, 15]
    # Filter strategies: 0=none, 1=minimum-sum, 2=entropy, 3=predefined,
    # 4=brute-force, m=minimum-sum, e=entropy, p=predefined, b=brute-force
    filter_strategies = ["0", "1", "m", "e"] if quick else [
        "0", "1", "2", "3", "4", "m", "e", "p", "b",
    ]
    lossy_transparent_opts = [False] if quick else [False, True]
    lossy_8bit_opts = [False] if quick else [False, True]

    is_gray = source["channels"] == 1

    for iters in iterations_list:
        for filters in filter_strategies:
            for lossy_transparent in lossy_transparent_opts:
                for lossy_8bit in lossy_8bit_opts:
                    # lossy_transparent only matters for RGBA/GA (not our
                    # PPM/PGM sources which lack alpha), but include for
                    # completeness — zopflipng will just ignore it
                    # lossy_8bit reduces 16-bit to 8-bit — skip for gray
                    # sources at 8-bit since it's a no-op
                    cmd = [binary]
                    cmd += [f"--iterations={iters}"]
                    cmd += [f"--filters={filters}"]
                    if lossy_transparent:
                        cmd += ["--lossy_transparent"]
                    if lossy_8bit:
                        cmd += ["--lossy_8bit"]
                    cmd += ["-y"]  # overwrite output without asking
                    # zopflipng: input output (positional)
                    cmd += ["{input}", "{output}"]

                    params = {
                        "iterations": iters,
                        "filters": filters,
                        "lossy_transparent": lossy_transparent,
                        "lossy_8bit": lossy_8bit,
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

def run_task_png(task: EncoderTask, output_dir: Path) -> EncoderResult:
    """Execute a single PNG encoding task."""
    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    tmp_png_input = None

    try:
        # Convert PPM/PGM to PNG for encoders that need PNG input
        input_path = task.source_path
        if task.needs_png_input and task.source_path.endswith((".ppm", ".pgm")):
            tmp_png_input = tmp_path + ".input.png"
            conv = subprocess.run(
                ["convert", task.source_path, tmp_png_input],
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
            input_path = tmp_png_input

        # Substitute {output} and {input} placeholders in command
        cmd = []
        for arg in task.cmd:
            if arg == "{output}":
                cmd.append(tmp_path)
            elif arg == "{input}":
                cmd.append(input_path)
            else:
                cmd.append(arg)

        # Use per-encoder environment to avoid library symbol conflicts
        env = dict(os.environ)
        env.update(task.env_override)

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,  # 5 min timeout
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

        # Read output and hash
        with open(tmp_path, "rb") as f:
            data = f.read()

        if len(data) < 8:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=f"Output too small: {len(data)} bytes",
            )

        # Verify PNG signature
        if data[:8] != _PNG_SIGNATURE:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=f"Invalid PNG signature: {data[:8].hex()}",
            )

        content_hash = hashlib.sha256(data).hexdigest()[:16]

        # Shard into <encoder>/<hash[0:2]>/<hash>.png
        encoder_dir = output_dir / "png" / task.encoder_id
        shard_dir = encoder_dir / content_hash[:2]
        shard_dir.mkdir(parents=True, exist_ok=True)

        final_path = shard_dir / f"{content_hash}.png"
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
        for p in [tmp_path, tmp_png_input]:
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ── Orchestration ──────────────────────────────────────────────────────────

TASK_BUILDERS = [
    build_imagemagick_tasks,
    build_optipng_tasks,
    build_pngcrush_tasks,
    build_zopflipng_tasks,
]


def build_all_tasks(sources: list[dict], quick: bool) -> list[EncoderTask]:
    """Build the full task matrix from all encoders x all sources."""
    tasks = []
    for source in sources:
        for builder in TASK_BUILDERS:
            tasks.extend(builder(source, quick))
    return tasks


def run_all(sources: list[dict], output_dir: Path, quick: bool = False,
            workers: int = 0) -> list[EncoderResult]:
    """Run all PNG encoding tasks in parallel."""
    tasks = build_all_tasks(sources, quick)
    print(f"Built {len(tasks)} PNG encoding tasks across {len(sources)} sources")

    if workers <= 0:
        workers = min(os.cpu_count() or 4, 16)

    results = []
    completed = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_task_png, task, output_dir): task
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

    # Deduplicate: collect unique hashes
    unique_hashes = set()
    for r in results:
        if r.success:
            unique_hashes.add(r.output_hash)

    print(f"\nPNG encoding complete: {completed - failed} ok, {failed} failed, "
          f"{len(unique_hashes)} unique files")

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PNG encoding permutations")
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
    results_path = args.output / "png_encoding_results.json"
    with open(results_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"Results saved to {results_path}")


if __name__ == "__main__":
    main()
