#!/usr/bin/env python3
"""Reference decoder pixel hash computation.

Decodes each corpus JPEG with multiple reference decoders and records
the BLAKE3 hash of the raw decoded pixels. Downstream zen crates can
validate their decoder output against these hashes without any FFI.

Reference decoders:
  - djpeg from libjpeg-turbo (the de facto standard)
  - djpeg from mozjpeg (libjpeg-turbo fork, should match)
  - djpegli from libjxl (handles XYB colorspace)

Output: appends reference_decodes to each file entry in the manifest.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

try:
    import blake3 as _blake3
    def blake3_hash(data: bytes) -> str:
        return _blake3.blake3(data).hexdigest()
except ImportError:
    # Fallback to SHA-256 if blake3 not installed
    def blake3_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


def decode_with_djpeg(jpeg_path: str, djpeg_binary: str) -> bytes | None:
    """Decode a JPEG to raw PPM/PGM pixels using djpeg.

    Returns the raw pixel data (without PPM/PGM header) or None on failure.
    djpeg outputs PPM (RGB) for color images or PGM (grayscale).
    """
    try:
        result = subprocess.run(
            [djpeg_binary, "-pnm", jpeg_path],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            return None

        pnm_data = result.stdout
        if len(pnm_data) < 10:
            return None

        # Parse PNM header to find pixel data start
        # P5 (PGM) or P6 (PPM) binary format
        # Header: "P5\n<width> <height>\n<maxval>\n" or "P6\n..."
        header_end = 0
        newline_count = 0
        for i, b in enumerate(pnm_data):
            if b == ord('\n'):
                newline_count += 1
                if newline_count == 3:
                    header_end = i + 1
                    break
            # Skip comments
            if b == ord('#') and (i == 0 or pnm_data[i-1] == ord('\n')):
                while i < len(pnm_data) and pnm_data[i] != ord('\n'):
                    i += 1
                # Don't count comment line as a real header line
                newline_count -= 1

        if header_end == 0:
            return None

        return pnm_data[header_end:]

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def decode_with_djpegli(jpeg_path: str, djpegli_binary: str) -> bytes | None:
    """Decode a JPEG to raw pixels using djpegli.

    djpegli outputs PPM/PGM to stdout or a file.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".ppm")
    os.close(fd)
    try:
        result = subprocess.run(
            [djpegli_binary, jpeg_path, tmp_path],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            return None

        with open(tmp_path, "rb") as f:
            pnm_data = f.read()

        if len(pnm_data) < 10:
            return None

        # Parse PNM header (same as above)
        header_end = 0
        newline_count = 0
        for i, b in enumerate(pnm_data):
            if b == ord('\n'):
                newline_count += 1
                if newline_count == 3:
                    header_end = i + 1
                    break

        if header_end == 0:
            return None

        return pnm_data[header_end:]

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# Decoder registry
DECODERS = {}

def init_decoders():
    """Initialize available decoders from environment."""
    global DECODERS

    djpeg_turbo = os.environ.get("DJPEG_TURBO", "")
    if djpeg_turbo and os.path.isfile(djpeg_turbo):
        DECODERS["djpeg-turbo-3.1.0"] = {
            "name": "libjpeg-turbo",
            "version": "3.1.0",
            "binary": djpeg_turbo,
            "decode_fn": decode_with_djpeg,
        }

    djpeg_moz = os.environ.get("DJPEG_MOZ", "")
    if djpeg_moz and os.path.isfile(djpeg_moz):
        DECODERS["djpeg-mozjpeg-4.1.5"] = {
            "name": "mozjpeg",
            "version": "4.1.5",
            "binary": djpeg_moz,
            "decode_fn": decode_with_djpeg,
        }

    djpegli = os.environ.get("DJPEGLI", "")
    if djpegli and os.path.isfile(djpegli):
        DECODERS["djpegli-0.11.1"] = {
            "name": "jpegli",
            "version": "0.11.1",
            "binary": djpegli,
            "decode_fn": decode_with_djpegli,
        }


def compute_reference_hashes(jpeg_path: str) -> dict[str, str]:
    """Decode a JPEG with all available reference decoders, return pixel hashes."""
    hashes = {}
    for decoder_id, decoder in DECODERS.items():
        pixels = decoder["decode_fn"](jpeg_path, decoder["binary"])
        if pixels is not None:
            hashes[decoder_id] = blake3_hash(pixels)
    return hashes


def process_file(file_entry: dict, corpus_dir: Path) -> dict:
    """Compute reference hashes for a single corpus file."""
    jpeg_path = str(corpus_dir / file_entry["path"])
    if not os.path.isfile(jpeg_path):
        return file_entry

    hashes = compute_reference_hashes(jpeg_path)
    file_entry["reference_decodes"] = hashes
    return file_entry


def compute_all(manifest: dict, corpus_dir: Path, workers: int = 0) -> dict:
    """Add reference decode hashes to all files in the manifest."""
    init_decoders()

    if not DECODERS:
        print("WARNING: No reference decoders available!", file=sys.stderr)
        return manifest

    print(f"Reference decoders: {', '.join(DECODERS.keys())}")

    files = manifest.get("files", [])
    if not files:
        return manifest

    if workers <= 0:
        workers = min(os.cpu_count() or 4, 8)

    completed = 0
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_file, f, corpus_dir): i
            for i, f in enumerate(files)
        }
        for future in as_completed(futures):
            idx = futures[future]
            files[idx] = future.result()
            completed += 1
            if completed % 500 == 0 or completed == len(files):
                print(f"  [{completed}/{len(files)}] decoded")

    # Add decoder metadata to manifest
    manifest["decoders"] = {
        dec_id: {"name": d["name"], "version": d["version"], "binary": d["binary"]}
        for dec_id, d in DECODERS.items()
    }

    # Count how many files got at least one reference decode
    with_refs = sum(1 for f in files if f.get("reference_decodes"))
    print(f"Reference decodes: {with_refs}/{len(files)} files have hashes")

    return manifest


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Compute reference decoder pixel hashes")
    parser.add_argument("--manifest", "-m", type=Path, required=True,
                        help="Path to manifest.json")
    parser.add_argument("--corpus", "-c", type=Path, required=True,
                        help="Corpus root directory")
    parser.add_argument("--workers", "-j", type=int, default=0)
    args = parser.parse_args()

    with open(args.manifest) as f:
        manifest = json.load(f)

    manifest = compute_all(manifest, args.corpus, workers=args.workers)

    with open(args.manifest, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Updated manifest: {args.manifest}")


if __name__ == "__main__":
    main()
