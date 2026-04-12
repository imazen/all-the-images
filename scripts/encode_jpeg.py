#!/usr/bin/env python3
"""JPEG encoding permutation engine.

Invokes each JPEG encoder with a matrix of parameter combinations against
every source image. Deduplicates outputs by content hash, writes files into
a sharded directory tree per encoder, and records results for manifest assembly.

Each encoder is invoked via its fully-qualified path from environment variables
set in the Dockerfile. This avoids $PATH conflicts between libjpeg variants.

Output layout:
    <output_dir>/jpeg/<encoder-id>/<hash[0:2]>/<hash>.jpg

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


@dataclass
class EncoderTask:
    """A single encoding invocation."""
    encoder_id: str
    binary: str
    source_name: str
    source_path: str
    source_channels: int
    params: dict
    cmd: list[str]
    output_path: str = ""  # filled by runner
    env_override: dict = field(default_factory=dict)  # per-encoder LD_LIBRARY_PATH
    needs_png_input: bool = False  # guetzli needs PNG, not PPM


@dataclass
class EncoderResult:
    """Result of a single encoding invocation."""
    encoder_id: str
    source_name: str
    params: dict
    success: bool
    output_hash: str = ""
    output_bytes: int = 0
    output_path: str = ""
    error: str = ""
    expect_fail: bool = False


# ── Encoder definitions ────────────────────────────────────────────────────

_env_warned: set[str] = set()

def env_bin(name: str) -> str:
    """Get encoder binary path from environment."""
    path = os.environ.get(name, "")
    if not path and name not in _env_warned:
        _env_warned.add(name)
        print(f"  [skip] {name} not set in environment", file=sys.stderr)
    return path


def _build_cjpeg_turbo_version_tasks(source: dict, quick: bool,
                                      env_name: str, encoder_id: str,
                                      lib_path: str) -> list[EncoderTask]:
    """libjpeg-turbo cjpeg parameter permutations for a single version."""
    # JPEG is 8-bit only — skip 16-bit and HDR sources
    if source.get("bit_depth", 8) > 8:
        return []
    binary = env_bin(env_name)
    if not binary:
        return []

    tasks = []
    turbo_env = {"LD_LIBRARY_PATH": lib_path}

    qualities = [1, 5, 15, 35, 55, 75, 85, 92, 97, 100] if not quick else [15, 75, 97]
    subsampling_rgb = ["1x1", "2x1", "1x2", "2x2", "4x1"] if not quick else ["1x1", "2x2"]
    progressives = [False, True] if not quick else [False]
    optimizes = [False, True] if not quick else [True]
    restarts = [0, 1, 8] if not quick else [0]
    dcts = ["int", "fast", "float"] if not quick else ["int"]
    arithmetics = [False, True] if not quick else [False]

    is_gray = source["channels"] == 1
    subs = ["1x1"] if is_gray else subsampling_rgb

    for q in qualities:
        for sub in subs:
            for prog in progressives:
                for opt in optimizes:
                    for rst in restarts:
                        for dct in dcts:
                            for arith in arithmetics:
                                # Arithmetic and optimize are mutually exclusive
                                if arith and opt:
                                    continue
                                # Arithmetic and progressive interact oddly at
                                # very low quality — skip to reduce noise
                                if arith and q < 10:
                                    continue

                                cmd = [binary]
                                cmd += ["-quality", str(q)]
                                if is_gray:
                                    cmd += ["-grayscale"]
                                else:
                                    cmd += ["-sample", sub]
                                if prog:
                                    cmd += ["-progressive"]
                                if opt and not arith:
                                    cmd += ["-optimize"]
                                if arith:
                                    cmd += ["-arithmetic"]
                                if rst > 0:
                                    cmd += ["-restart", f"{rst}B"]
                                cmd += ["-dct", dct]
                                cmd += ["-outfile", "{output}"]
                                cmd += [source["path"]]

                                sub_label = sub if not is_gray else "gray"
                                params = {
                                    "quality": q,
                                    "subsampling": sub_label,
                                    "progressive": prog,
                                    "optimize": opt,
                                    "arithmetic": arith,
                                    "restart": rst,
                                    "dct": dct,
                                }

                                tasks.append(EncoderTask(
                                    encoder_id=encoder_id,
                                    binary=binary,
                                    source_name=source["name"],
                                    source_path=source["path"],
                                    source_channels=source["channels"],
                                    params=params,
                                    cmd=cmd,
                                    env_override=turbo_env,
                                ))
    return tasks


def build_cjpeg_turbo_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """libjpeg-turbo 3.1.0 — latest upstream."""
    return _build_cjpeg_turbo_version_tasks(
        source, quick, "CJPEG_TURBO", "libjpeg-turbo-3.1.0",
        "/opt/libjpeg-turbo-3.1.0/lib")


def build_cjpeg_turbo_1_3_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """libjpeg-turbo 1.3.0 — Ubuntu 14.04 Trusty."""
    return _build_cjpeg_turbo_version_tasks(
        source, quick, "CJPEG_TURBO_1_3", "libjpeg-turbo-1.3.0",
        "/opt/libjpeg-turbo-1.3.0/lib")


def build_cjpeg_turbo_2_0_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """libjpeg-turbo 2.0.3 — Ubuntu 20.04 Focal."""
    return _build_cjpeg_turbo_version_tasks(
        source, quick, "CJPEG_TURBO_2_0", "libjpeg-turbo-2.0.3",
        "/opt/libjpeg-turbo-2.0.3/lib")


def build_cjpeg_turbo_2_1_2_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """libjpeg-turbo 2.1.2 — Ubuntu 22.04 Jammy."""
    return _build_cjpeg_turbo_version_tasks(
        source, quick, "CJPEG_TURBO_2_1_2", "libjpeg-turbo-2.1.2",
        "/opt/libjpeg-turbo-2.1.2/lib")


def build_cjpeg_turbo_2_1_5_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """libjpeg-turbo 2.1.5 — Ubuntu 24.04 Noble."""
    return _build_cjpeg_turbo_version_tasks(
        source, quick, "CJPEG_TURBO_2_1_5", "libjpeg-turbo-2.1.5",
        "/opt/libjpeg-turbo-2.1.5/lib")


def _build_cjpeg_ijg_version_tasks(source: dict, quick: bool,
                                    env_name: str, encoder_id: str,
                                    lib_path: str,
                                    has_block_sizes: bool = True,
                                    has_arithmetic: bool = True,
                                    ) -> list[EncoderTask]:
    """IJG libjpeg parameter permutations for a single version.

    v6b: no arithmetic, no block sizes, no progressive in cjpeg
    v9+: arithmetic coding, block sizes 1-16, RGB identity encoding
    """
    if source.get("bit_depth", 8) > 8:
        return []
    binary = env_bin(env_name)
    if not binary:
        return []

    tasks = []
    ijg_env = {"LD_LIBRARY_PATH": lib_path}
    is_gray = source["channels"] == 1

    qualities = [10, 50, 85, 97] if not quick else [50, 85]
    blocks = ([8, 1, 16] if has_block_sizes else [8]) if not quick else [8]
    arithmetics = ([False, True] if has_arithmetic else [False]) if not quick else [False]
    progressives = [False, True] if not quick else [False]

    for q in qualities:
        for block in blocks:
            for arith in arithmetics:
                for prog in progressives:
                    cmd = [binary]
                    cmd += ["-quality", str(q)]
                    if is_gray:
                        cmd += ["-grayscale"]
                    if block != 8:
                        cmd += ["-block", str(block)]
                    if arith:
                        cmd += ["-arithmetic"]
                    if prog:
                        cmd += ["-progressive"]
                    cmd += ["-outfile", "{output}"]
                    cmd += [source["path"]]

                    params = {
                        "quality": q,
                        "block_size": block,
                        "arithmetic": arith,
                        "progressive": prog,
                    }

                    tasks.append(EncoderTask(
                        encoder_id=encoder_id,
                        binary=binary,
                        source_name=source["name"],
                        source_path=source["path"],
                        source_channels=source["channels"],
                        params=params,
                        cmd=cmd,
                        env_override=ijg_env,
                    ))
    return tasks


def build_cjpeg_ijg6b_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """IJG libjpeg v6b — Ubuntu 14.04, the ancient baseline."""
    return _build_cjpeg_ijg_version_tasks(
        source, quick, "CJPEG_IJG6B", "libjpeg-6b", "/opt/libjpeg-6b/lib",
        has_block_sizes=False, has_arithmetic=False)


def build_cjpeg_ijg9b_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """IJG libjpeg v9b — Ubuntu 16.04/18.04."""
    return _build_cjpeg_ijg_version_tasks(
        source, quick, "CJPEG_IJG9B", "libjpeg-9b", "/opt/libjpeg-9b/lib")


def build_cjpeg_ijg9d_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """IJG libjpeg v9d — Ubuntu 20.04/22.04."""
    return _build_cjpeg_ijg_version_tasks(
        source, quick, "CJPEG_IJG9D", "libjpeg-9d", "/opt/libjpeg-9d/lib")


def build_cjpeg_ijg10_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """IJG libjpeg v10 — released 2026-01-25, not yet in any Ubuntu."""
    return _build_cjpeg_ijg_version_tasks(
        source, quick, "CJPEG_IJG10", "libjpeg-10", "/opt/libjpeg-10/lib")


def build_mozjpeg_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """mozjpeg parameter permutations."""
    if source.get("bit_depth", 8) > 8:
        return []
    binary = env_bin("CJPEG_MOZ")
    if not binary:
        return []

    encoder_id = "mozjpeg-4.1.5"
    tasks = []
    is_gray = source["channels"] == 1

    # mozjpeg needs its own library path to avoid symbol conflicts with
    # libjpeg-turbo or IJG libjpeg (jpeg_c_set_int_param is IJG-only)
    moz_env = {"LD_LIBRARY_PATH": "/opt/mozjpeg-4.1.5/lib64:/opt/mozjpeg-4.1.5/lib"}

    qualities = [5, 30, 55, 75, 85, 92, 97] if not quick else [55, 85]
    subsampling_rgb = ["1x1", "2x2"] if not quick else ["2x2"]
    arithmetics = [False, True] if not quick else [False]

    subs = ["1x1"] if is_gray else subsampling_rgb

    for q in qualities:
        for sub in subs:
            for arith in arithmetics:
                cmd = [binary]
                cmd += ["-quality", str(q)]
                if is_gray:
                    cmd += ["-grayscale"]
                else:
                    cmd += ["-sample", sub]
                if arith:
                    cmd += ["-arithmetic"]
                # mozjpeg defaults to progressive + optimize, which is what
                # we want. Add -baseline to test non-progressive too.
                for baseline in ([False, True] if not quick else [False]):
                    actual_cmd = list(cmd)
                    if baseline:
                        actual_cmd += ["-baseline"]
                    actual_cmd += ["-outfile", "{output}"]
                    actual_cmd += [source["path"]]

                    sub_label = sub if not is_gray else "gray"
                    params = {
                        "quality": q,
                        "subsampling": sub_label,
                        "arithmetic": arith,
                        "baseline": baseline,
                    }

                    tasks.append(EncoderTask(
                        encoder_id=encoder_id,
                        binary=binary,
                        source_name=source["name"],
                        source_path=source["path"],
                        source_channels=source["channels"],
                        params=params,
                        cmd=actual_cmd,
                        env_override=moz_env,
                    ))
    return tasks


def build_cjpegli_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """jpegli (cjpegli) parameter permutations.

    jpegli uses distance-based quality (butteraugli distance) and supports
    XYB colorspace, adaptive quantization, and progressive levels 0-2.
    """
    binary = env_bin("CJPEGLI")
    if not binary:
        return []

    encoder_id = "jpegli-0.11.1"
    tasks = []
    is_gray = source["channels"] == 1

    # Distance values (lower = higher quality)
    distances = [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0] if not quick else [1.0, 5.0]
    subsampling_rgb = ["444", "422", "420"] if not quick else ["444", "420"]
    prog_levels = [0, 1, 2] if not quick else [0, 2]

    subs = ["444"] if is_gray else subsampling_rgb

    for dist in distances:
        for sub in subs:
            for plevel in prog_levels:
                # Standard mode
                cmd = [binary]
                cmd += [source["path"]]
                cmd += ["{output}"]
                cmd += ["-d", str(dist)]
                cmd += ["--chroma_subsampling=" + sub]
                cmd += ["-p", str(plevel)]

                params = {
                    "distance": dist,
                    "subsampling": sub,
                    "progressive_level": plevel,
                    "xyb": False,
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

                # XYB mode (RGB only, skip high distances where XYB is pointless)
                if not is_gray and dist <= 5.0:
                    cmd_xyb = [binary]
                    cmd_xyb += [source["path"]]
                    cmd_xyb += ["{output}"]
                    cmd_xyb += ["-d", str(dist)]
                    cmd_xyb += ["--chroma_subsampling=" + sub]
                    cmd_xyb += ["-p", str(plevel)]
                    cmd_xyb += ["-x", "color_space=xyb"]

                    params_xyb = {
                        "distance": dist,
                        "subsampling": sub,
                        "progressive_level": plevel,
                        "xyb": True,
                    }

                    tasks.append(EncoderTask(
                        encoder_id=encoder_id,
                        binary=binary,
                        source_name=source["name"],
                        source_path=source["path"],
                        source_channels=source["channels"],
                        params=params_xyb,
                        cmd=cmd_xyb,
                    ))

    return tasks


def build_guetzli_tasks(source: dict, quick: bool) -> list[EncoderTask]:
    """guetzli parameter permutations.

    Guetzli is VERY slow (minutes per image). Only used on small sources
    with limited quality levels. Minimum quality is 84.
    """
    if source.get("bit_depth", 8) > 8:
        return []
    binary = env_bin("GUETZLI")
    if not binary:
        return []

    # Only encode small images — guetzli is O(n^2) in pixels
    w, h = source.get("w", 0), source.get("h", 0)
    if w * h > 64 * 64:
        return []

    # Guetzli only handles RGB, not grayscale
    if source["channels"] != 3:
        return []

    encoder_id = "guetzli-1.0.1"
    tasks = []

    qualities = [84, 90, 97] if not quick else [90]

    for q in qualities:
        cmd = [binary]
        cmd += ["--quality", str(q)]
        cmd += ["{input}"]  # guetzli needs PNG, not PPM
        cmd += ["{output}"]

        params = {"quality": q}

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

def run_task(task: EncoderTask, output_dir: Path) -> EncoderResult:
    """Execute a single encoding task."""
    # Create temp file for output
    fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    tmp_png = None

    try:
        # Convert PPM/PGM to PNG for encoders that need it (guetzli)
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

        # Use per-encoder environment to avoid library symbol conflicts
        env = dict(os.environ)
        env.update(task.env_override)

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,  # 5 min timeout (guetzli is slow)
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

        if len(data) < 4:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=f"Output too small: {len(data)} bytes",
            )

        # Verify JPEG SOI marker
        if data[0] != 0xFF or data[1] != 0xD8:
            return EncoderResult(
                encoder_id=task.encoder_id,
                source_name=task.source_name,
                params=task.params,
                success=False,
                error=f"Invalid SOI: {data[0]:02x} {data[1]:02x}",
            )

        content_hash = hashlib.sha256(data).hexdigest()[:16]

        # Shard into <encoder>/<hash[0:2]>/<hash>.jpg
        encoder_dir = output_dir / "jpeg" / task.encoder_id
        shard_dir = encoder_dir / content_hash[:2]
        shard_dir.mkdir(parents=True, exist_ok=True)

        final_path = shard_dir / f"{content_hash}.jpg"
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

TASK_BUILDERS = [
    # libjpeg-turbo versions (Ubuntu LTS history + latest)
    build_cjpeg_turbo_1_3_tasks,   # Ubuntu 14.04
    build_cjpeg_turbo_2_0_tasks,   # Ubuntu 20.04
    build_cjpeg_turbo_2_1_2_tasks, # Ubuntu 22.04
    build_cjpeg_turbo_2_1_5_tasks, # Ubuntu 24.04
    build_cjpeg_turbo_tasks,       # 3.1.0 (latest)
    # IJG libjpeg versions
    build_cjpeg_ijg6b_tasks,       # Ubuntu 14.04
    build_cjpeg_ijg9b_tasks,       # Ubuntu 16.04/18.04
    build_cjpeg_ijg9d_tasks,       # Ubuntu 20.04/22.04
    build_cjpeg_ijg10_tasks,       # latest (2026-01-25)
    # Other JPEG encoders
    build_mozjpeg_tasks,
    build_cjpegli_tasks,
    build_guetzli_tasks,
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
    """Run all encoding tasks in parallel."""
    tasks = build_all_tasks(sources, quick)
    print(f"Built {len(tasks)} encoding tasks across {len(sources)} sources")

    if workers <= 0:
        workers = min(os.cpu_count() or 4, 16)

    results = []
    completed = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_task, task, output_dir): task
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

    print(f"\nEncoding complete: {completed - failed} ok, {failed} failed, "
          f"{len(unique_hashes)} unique files")

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="JPEG encoding permutations")
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
    results_path = args.output / "encoding_results.json"
    with open(results_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"Results saved to {results_path}")


if __name__ == "__main__":
    main()
