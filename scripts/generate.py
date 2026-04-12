#!/usr/bin/env python3
"""Main corpus generation orchestrator.

Runs the full pipeline:
  1. Generate synthetic source images
  2. Encode with all format encoders (parameter permutations)
  3. Build manifest from encoding results
  4. Compute reference decoder pixel hashes
  5. Write final manifest.json

This is the Docker ENTRYPOINT — invoked by `docker compose run generate`.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

# Import sibling modules
sys.path.insert(0, str(Path(__file__).parent))
import generate_sources
import encode_jpeg
import encode_png
import encode_webp
import encode_avif
import encode_jxl
import encode_gif
import encode_tiff
import build_manifest
import compute_reference


# All format encoders in pipeline order
FORMAT_ENCODERS = [
    ("JPEG",  encode_jpeg),
    ("PNG",   encode_png),
    ("WebP",  encode_webp),
    ("AVIF",  encode_avif),
    ("JXL",   encode_jxl),
    ("GIF",   encode_gif),
    ("TIFF/HEIC", encode_tiff),
]


def result_to_dict(r) -> dict:
    """Convert an EncoderResult to a JSON-serializable dict."""
    d = {
        "encoder_id": r.encoder_id,
        "source_name": r.source_name,
        "params": r.params,
        "success": r.success,
        "output_hash": r.output_hash,
        "output_bytes": r.output_bytes,
        "output_path": r.output_path,
        "error": r.error,
        "expect_fail": r.expect_fail,
    }
    if r.failure_category:
        d["failure_category"] = r.failure_category
    return d


def print_failure_summary(results: list) -> None:
    """Print categorized failure breakdown."""
    from collections import Counter
    failures = [r for r in results if not r.success]
    if not failures:
        return

    # Categorize any failures missing a category (from other format scripts)
    for r in failures:
        if not r.failure_category:
            r.failure_category = encode_jpeg.categorize_failure(r.error)

    # By category
    by_cat = Counter(r.failure_category for r in failures)
    print("  Failures by category:")
    for cat, count in by_cat.most_common():
        print(f"    {cat:20s} {count:5d}")

    # By encoder × category (top 10)
    by_enc_cat = Counter((r.encoder_id, r.failure_category) for r in failures)
    print("  Top failure encoder×category:")
    for (enc, cat), count in by_enc_cat.most_common(10):
        print(f"    {enc:30s} {cat:20s} {count:5d}")


def main():
    parser = argparse.ArgumentParser(
        description="all-the-images corpus generator")
    parser.add_argument("--output", "-o", type=Path,
                        default=Path(os.environ.get("OUTPUT_DIR", "/output")),
                        help="Output directory")
    parser.add_argument("--quick", action="store_true",
                        default=os.environ.get("QUICK_MODE", "0") == "1",
                        help="Quick mode: fewer dimensions, fewer params")
    parser.add_argument("--workers", "-j", type=int, default=0,
                        help="Parallel workers (0 = auto)")
    parser.add_argument("--skip-reference", action="store_true",
                        help="Skip reference decode hash computation")
    parser.add_argument("--formats", type=str, default="all",
                        help="Comma-separated format list (jpeg,png,webp,avif,jxl,gif,tiff) or 'all'")
    parser.add_argument("--dimensions", type=str, default="",
                        help="Extra dimensions: '320x240,1920x1080,512'")
    parser.add_argument("--sources-dir", type=Path, default=None,
                        help="Directory of user-provided images (PNG/PPM/PFM) to re-encode")
    parser.add_argument("--no-16bit", action="store_true",
                        help="Skip 16-bit source generation")
    parser.add_argument("--no-hdr", action="store_true",
                        help="Skip HDR float source generation")
    args = parser.parse_args()

    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    sources_dir = output / "sources"

    # Parse format filter
    if args.formats == "all":
        enabled_formats = None  # run all
    else:
        enabled_formats = set(f.strip().upper() for f in args.formats.split(","))

    mode = "quick" if args.quick else "full"
    print(f"=== all-the-images corpus generation ({mode}) ===")
    print(f"Output: {output}")
    if enabled_formats:
        print(f"Formats: {', '.join(sorted(enabled_formats))}")
    print()

    t0 = time.monotonic()

    # ── Step 1: Generate source images ──────────────────────────────────
    print("─── Step 1: Generating source images ───")
    extra_dims = (generate_sources.parse_dimensions(args.dimensions)
                  if args.dimensions else None)
    sources = generate_sources.generate_all(
        sources_dir, quick=args.quick,
        extra_dims=extra_dims,
        user_sources_dir=args.sources_dir,
        enable_16bit=not args.no_16bit,
        enable_hdr=not args.no_hdr,
    )
    sources_json = sources_dir / "sources.json"
    with open(sources_json, "w") as f:
        json.dump(sources, f, indent=2)
    print(f"  {len(sources)} source images generated")
    print()

    # ── Step 2: Encode with all format encoders ─────────────────────────
    all_results = []
    step = 2

    for fmt_name, fmt_module in FORMAT_ENCODERS:
        # Skip formats not in filter
        if enabled_formats and fmt_name.upper() not in enabled_formats:
            # Also check individual names in combined formats like "TIFF/HEIC"
            parts = [p.strip() for p in fmt_name.upper().split("/")]
            if not any(p in enabled_formats for p in parts):
                continue

        print(f"─── Step {step}: {fmt_name} encoding permutations ───")
        results = fmt_module.run_all(sources, output, quick=args.quick,
                                     workers=args.workers)
        all_results.extend(results)
        step += 1
        print()

    # Categorize failures before serialization so categories are in the JSON
    for r in all_results:
        if not r.success and not r.failure_category:
            r.failure_category = encode_jpeg.categorize_failure(r.error)

    # Save all results
    results_json = output / "encoding_results.json"
    with open(results_json, "w") as f:
        json.dump([result_to_dict(r) for r in all_results], f, indent=2)

    # ── Step N: Build manifest ──────────────────────────────────────────
    print(f"─── Step {step}: Building manifest ───")
    manifest = build_manifest.build_manifest(results_json, sources_json, output)
    manifest_path = output / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    stats = manifest["stats"]
    print(f"  Files: {stats['total_files']}")
    print(f"  Unique: {stats['unique_hashes']}")
    print(f"  Size: {stats['total_bytes'] / 1024 / 1024:.1f} MB")
    print(f"  Failures: {stats['encoding_failures']}")
    if stats['encoding_failures'] > 0:
        print_failure_summary(all_results)
    print()
    step += 1

    # ── Step N+1: Reference decode hashes ───────────────────────────────
    if not args.skip_reference:
        print(f"─── Step {step}: Computing reference decode hashes ───")
        manifest = compute_reference.compute_all(manifest, output,
                                                  workers=args.workers)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
    else:
        print(f"─── Step {step}: Skipped (--skip-reference) ───")
    print()

    # ── Summary ─────────────────────────────────────────────────────────
    elapsed = time.monotonic() - t0
    print(f"=== Done in {elapsed:.1f}s ===")
    print(f"Manifest: {manifest_path}")

    # Count files per format
    from collections import Counter
    fmt_counts = Counter(f["format"] for f in manifest["files"])
    for fmt, count in sorted(fmt_counts.items()):
        print(f"  {fmt}: {count} files")
    print(f"  total: {sum(fmt_counts.values())} files")

    # Verify the manifest is valid JSON
    with open(manifest_path) as f:
        json.load(f)
    print("Manifest JSON: valid")


if __name__ == "__main__":
    main()
