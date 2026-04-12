#!/usr/bin/env python3
"""WebP encoding permutation engine.

Invokes cwebp with a matrix of lossy and lossless parameter combinations
against every source image. Deduplicates outputs by content hash, writes
files into a sharded directory tree, and records results for manifest assembly.

Encoders:
  - libwebp cwebp (lossy and lossless modes)

Output layout:
    <output_dir>/webp/<encoder-id>/<hash[0:2]>/<hash>.webp

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
from pathlib import Path

# Re-use shared types and helpers from encode_jpeg
from encode_jpeg import EncoderTask, EncoderResult, env_bin, _env_warned


# ── Encoder definitions ────────────────────────────────────────────────────

def build_cwebp_lossy_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """libwebp cwebp lossy parameter permutations."""
    if source.get("bit_depth", 8) > 8 or source.get("type") == "cmyk":
        return []
    binary = env_bin("CWEBP")
    if not binary:
        return []

    encoder_id = "libwebp-cwebp-lossy"
    tasks = []
    has_alpha = source["channels"] == 4

    qualities = [5, 25, 50, 75, 85, 95] if not quick else [50, 85]
    methods = [0, 2, 4, 6] if not quick else [2, 6]
    presets = ["default", "photo", "picture"] if not quick else ["default"]
    filters = [0, 25, 50] if not quick else []
    sns_values = [0, 50, 100] if not quick else []
    alpha_qualities = [0, 50, 100] if not quick else [50]

    for q in qualities:
        for m in methods:
            for preset in presets:
                if quick:
                    # Quick mode: no filter/sns axes, single pass
                    filter_combos = [(None, None)]
                    sns_combos = [None]
                else:
                    filter_combos = [(f, s) for f in filters
                                     for s in ([0, 4, 7] if f > 0 else [0])]
                    sns_combos = sns_values

                for f_val, sharp in filter_combos:
                    for sns in sns_combos:
                        if has_alpha:
                            aq_list = alpha_qualities
                        else:
                            aq_list = [None]

                        for aq in aq_list:
                            cmd = [binary]
                            cmd += ["-q", str(q)]
                            cmd += ["-m", str(m)]
                            cmd += ["-preset", preset]

                            if f_val is not None:
                                cmd += ["-f", str(f_val)]
                                cmd += ["-sharpness", str(sharp)]

                            if sns is not None:
                                cmd += ["-sns", str(sns)]

                            if aq is not None:
                                cmd += ["-alpha_q", str(aq)]

                            cmd += [source["path"]]
                            cmd += ["-o", "{output}"]

                            params = {
                                "mode": "lossy",
                                "quality": q,
                                "method": m,
                                "preset": preset,
                            }
                            if f_val is not None:
                                params["filter"] = f_val
                                params["sharpness"] = sharp
                            if sns is not None:
                                params["sns"] = sns
                            if aq is not None:
                                params["alpha_quality"] = aq

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


def build_cwebp_lossless_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """libwebp cwebp lossless parameter permutations."""
    if source.get("bit_depth", 8) > 8 or source.get("type") == "cmyk":
        return []
    binary = env_bin("CWEBP")
    if not binary:
        return []

    encoder_id = "libwebp-cwebp-lossless"
    tasks = []

    qualities = [0, 50, 100] if not quick else [0, 100]
    methods = [0, 3, 6] if not quick else [0, 6]
    near_lossless_values = [60, 100] if not quick else [100]

    for q in qualities:
        for m in methods:
            for nl in near_lossless_values:
                cmd = [binary]
                cmd += ["-lossless"]
                cmd += ["-q", str(q)]
                cmd += ["-m", str(m)]

                if nl < 100:
                    cmd += ["-near_lossless", str(nl)]

                cmd += [source["path"]]
                cmd += ["-o", "{output}"]

                params = {
                    "mode": "lossless",
                    "quality": q,
                    "method": m,
                    "near_lossless": nl,
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

def run_task_webp(task: EncoderTask, output_dir: Path) -> EncoderResult:
    """Execute a single WebP encoding task."""
    fd, tmp_path = tempfile.mkstemp(suffix=".webp")
    os.close(fd)

    try:
        # Substitute {output} placeholder in command
        cmd = []
        for arg in task.cmd:
            if arg == "{output}":
                cmd.append(tmp_path)
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

        # Read output and hash
        with open(tmp_path, "rb") as f:
            data = f.read()

        if len(data) < 12:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=f"Output too small: {len(data)} bytes",
            )

        # Verify WebP RIFF header: bytes 0-3 = "RIFF", bytes 8-11 = "WEBP"
        if data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=(f"Invalid WebP header: "
                       f"{data[0:4].hex()} ... {data[8:12].hex()}"),
            )

        content_hash = hashlib.sha256(data).hexdigest()[:16]

        # Shard into <encoder>/<hash[0:2]>/<hash>.webp
        encoder_dir = output_dir / "webp" / task.encoder_id
        shard_dir = encoder_dir / content_hash[:2]
        shard_dir.mkdir(parents=True, exist_ok=True)

        final_path = shard_dir / f"{content_hash}.webp"
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


# ── Orchestration ──────────────────────────────────────────────────────────

ENCODER_METADATA = {
    "libwebp-cwebp-lossy": {
        "name": "libwebp (lossy)",
        "version": "1.5.0",
        "binary": "cwebp",
        "source_url": "https://chromium.googlesource.com/webm/libwebp",
        "compile_flags": [],
    },
    "libwebp-cwebp-lossless": {
        "name": "libwebp (lossless)",
        "version": "1.5.0",
        "binary": "cwebp",
        "source_url": "https://chromium.googlesource.com/webm/libwebp",
        "compile_flags": [],
    },
}

TASK_BUILDERS = [
    build_cwebp_lossy_tasks,
    build_cwebp_lossless_tasks,
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
    """Run all WebP encoding tasks in parallel."""
    tasks = build_all_tasks(sources, quick)
    print(f"Built {len(tasks)} WebP encoding tasks across {len(sources)} sources")

    if workers <= 0:
        workers = min(os.cpu_count() or 4, 16)

    results = []
    completed = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_task_webp, task, output_dir): task
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

    print(f"\nWebP encoding complete: {completed - failed} ok, {failed} failed, "
          f"{len(unique_hashes)} unique files")

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="WebP encoding permutations")
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
    from dataclasses import asdict
    results_path = args.output / "webp_encoding_results.json"
    with open(results_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"Results saved to {results_path}")


if __name__ == "__main__":
    main()
