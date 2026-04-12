#!/usr/bin/env python3
"""Manifest builder for all-the-images corpus.

Reads encoding results from encode_jpeg.py and assembles the final
manifest.json with encoder metadata, source metadata, and per-file entries.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import blake3 as _blake3
    def blake3_file(path: str) -> str:
        h = _blake3.blake3()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
except ImportError:
    import hashlib
    def blake3_file(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()


# Encoder metadata registry
# Collect encoder metadata from all format modules.
# Each encode_*.py has an ENCODER_METADATA dict.
ENCODER_METADATA = {
    # ── JPEG ──
    "libjpeg-turbo-3.1.0": {
        "name": "libjpeg-turbo",
        "version": "3.1.0",
        "binary": "cjpeg",
        "source_url": "https://github.com/libjpeg-turbo/libjpeg-turbo",
        "compile_flags": ["WITH_ARITH_ENC=1", "WITH_ARITH_DEC=1"],
    },
    "libjpeg-9e": {
        "name": "libjpeg (IJG)",
        "version": "9e",
        "binary": "cjpeg",
        "source_url": "https://www.ijg.org/",
        "compile_flags": [],
    },
    "mozjpeg-4.1.5": {
        "name": "mozjpeg",
        "version": "4.1.5",
        "binary": "cjpeg",
        "source_url": "https://github.com/mozilla/mozjpeg",
        "compile_flags": ["WITH_ARITH_ENC=1", "WITH_ARITH_DEC=1"],
    },
    "jpegli-0.11.1": {
        "name": "jpegli",
        "version": "0.11.1 (libjxl)",
        "binary": "cjpegli",
        "source_url": "https://github.com/libjxl/libjxl",
        "compile_flags": ["JPEGXL_ENABLE_JPEGLI=ON"],
    },
    "guetzli-1.0.1": {
        "name": "guetzli",
        "version": "1.0.1",
        "binary": "guetzli",
        "source_url": "https://github.com/google/guetzli",
        "compile_flags": [],
    },
    # ── WebP ──
    "libwebp-cwebp-lossy": {
        "name": "libwebp cwebp (lossy)",
        "version": "1.5.0",
        "binary": "cwebp",
        "source_url": "https://github.com/webmproject/libwebp",
        "compile_flags": [],
    },
    "libwebp-cwebp-lossless": {
        "name": "libwebp cwebp (lossless)",
        "version": "1.5.0",
        "binary": "cwebp",
        "source_url": "https://github.com/webmproject/libwebp",
        "compile_flags": [],
    },
    # ── AVIF ──
    "avifenc-libavif": {
        "name": "avifenc (libavif+aom)",
        "version": "1.2.1",
        "binary": "avifenc",
        "source_url": "https://github.com/AOMediaCodec/libavif",
        "compile_flags": ["AVIF_CODEC_AOM=SYSTEM"],
    },
    # ── JPEG XL ──
    "cjxl-libjxl": {
        "name": "cjxl (libjxl)",
        "version": "0.11.1",
        "binary": "cjxl",
        "source_url": "https://github.com/libjxl/libjxl",
        "compile_flags": [],
    },
    # ── PNG ──
    "imagemagick-convert": {
        "name": "ImageMagick convert (PNG)",
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
        "version": "1.0.3",
        "binary": "zopflipng",
        "source_url": "https://github.com/google/zopfli",
        "compile_flags": [],
    },
    # ── GIF ──
    "gifsicle-1.95": {
        "name": "gifsicle",
        "version": "1.95",
        "binary": "gifsicle",
        "source_url": "https://github.com/kohler/gifsicle",
        "compile_flags": [],
    },
    "imagemagick-gif": {
        "name": "ImageMagick convert (GIF)",
        "version": "system",
        "binary": "convert",
        "source_url": "https://imagemagick.org/",
        "compile_flags": [],
    },
    # ── TIFF ──
    "imagemagick-tiff": {
        "name": "ImageMagick convert (TIFF)",
        "version": "system",
        "binary": "convert",
        "source_url": "https://imagemagick.org/",
        "compile_flags": [],
    },
    "tiffcp-libtiff": {
        "name": "tiffcp (libtiff)",
        "version": "4.7.0",
        "binary": "tiffcp",
        "source_url": "https://gitlab.com/libtiff/libtiff",
        "compile_flags": [],
    },
    # ── HEIC ──
    "heif-enc-x265": {
        "name": "heif-enc (libheif+x265)",
        "version": "1.19.7",
        "binary": "heif-enc",
        "source_url": "https://github.com/strukturag/libheif",
        "compile_flags": [],
    },
}


def build_manifest(results_path: Path, sources_path: Path,
                   corpus_dir: Path) -> dict:
    """Assemble manifest from encoding results and source metadata."""
    with open(results_path) as f:
        results = json.load(f)

    with open(sources_path) as f:
        sources_list = json.load(f)

    # Build source metadata map
    sources_meta = {}
    for s in sources_list:
        sources_meta[s["name"]] = {
            "width": s["w"],
            "height": s["h"],
            "channels": s["channels"],
            "type": s["type"],
        }

    # Collect successful results, dedup by output hash
    seen_hashes = {}  # hash -> file entry
    files = []
    encoders_used = set()
    sources_used = set()
    failures = 0

    for r in results:
        if not r["success"]:
            failures += 1
            continue

        encoders_used.add(r["encoder_id"])
        sources_used.add(r["source_name"])

        h = r["output_hash"]
        if h in seen_hashes:
            # Already have this file — just note the duplicate params
            continue

        # Compute BLAKE3 of the actual file
        file_path = corpus_dir / r["output_path"]
        if file_path.exists():
            b3 = blake3_file(str(file_path))
        else:
            b3 = ""

        # Detect format from file extension
        ext = Path(r["output_path"]).suffix.lstrip(".")
        fmt = {
            "jpg": "jpeg", "jpeg": "jpeg",
            "png": "png",
            "webp": "webp",
            "avif": "avif",
            "jxl": "jxl",
            "gif": "gif",
            "tiff": "tiff", "tif": "tiff",
            "heic": "heic",
        }.get(ext, ext)

        entry = {
            "path": r["output_path"],
            "blake3": b3,
            "bytes": r["output_bytes"],
            "format": fmt,
            "encoder": r["encoder_id"],
            "source": r["source_name"],
            "params": r["params"],
        }
        files.append(entry)
        seen_hashes[h] = entry

    # Filter encoder metadata to only include encoders that produced output
    encoders = {
        k: v for k, v in ENCODER_METADATA.items() if k in encoders_used
    }

    total_bytes = sum(f["bytes"] for f in files)

    manifest = {
        "schema_version": "0.1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "encoders": encoders,
        "sources": sources_meta,
        "files": files,
        "stats": {
            "total_files": len(files),
            "total_bytes": total_bytes,
            "unique_hashes": len(seen_hashes),
            "encoders_used": len(encoders_used),
            "sources_used": len(sources_used),
            "encoding_failures": failures,
        },
    }

    return manifest


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Build corpus manifest")
    parser.add_argument("--results", "-r", type=Path, required=True,
                        help="Path to encoding_results.json")
    parser.add_argument("--sources", "-s", type=Path, required=True,
                        help="Path to sources.json")
    parser.add_argument("--corpus", "-c", type=Path, required=True,
                        help="Corpus root directory")
    parser.add_argument("--output", "-o", type=Path, required=True,
                        help="Output manifest.json path")
    args = parser.parse_args()

    manifest = build_manifest(args.results, args.sources, args.corpus)

    with open(args.output, "w") as f:
        json.dump(manifest, f, indent=2)

    stats = manifest["stats"]
    print(f"Manifest written: {args.output}")
    print(f"  Files: {stats['total_files']}")
    print(f"  Unique: {stats['unique_hashes']}")
    print(f"  Size: {stats['total_bytes'] / 1024 / 1024:.1f} MB")
    print(f"  Encoders: {stats['encoders_used']}")
    print(f"  Sources: {stats['sources_used']}")
    print(f"  Failures: {stats['encoding_failures']}")


if __name__ == "__main__":
    main()
