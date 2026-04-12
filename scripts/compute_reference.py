#!/usr/bin/env python3
"""Reference decoder pixel hash computation.

Decodes each corpus file with format-appropriate reference decoders and
records the BLAKE3 hash of the raw decoded pixels. Downstream zen crates
validate their decoder output against these hashes without any FFI.

Reference decoders by format:
  JPEG:  djpeg (libjpeg-turbo), djpeg (mozjpeg), djpegli
  PNG:   convert (ImageMagick/libpng) → raw pixels
  WebP:  dwebp (libwebp)
  AVIF:  avifdec (libavif)
  JXL:   djxl (libjxl)
  GIF:   convert (ImageMagick) → first frame raw pixels
  TIFF:  convert (ImageMagick/libtiff) → raw pixels
  HEIC:  convert (ImageMagick/libheif) → raw pixels

All decoders produce PNM (PPM/PGM) intermediate output whose pixel
data is extracted and hashed. This gives a canonical representation:
row-major, tightly packed, no padding, consistent byte order.
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
    def blake3_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


def _parse_pnm_pixels(pnm_data: bytes) -> bytes | None:
    """Extract raw pixel bytes from PNM (P5/P6) data, skipping the header."""
    if len(pnm_data) < 10:
        return None
    # PNM header: magic\n[comments]\nwidth height\nmaxval\n<pixels>
    pos = 0
    lines_needed = 3  # magic, dimensions, maxval
    lines_found = 0
    while pos < len(pnm_data) and lines_found < lines_needed:
        if pnm_data[pos] == ord('#'):
            # Skip comment line
            while pos < len(pnm_data) and pnm_data[pos] != ord('\n'):
                pos += 1
            pos += 1
            continue
        if pnm_data[pos] == ord('\n'):
            lines_found += 1
        pos += 1
    if lines_found < lines_needed:
        return None
    return pnm_data[pos:]


# ── Format-specific decoders ──────────────────────────────────────────────

def decode_jpeg_djpeg(path: str, binary: str) -> bytes | None:
    """Decode JPEG → PNM via djpeg."""
    try:
        r = subprocess.run([binary, "-pnm", path],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            return None
        return _parse_pnm_pixels(r.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def decode_jpeg_djpegli(path: str, binary: str) -> bytes | None:
    """Decode JPEG → PNM via djpegli (writes to file)."""
    fd, tmp = tempfile.mkstemp(suffix=".ppm")
    os.close(fd)
    try:
        r = subprocess.run([binary, path, tmp],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            return None
        with open(tmp, "rb") as f:
            return _parse_pnm_pixels(f.read())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        try: os.unlink(tmp)
        except OSError: pass


def decode_webp_dwebp(path: str, binary: str) -> bytes | None:
    """Decode WebP → PPM via dwebp."""
    fd, tmp = tempfile.mkstemp(suffix=".ppm")
    os.close(fd)
    try:
        r = subprocess.run([binary, path, "-ppm", "-o", tmp],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            return None
        with open(tmp, "rb") as f:
            return _parse_pnm_pixels(f.read())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        try: os.unlink(tmp)
        except OSError: pass


def decode_avif_avifdec(path: str, binary: str) -> bytes | None:
    """Decode AVIF → PNG via avifdec, then extract raw pixels."""
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        r = subprocess.run([binary, path, tmp],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            return None
        return _decode_to_pnm_via_convert(tmp)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        try: os.unlink(tmp)
        except OSError: pass


def decode_jxl_djxl(path: str, binary: str) -> bytes | None:
    """Decode JXL → PPM via djxl."""
    fd, tmp = tempfile.mkstemp(suffix=".ppm")
    os.close(fd)
    try:
        r = subprocess.run([binary, path, tmp],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            return None
        with open(tmp, "rb") as f:
            return _parse_pnm_pixels(f.read())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    finally:
        try: os.unlink(tmp)
        except OSError: pass


def _decode_to_pnm_via_convert(path: str) -> bytes | None:
    """Decode any image → PPM raw pixels via ImageMagick convert.

    Used as a universal reference decoder for PNG, GIF, TIFF, HEIC.
    Forces 8-bit depth and first frame only (for GIF).
    """
    try:
        # [0] selects first frame (for GIF/APNG), -depth 8 normalizes
        r = subprocess.run(
            ["convert", f"{path}[0]", "-depth", "8", "ppm:-"],
            capture_output=True, timeout=60,
        )
        if r.returncode != 0:
            return None
        return _parse_pnm_pixels(r.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def decode_via_convert(path: str, _binary: str) -> bytes | None:
    """Universal decoder via ImageMagick convert."""
    return _decode_to_pnm_via_convert(path)


# ── Decoder registry ──────────────────────────────────────────────────────

# format -> list of (decoder_id, decoder_info)
FORMAT_DECODERS: dict[str, list[tuple[str, dict]]] = {}


def init_decoders():
    """Initialize available decoders from environment."""
    global FORMAT_DECODERS
    FORMAT_DECODERS = {}

    # ── JPEG decoders ──
    jpeg_decs = []
    djpeg_turbo = os.environ.get("DJPEG_TURBO", "")
    if djpeg_turbo and os.path.isfile(djpeg_turbo):
        jpeg_decs.append(("djpeg-turbo-3.1.0", {
            "name": "libjpeg-turbo", "version": "3.1.0",
            "binary": djpeg_turbo, "decode_fn": decode_jpeg_djpeg,
        }))
    djpeg_moz = os.environ.get("DJPEG_MOZ", "")
    if djpeg_moz and os.path.isfile(djpeg_moz):
        jpeg_decs.append(("djpeg-mozjpeg-4.1.5", {
            "name": "mozjpeg", "version": "4.1.5",
            "binary": djpeg_moz, "decode_fn": decode_jpeg_djpeg,
        }))
    djpegli = os.environ.get("DJPEGLI", "")
    if djpegli and os.path.isfile(djpegli):
        jpeg_decs.append(("djpegli-0.11.1", {
            "name": "jpegli", "version": "0.11.1",
            "binary": djpegli, "decode_fn": decode_jpeg_djpegli,
        }))
    if jpeg_decs:
        FORMAT_DECODERS["jpeg"] = jpeg_decs

    # ── WebP decoder ──
    dwebp = os.environ.get("DWEBP", "")
    if dwebp and os.path.isfile(dwebp):
        FORMAT_DECODERS["webp"] = [("dwebp-1.5.0", {
            "name": "libwebp", "version": "1.5.0",
            "binary": dwebp, "decode_fn": decode_webp_dwebp,
        })]

    # ── AVIF decoder ──
    avifdec = os.environ.get("AVIFDEC", "")
    if avifdec and os.path.isfile(avifdec):
        FORMAT_DECODERS["avif"] = [("avifdec-1.2.1", {
            "name": "libavif", "version": "1.2.1",
            "binary": avifdec, "decode_fn": decode_avif_avifdec,
        })]

    # ── JXL decoder ──
    djxl = os.environ.get("DJXL", "")
    if djxl and os.path.isfile(djxl):
        FORMAT_DECODERS["jxl"] = [("djxl-0.11.1", {
            "name": "libjxl", "version": "0.11.1",
            "binary": djxl, "decode_fn": decode_jxl_djxl,
        })]

    # ── Universal decoder (ImageMagick) for PNG, GIF, TIFF, HEIC ──
    # convert is expected in PATH
    for fmt in ("png", "gif", "tiff", "heic"):
        FORMAT_DECODERS.setdefault(fmt, []).append(
            (f"convert-imagemagick", {
                "name": "ImageMagick", "version": "system",
                "binary": "convert", "decode_fn": decode_via_convert,
            })
        )


def compute_reference_hashes(file_path: str, fmt: str) -> dict[str, str]:
    """Decode a file with all reference decoders for its format."""
    decoders = FORMAT_DECODERS.get(fmt, [])
    hashes = {}
    for decoder_id, dec in decoders:
        pixels = dec["decode_fn"](file_path, dec["binary"])
        if pixels is not None:
            hashes[decoder_id] = blake3_hash(pixels)
    return hashes


def process_file(file_entry: dict, corpus_dir: Path) -> dict:
    """Compute reference hashes for a single corpus file."""
    file_path = str(corpus_dir / file_entry["path"])
    if not os.path.isfile(file_path):
        return file_entry

    fmt = file_entry.get("format", "")
    hashes = compute_reference_hashes(file_path, fmt)
    if hashes:
        file_entry["reference_decodes"] = hashes
    return file_entry


def compute_all(manifest: dict, corpus_dir: Path, workers: int = 0) -> dict:
    """Add reference decode hashes to all files in the manifest."""
    init_decoders()

    if not FORMAT_DECODERS:
        print("WARNING: No reference decoders available!", file=sys.stderr)
        return manifest

    fmt_decoders = []
    for fmt, decs in FORMAT_DECODERS.items():
        dec_names = [d[0] for d in decs]
        fmt_decoders.append(f"{fmt}: {', '.join(dec_names)}")
    print(f"Reference decoders: {'; '.join(fmt_decoders)}")

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
    all_decoders = {}
    for fmt, decs in FORMAT_DECODERS.items():
        for dec_id, d in decs:
            all_decoders[dec_id] = {
                "name": d["name"], "version": d["version"],
                "binary": d["binary"], "format": fmt,
            }
    manifest["decoders"] = all_decoders

    with_refs = sum(1 for f in files if f.get("reference_decodes"))
    print(f"Reference decodes: {with_refs}/{len(files)} files have hashes")

    return manifest


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Compute reference decoder pixel hashes")
    parser.add_argument("--manifest", "-m", type=Path, required=True)
    parser.add_argument("--corpus", "-c", type=Path, required=True)
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
