#!/usr/bin/env python3
"""GIF encoding permutation engine.

Invokes each GIF encoder with a matrix of parameter combinations against
every source image. Deduplicates outputs by content hash, writes files into
a sharded directory tree per encoder, and records results for manifest assembly.

GIF is limited to 256 colors maximum, so all sources are quantized during
encoding. Grayscale sources are represented via a palette.

Output layout:
    <output_dir>/gif/<encoder-id>/<hash[0:2]>/<hash>.gif

Encoders:
  - gifsicle: GIF optimizer (requires GIF input, so PPM is pre-converted
    via ImageMagick convert, then gifsicle optimizes the intermediate GIF)
  - ImageMagick convert: direct PPM-to-GIF conversion with dither/color control
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Import shared types from encode_jpeg
sys.path.insert(0, str(Path(__file__).parent))
from encode_jpeg import EncoderTask, EncoderResult, env_bin, _env_warned


# ── Encoder definitions ────────────────────────────────────────────────────

ENCODER_METADATA = {
    "gifsicle-1.95": {
        "name": "gifsicle",
        "version": "1.95",
        "binary": "gifsicle",
        "source_url": "https://github.com/kohler/gifsicle",
        "compile_flags": [],
    },
    "imagemagick-gif": {
        "name": "ImageMagick convert",
        "version": "system",
        "binary": "convert",
        "source_url": "https://imagemagick.org/",
        "compile_flags": [],
    },
}


def build_gifsicle_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """gifsicle parameter permutations.

    gifsicle is a GIF optimizer, not a converter. The flow is:
      PPM -> (ImageMagick convert) -> temp.gif -> gifsicle -> output.gif

    The pre-conversion step is handled inside run_task_gif when it detects
    a PPM/PGM source. The cmd here operates on {input} which will be
    a pre-converted GIF.
    """
    # GIF is 8-bit palette only
    if source.get("bit_depth", 8) > 8:
        return []
    binary = env_bin("GIFSICLE")
    if not binary:
        return []

    encoder_id = "gifsicle-1.95"
    tasks = []

    opt_levels = [1, 2, 3] if not quick else [1, 3]
    color_counts = [2, 16, 64, 256] if not quick else [64, 256]
    lossy_values = [0, 20, 80] if not quick else [0, 20]
    dithers = [True, False] if not quick else [True]
    interlaces = [True, False] if not quick else [False]

    for opt in opt_levels:
        for colors in color_counts:
            for lossy in lossy_values:
                for dither in dithers:
                    for interlace in interlaces:
                        cmd = [binary]
                        cmd += [f"-O{opt}"]
                        cmd += ["--colors", str(colors)]
                        if lossy > 0:
                            cmd += [f"--lossy={lossy}"]
                        if dither:
                            cmd += ["--dither"]
                        else:
                            cmd += ["--no-dither"]
                        if interlace:
                            cmd += ["--interlace"]
                        else:
                            cmd += ["--no-interlace"]
                        cmd += ["{input}"]
                        cmd += ["-o", "{output}"]

                        params = {
                            "optimization": opt,
                            "colors": colors,
                            "lossy": lossy,
                            "dither": dither,
                            "interlace": interlace,
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


def build_imagemagick_gif_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """ImageMagick convert PPM-to-GIF parameter permutations.

    ImageMagick can read PPM directly and output GIF with various
    quantization and dithering options.
    """
    if source.get("bit_depth", 8) > 8:
        return []
    binary = "convert"  # ImageMagick convert is expected in PATH
    encoder_id = "imagemagick-gif"
    tasks = []

    color_counts = [2, 16, 64, 128, 256] if not quick else [64, 256]

    # Standard dithering options
    standard_dithers = ["None", "Floyd-Steinberg", "Riemersma"] if not quick else ["None", "Floyd-Steinberg"]

    # Ordered dither patterns (used with -ordered-dither, mutually exclusive
    # with -dither)
    ordered_dithers = (
        ["threshold", "checks", "o2x2", "o3x3", "o4x4", "o8x8"]
        if not quick else []
    )

    for colors in color_counts:
        # Standard dithering modes
        for dither in standard_dithers:
            cmd = [binary]
            cmd += [source["path"]]
            cmd += ["-colors", str(colors)]
            cmd += ["-dither", dither]
            cmd += ["{output}"]

            params = {
                "colors": colors,
                "dither": dither,
                "ordered_dither": None,
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

        # Ordered dithering modes
        for od in ordered_dithers:
            cmd = [binary]
            cmd += [source["path"]]
            cmd += ["-colors", str(colors)]
            cmd += ["-ordered-dither", od]
            cmd += ["{output}"]

            params = {
                "colors": colors,
                "dither": None,
                "ordered_dither": od,
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


# ── Task execution ─────────────────────────────────────────────────────────

def run_task_gif(task: EncoderTask, output_dir: Path) -> EncoderResult:
    """Execute a single GIF encoding task.

    For gifsicle tasks, the source PPM/PGM is first pre-converted to a
    temporary GIF via ImageMagick convert, since gifsicle only accepts
    GIF input. The {input} placeholder in the command is then replaced
    with the path to that intermediate GIF.

    For ImageMagick tasks, the source PPM/PGM is read directly.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".gif")
    os.close(fd)
    tmp_gif_input = None

    try:
        input_path = task.source_path

        # gifsicle needs GIF input -- pre-convert PPM/PGM via ImageMagick
        is_gifsicle = task.encoder_id.startswith("gifsicle")
        if is_gifsicle and task.source_path.endswith((".ppm", ".pgm")):
            tmp_gif_input = tmp_path + ".input.gif"
            conv = subprocess.run(
                ["convert", task.source_path, tmp_gif_input],
                capture_output=True, timeout=60,
            )
            if conv.returncode != 0:
                return EncoderResult(
                    encoder_id=task.encoder_id,
                    source_name=task.source_name,
                    params=task.params,
                    success=False,
                    error=f"GIF pre-conversion failed: {conv.stderr.decode()[:200]}",
                )
            input_path = tmp_gif_input

        # Substitute {output} and {input} placeholders in command
        cmd = []
        for arg in task.cmd:
            if arg == "{output}":
                cmd.append(tmp_path)
            elif arg == "{input}":
                cmd.append(input_path)
            else:
                cmd.append(arg)

        # Use per-encoder environment if provided
        env = dict(os.environ)
        env.update(task.env_override)

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
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

        if len(data) < 6:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=f"Output too small: {len(data)} bytes",
            )

        # Validate GIF signature: "GIF87a" or "GIF89a"
        sig = data[:6]
        if sig not in (b"GIF87a", b"GIF89a"):
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=f"Invalid GIF signature: {sig!r}",
            )

        content_hash = hashlib.sha256(data).hexdigest()[:16]

        # Shard into <encoder>/<hash[0:2]>/<hash>.gif
        encoder_dir = output_dir / "gif" / task.encoder_id
        shard_dir = encoder_dir / content_hash[:2]
        shard_dir.mkdir(parents=True, exist_ok=True)

        final_path = shard_dir / f"{content_hash}.gif"
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
            error="Timeout (120s)",
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
        for p in [tmp_path, tmp_gif_input]:
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ── Orchestration ──────────────────────────────────────────────────────────

TASK_BUILDERS = [
    build_gifsicle_tasks,
    build_imagemagick_gif_tasks,
]


def build_all_tasks(sources: list[dict], quick: bool) -> list[EncoderTask]:
    """Build the full task matrix from all GIF encoders x all sources."""
    tasks = []
    for source in sources:
        for builder in TASK_BUILDERS:
            tasks.extend(builder(source, quick))
    return tasks


def run_all(sources: list[dict], output_dir: Path, quick: bool = False,
            workers: int = 0) -> list[EncoderResult]:
    """Run all GIF encoding tasks in parallel."""
    tasks = build_all_tasks(sources, quick)
    print(f"Built {len(tasks)} GIF encoding tasks across {len(sources)} sources")

    if workers <= 0:
        workers = min(os.cpu_count() or 4, 16)

    results = []
    completed = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_task_gif, task, output_dir): task
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

    print(f"\nGIF encoding complete: {completed - failed} ok, {failed} failed, "
          f"{len(unique_hashes)} unique files")

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GIF encoding permutations")
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
    results_path = args.output / "gif_encoding_results.json"
    with open(results_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"Results saved to {results_path}")


if __name__ == "__main__":
    main()
