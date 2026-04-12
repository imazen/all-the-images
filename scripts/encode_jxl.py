#!/usr/bin/env python3
"""JPEG XL encoding permutation engine.

Invokes cjxl with a matrix of parameter combinations against every source
image. Deduplicates outputs by content hash, writes files into a sharded
directory tree, and records results for manifest assembly.

cjxl is from libjxl, built in a separate Docker stage. Binary path comes
from the CJXL environment variable; the decoder from DJXL.

Output layout:
    <output_dir>/jxl/<encoder-id>/<hash[0:2]>/<hash>.jxl

Deduplication: SHA-256 of file content. Identical outputs from different
parameter combos are stored once; the manifest records all parameter combos
that produced each hash.
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


# ── JXL magic bytes ───────────────────────────────────────────────────────

# Naked codestream: FF 0A
_JXL_CODESTREAM_PREFIX = b"\xFF\x0A"

# ISOBMFF container: 00 00 00 0C 4A 58 4C 20 0D 0A 87 0A
_JXL_CONTAINER_PREFIX = b"\x00\x00\x00\x0C\x4A\x58\x4C\x20\x0D\x0A\x87\x0A"


def _is_jxl(data: bytes) -> bool:
    """Check if data starts with a valid JPEG XL signature."""
    if len(data) < 2:
        return False
    if data[:2] == _JXL_CODESTREAM_PREFIX:
        return True
    if len(data) >= 12 and data[:12] == _JXL_CONTAINER_PREFIX:
        return True
    return False


# ── Encoder definitions ──────────────────────────────────────────────────

def build_cjxl_lossy_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """cjxl lossy (VarDCT) parameter permutations.

    VarDCT is the default lossy mode. It uses XYB color space by default.
    We test distance values, effort levels, progressive, and colorspace.
    """
    binary = env_bin("CJXL")
    if not binary:
        return []

    encoder_id = "cjxl-0.11.1"
    tasks = []
    is_gray = source["channels"] == 1

    # Distance: 0=lossless (handled in modular), 0.5..25 for lossy
    distances = [0.5, 1.0, 2.0, 3.0, 5.0, 8.0] if not quick else [1.0, 5.0]
    efforts = [1, 3, 5, 7, 9] if not quick else [3, 7]
    progressives = [False, True] if not quick else [False]
    # colorspace=0 is XYB (default for lossy), colorspace=1 is no-XYB
    colorspaces = [0, 1] if not quick else [0]

    for dist in distances:
        for effort in efforts:
            for prog in progressives:
                for cs in colorspaces:
                    # XYB is meaningless for grayscale
                    if is_gray and cs == 0:
                        # cjxl uses no-XYB automatically for gray,
                        # skip explicit XYB to avoid duplicates
                        continue

                    cmd = [binary]
                    cmd += ["{input}"]
                    cmd += ["{output}"]
                    cmd += ["-d", str(dist)]
                    cmd += ["-e", str(effort)]
                    if prog:
                        cmd += ["-p"]
                    if cs != 0:
                        cmd += [f"--colorspace={cs}"]

                    params = {
                        "distance": dist,
                        "effort": effort,
                        "progressive": prog,
                        "colorspace": "xyb" if cs == 0 else "native",
                        "modular": False,
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


def build_cjxl_modular_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """cjxl modular mode parameter permutations.

    Modular mode is used for lossless and can also be used for lossy.
    With -d 0, encoding is mathematically lossless.
    """
    binary = env_bin("CJXL")
    if not binary:
        return []

    encoder_id = "cjxl-0.11.1"
    tasks = []

    # Lossless (d=0) is always modular; also test modular lossy
    distances = [0, 1.0, 3.0] if not quick else [0, 1.0]
    efforts = [1, 3, 5, 7, 9] if not quick else [3, 7]
    progressives = [False, True] if not quick else [False]

    for dist in distances:
        for effort in efforts:
            for prog in progressives:
                cmd = [binary]
                cmd += ["{input}"]
                cmd += ["{output}"]
                cmd += ["-d", str(dist)]
                cmd += ["-e", str(effort)]
                cmd += ["--modular"]
                if prog:
                    cmd += ["-p"]

                params = {
                    "distance": dist,
                    "effort": effort,
                    "progressive": prog,
                    "modular": True,
                    "lossless": dist == 0,
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


# ── Task execution ───────────────────────────────────────────────────────

def run_task_jxl(task: EncoderTask, output_dir: Path) -> EncoderResult:
    """Execute a single JXL encoding task."""
    fd, tmp_path = tempfile.mkstemp(suffix=".jxl")
    os.close(fd)

    try:
        # Substitute {output} and {input} placeholders in command
        cmd = []
        for arg in task.cmd:
            if arg == "{output}":
                cmd.append(tmp_path)
            elif arg == "{input}":
                cmd.append(task.source_path)
            else:
                cmd.append(arg)

        # Use per-encoder environment if set
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

        # Read output and hash
        with open(tmp_path, "rb") as f:
            data = f.read()

        if len(data) < 2:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=f"Output too small: {len(data)} bytes",
            )

        # Verify JXL signature (codestream or container)
        if not _is_jxl(data):
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=f"Invalid JXL signature: {data[:12].hex()}",
            )

        content_hash = hashlib.sha256(data).hexdigest()[:16]

        # Shard into <encoder>/<hash[0:2]>/<hash>.jxl
        encoder_dir = output_dir / "jxl" / task.encoder_id
        shard_dir = encoder_dir / content_hash[:2]
        shard_dir.mkdir(parents=True, exist_ok=True)

        final_path = shard_dir / f"{content_hash}.jxl"
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
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Orchestration ────────────────────────────────────────────────────────

ENCODER_METADATA = {
    "cjxl-0.11.1": {
        "name": "cjxl (libjxl)",
        "version": "0.11.1",
        "binary": "cjxl",
        "source_url": "https://github.com/libjxl/libjxl",
        "compile_flags": ["JPEGXL_ENABLE_TOOLS=ON"],
    },
}


TASK_BUILDERS = [
    build_cjxl_lossy_tasks,
    build_cjxl_modular_tasks,
]


def build_all_tasks(sources: list[dict], quick: bool) -> list[EncoderTask]:
    """Build the full task matrix from all JXL encoders x all sources."""
    tasks = []
    for source in sources:
        for builder in TASK_BUILDERS:
            tasks.extend(builder(source, quick))
    return tasks


def run_all(sources: list[dict], output_dir: Path, quick: bool = False,
            workers: int = 0) -> list[EncoderResult]:
    """Run all JXL encoding tasks in parallel."""
    tasks = build_all_tasks(sources, quick)
    print(f"Built {len(tasks)} JXL encoding tasks across {len(sources)} sources")

    if workers <= 0:
        workers = min(os.cpu_count() or 4, 16)

    results = []
    completed = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_task_jxl, task, output_dir): task
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

    print(f"\nJXL encoding complete: {completed - failed} ok, {failed} failed, "
          f"{len(unique_hashes)} unique files")

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="JPEG XL encoding permutations")
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
    results_path = args.output / "jxl_encoding_results.json"
    with open(results_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"Results saved to {results_path}")


if __name__ == "__main__":
    main()
