#!/usr/bin/env python3
"""Main corpus generation orchestrator.

Runs the full pipeline:
  1. Generate synthetic source images
  2. Encode with all JPEG encoders (parameter permutations)
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
from pathlib import Path

# Import sibling modules
sys.path.insert(0, str(Path(__file__).parent))
import generate_sources
import encode_jpeg
import build_manifest
import compute_reference


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
    args = parser.parse_args()

    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    sources_dir = output / "sources"

    mode = "quick" if args.quick else "full"
    print(f"=== all-the-images corpus generation ({mode}) ===")
    print(f"Output: {output}")
    print()

    t0 = time.monotonic()

    # ── Step 1: Generate source images ──────────────────────────────────
    print("─── Step 1: Generating source images ───")
    sources = generate_sources.generate_all(sources_dir, quick=args.quick)
    sources_json = sources_dir / "sources.json"
    with open(sources_json, "w") as f:
        json.dump(sources, f, indent=2)
    print(f"  {len(sources)} source images generated")
    print()

    # ── Step 2: Encode with all JPEG encoders ───────────────────────────
    print("─── Step 2: JPEG encoding permutations ───")
    results = encode_jpeg.run_all(sources, output, quick=args.quick,
                                  workers=args.workers)
    results_json = output / "encoding_results.json"
    with open(results_json, "w") as f:
        json.dump([{
            "encoder_id": r.encoder_id,
            "source_name": r.source_name,
            "params": r.params,
            "success": r.success,
            "output_hash": r.output_hash,
            "output_bytes": r.output_bytes,
            "output_path": r.output_path,
            "error": r.error,
            "expect_fail": r.expect_fail,
        } for r in results], f, indent=2)
    print()

    # ── Step 3: Build manifest ──────────────────────────────────────────
    print("─── Step 3: Building manifest ───")
    manifest = build_manifest.build_manifest(results_json, sources_json, output)
    manifest_path = output / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    stats = manifest["stats"]
    print(f"  Files: {stats['total_files']}")
    print(f"  Unique: {stats['unique_hashes']}")
    print(f"  Size: {stats['total_bytes'] / 1024 / 1024:.1f} MB")
    print(f"  Failures: {stats['encoding_failures']}")
    print()

    # ── Step 4: Reference decode hashes ─────────────────────────────────
    if not args.skip_reference:
        print("─── Step 4: Computing reference decode hashes ───")
        manifest = compute_reference.compute_all(manifest, output,
                                                  workers=args.workers)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
    else:
        print("─── Step 4: Skipped (--skip-reference) ───")
    print()

    # ── Summary ─────────────────────────────────────────────────────────
    elapsed = time.monotonic() - t0
    print(f"=== Done in {elapsed:.1f}s ===")
    print(f"Manifest: {manifest_path}")
    print(f"Corpus:   {output}/jpeg/")

    # Verify the manifest is valid JSON
    with open(manifest_path) as f:
        json.load(f)
    print("Manifest JSON: valid")


if __name__ == "__main__":
    main()
