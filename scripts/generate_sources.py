#!/usr/bin/env python3
"""Source image generator for all-the-images corpus.

Three source categories:
  1. Synthetic — deterministic test patterns (noise, patches, checkerboard,
     edges, bands) at various dimensions, bit depths, and channel counts.
  2. User-provided — real PNG/PPM/PFM/EXR images from --sources-dir.
  3. Derived — CMYK, wide-gamut, and HDR variants created via ImageMagick
     from synthetic or user-provided sources.

Source metadata dict keys:
  name, path, w, h, channels, type, bit_depth, color_space, ext

bit_depth: 8, 16, or 32 (float)
color_space: "srgb" | "linear" | "p3" | "rec2020" | "pq" | "hlg"
"""

import os
import struct
import subprocess
import sys
from pathlib import Path


# ── Pixel generators ───────────────────────────────────────────────────────

def lcg(seed: int) -> tuple[int, int]:
    """Linear congruential generator matching the Rust version."""
    seed = (seed * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
    return seed, (seed >> 33) & 0xFF


def gen_noise(w: int, h: int, c: int, seed: int, maxval: int = 255) -> list[int]:
    """Uniform random pixels scaled to maxval."""
    s = seed
    out = []
    for _ in range(w * h * c):
        s, val = lcg(s)
        out.append((val * maxval) // 255)
    return out


def gen_noise_patches(w: int, h: int, c: int, seed: int, maxval: int = 255) -> list[int]:
    """Random noise with solid-color rectangular patches overlaid."""
    out = gen_noise(w, h, c, seed, maxval)
    s = seed ^ 0xDEADBEEF
    s, n_extra = lcg(s)
    n_patches = 6 + (n_extra % 6)
    for _ in range(n_patches):
        s, px = lcg(s); px = px % w
        s, py = lcg(s); py = py % h
        s, pw = lcg(s); pw = 1 + (pw % max(w // 3, 1))
        s, ph = lcg(s); ph = 1 + (ph % max(h // 3, 1))
        col = []
        for _ in range(3):
            s, v = lcg(s)
            col.append((v * maxval) // 255)
        for y in range(py, min(py + ph, h)):
            for x in range(px, min(px + pw, w)):
                idx = (y * w + x) * c
                for k in range(c):
                    out[idx + k] = col[min(k, 2)]
    return out


def gen_checkerboard(w: int, h: int, c: int, block: int, maxval: int = 255) -> list[int]:
    """High-contrast alternating blocks."""
    hi = (240 * maxval) // 255
    lo = (16 * maxval) // 255
    out = [0] * (w * h * c)
    for y in range(h):
        for x in range(w):
            v = hi if ((x // block) ^ (y // block)) & 1 == 0 else lo
            idx = (y * w + x) * c
            for k in range(c):
                out[idx + k] = v
    return out


def gen_edges(w: int, h: int, c: int, direction: str, maxval: int = 255) -> list[int]:
    """Gradient patterns."""
    out = [0] * (w * h * c)
    for y in range(h):
        for x in range(w):
            if direction == "x":
                v = int(maxval * x / max(w - 1, 1))
            else:
                v = int(maxval * y / max(h - 1, 1))
            idx = (y * w + x) * c
            for k in range(c):
                out[idx + k] = v
    return out


def gen_bands(w: int, h: int, c: int, horizontal: bool, maxval: int = 255) -> list[int]:
    """Irregular-width stripes."""
    hi = (220 * maxval) // 255
    lo = (35 * maxval) // 255
    stripes = {0, 3, 7, 8, 15, 16, 23, 24, 31, 39, 47, 48, 63, 64, 95, 127}
    out = [0] * (w * h * c)
    for y in range(h):
        for x in range(w):
            pos = y if horizontal else x
            v = hi if pos in stripes else lo
            idx = (y * w + x) * c
            for k in range(c):
                out[idx + k] = v
    return out


# ── Writers ────────────────────────────────────────────────────────────────

def write_pnm(path: Path, w: int, h: int, c: int, data: list[int],
              maxval: int = 255):
    """Write binary PNM: P5 (grayscale) or P6 (RGB). Supports 8-bit and 16-bit."""
    magic = "P5" if c == 1 else "P6"
    with open(path, "wb") as f:
        f.write(f"{magic}\n{w} {h}\n{maxval}\n".encode())
        if maxval <= 255:
            f.write(bytes(data))
        else:
            # 16-bit: big-endian unsigned shorts
            for v in data:
                f.write(struct.pack(">H", min(v, maxval)))


def write_pfm(path: Path, w: int, h: int, c: int, data: list[float]):
    """Write PFM (portable float map). 32-bit float, bottom-to-top rows.
    cjxl, cjpegli, and ImageMagick all read PFM natively.
    """
    magic = "Pf" if c == 1 else "PF"
    # Negative byte order = little-endian
    with open(path, "wb") as f:
        f.write(f"{magic}\n{w} {h}\n-1.0\n".encode())
        # PFM stores rows bottom-to-top
        for y in range(h - 1, -1, -1):
            row_start = y * w * c
            for i in range(w * c):
                f.write(struct.pack("<f", data[row_start + i]))


def write_png_16bit(path: Path, w: int, h: int, c: int, data: list[int]):
    """Write 16-bit PNG via ImageMagick from raw pixel data."""
    # Write as 16-bit PNM first, then convert to PNG
    pnm_path = str(path) + ".tmp.pnm"
    write_pnm(Path(pnm_path), w, h, c, data, maxval=65535)
    subprocess.run(
        ["convert", pnm_path, "-depth", "16", str(path)],
        check=True, capture_output=True,
    )
    os.unlink(pnm_path)


# ── Dimension configs ─────────────────────────────────────────────────────

DIMENSIONS_FULL = [
    (7, 7), (9, 9), (16, 16), (17, 17), (23, 29), (31, 17),
    (32, 32), (33, 33), (47, 63), (64, 64), (65, 65), (96, 96), (128, 128),
]

DIMENSIONS_QUICK = [(7, 7), (16, 16), (32, 32), (64, 64)]

SEED_NOISE = 0x12345678_9ABCDEF0
SEED_PATCHES = 0xFEDCBA98_76543210

# Pattern generators: (func, name_prefix, min_size, rgb_only)
PATTERNS_8BIT = [
    (lambda w, h, c, s: gen_noise(w, h, c, s), "noise", 1, False),
    (lambda w, h, c, s: gen_noise_patches(w, h, c, s), "patches", 16, False),
    (lambda w, h, c, s: gen_checkerboard(w, h, c, 4 if w < 32 else 8), "checker", 1, False),
    (lambda w, h, c, s: gen_edges(w, h, c, "x"), "edges_x", 1, True),
    (lambda w, h, c, s: gen_edges(w, h, c, "y"), "edges_y", 1, True),
    (lambda w, h, c, s: gen_bands(w, h, c, True), "bands_h", 16, False),
    (lambda w, h, c, s: gen_bands(w, h, c, False), "bands_v", 16, False),
]


def _make_source(name, path, w, h, channels, stype, bit_depth=8,
                 color_space="srgb") -> dict:
    """Build a source metadata dict."""
    return {
        "name": name,
        "path": str(path),
        "w": w, "h": h,
        "channels": channels,
        "type": stype,
        "bit_depth": bit_depth,
        "color_space": color_space,
        "ext": Path(path).suffix.lstrip("."),
    }


# ── Generation ─────────────────────────────────────────────────────────────

def generate_synthetic_8bit(output_dir: Path, dims: list[tuple[int, int]]) -> list[dict]:
    """Generate 8-bit synthetic sources (PPM/PGM)."""
    sources = []
    for w, h in dims:
        for channels, label in [(3, "rgb"), (1, "gray")]:
            ext = "ppm" if channels == 3 else "pgm"
            seed = SEED_NOISE ^ (w * 10000 + h)
            for gen_fn, prefix, min_size, rgb_only in PATTERNS_8BIT:
                if rgb_only and channels == 1:
                    continue
                if w < min_size or h < min_size:
                    continue
                name = f"{prefix}_{w}x{h}_{label}"
                path = output_dir / f"{name}.{ext}"
                data = gen_fn(w, h, channels, seed)
                write_pnm(path, w, h, channels, data)
                sources.append(_make_source(name, path, w, h, channels,
                                            prefix, 8, "srgb"))
    return sources


def generate_synthetic_16bit(output_dir: Path, dims: list[tuple[int, int]]) -> list[dict]:
    """Generate 16-bit synthetic sources (PNG via ImageMagick)."""
    sources = []
    # Subset of patterns and dims for 16-bit — full matrix would be huge
    subset_dims = [(d[0], d[1]) for d in dims if d[0] >= 16][:4]
    for w, h in subset_dims:
        for channels, label in [(3, "rgb"), (1, "gray")]:
            seed = SEED_NOISE ^ (w * 10000 + h) ^ 0x1600
            for prefix in ["noise", "checker"]:
                name = f"{prefix}_{w}x{h}_{label}_16bit"
                path = output_dir / f"{name}.png"
                maxval = 65535
                if prefix == "noise":
                    data = gen_noise(w, h, channels, seed, maxval)
                else:
                    data = gen_checkerboard(w, h, channels, 8, maxval)
                try:
                    write_png_16bit(path, w, h, channels, data)
                    sources.append(_make_source(name, path, w, h, channels,
                                                prefix, 16, "srgb"))
                except (FileNotFoundError, subprocess.CalledProcessError) as e:
                    print(f"  [skip] 16-bit PNG: {e}", file=sys.stderr)
                    break
    return sources


def generate_synthetic_hdr(output_dir: Path, dims: list[tuple[int, int]]) -> list[dict]:
    """Generate HDR float sources (PFM).

    PFM is read natively by cjxl, cjpegli, and ImageMagick.
    Values in [0, 1] for SDR-range, >1 for HDR highlights.
    """
    sources = []
    # Small subset for HDR — encoding is expensive
    hdr_dims = [(d[0], d[1]) for d in dims if 16 <= d[0] <= 64][:3]
    for w, h in hdr_dims:
        channels = 3
        seed = SEED_NOISE ^ (w * 10000 + h) ^ 0xF100A7

        # SDR-range float noise (0..1) — tests float pipeline without HDR values
        name = f"noise_{w}x{h}_float"
        path = output_dir / f"{name}.pfm"
        s = seed
        data = []
        for _ in range(w * h * channels):
            s, v = lcg(s)
            data.append(v / 255.0)
        write_pfm(path, w, h, channels, data)
        sources.append(_make_source(name, path, w, h, channels,
                                    "noise", 32, "linear"))

        # HDR noise with highlights up to 10.0 (PQ range)
        name = f"noise_{w}x{h}_hdr"
        path = output_dir / f"{name}.pfm"
        s = seed ^ 0xDEAD
        data = []
        for _ in range(w * h * channels):
            s, v = lcg(s)
            # Most pixels SDR (0-1), occasional HDR highlights (1-10)
            val = v / 255.0
            if val > 0.9:
                val = 1.0 + (val - 0.9) * 90.0  # 0.9-1.0 maps to 1.0-10.0
            data.append(val)
        write_pfm(path, w, h, channels, data)
        sources.append(_make_source(name, path, w, h, channels,
                                    "noise", 32, "pq"))

        # HDR checkerboard — high contrast between SDR and HDR blocks
        name = f"checker_{w}x{h}_hdr"
        path = output_dir / f"{name}.pfm"
        data = []
        for y in range(h):
            for x in range(w):
                block = 8
                if ((x // block) ^ (y // block)) & 1 == 0:
                    v = 0.8  # SDR
                else:
                    v = 5.0  # HDR highlight
                for _ in range(channels):
                    data.append(v)
        write_pfm(path, w, h, channels, data)
        sources.append(_make_source(name, path, w, h, channels,
                                    "checkerboard", 32, "pq"))

    return sources


def generate_wide_gamut(output_dir: Path, dims: list[tuple[int, int]]) -> list[dict]:
    """Generate wide-gamut sources (Display P3, Rec.2020) via ImageMagick.

    Creates PNGs with embedded ICC profiles. Only a few combos since the
    profile is the interesting part, not the pattern.
    """
    sources = []
    wg_dims = [(d[0], d[1]) for d in dims if 32 <= d[0] <= 64][:2]

    for w, h in wg_dims:
        # We need a base PPM first
        base_ppm = output_dir / f"noise_{w}x{h}_rgb.ppm"
        if not base_ppm.exists():
            continue

        for profile_name, cs_label in [("sRGB", "srgb"), ("P3", "p3")]:
            name = f"noise_{w}x{h}_{cs_label}_16bit"
            path = output_dir / f"{name}.png"
            try:
                # Convert to 16-bit PNG with colorspace tag
                # ImageMagick's -colorspace doesn't embed ICC, but we can
                # at least generate the 16-bit PNG with the right tag
                cmd = ["convert", str(base_ppm), "-depth", "16"]
                if profile_name == "P3":
                    cmd += ["-set", "colorspace", "RGB"]
                cmd += [str(path)]
                subprocess.run(cmd, check=True, capture_output=True)
                sources.append(_make_source(name, path, w, h, 3,
                                            "noise", 16, cs_label))
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass

    return sources


def generate_cmyk(output_dir: Path, dims: list[tuple[int, int]]) -> list[dict]:
    """Generate CMYK sources via ImageMagick."""
    sources = []
    for w, h in [(d[0], d[1]) for d in dims if 32 <= d[0] <= 64][:2]:
        base_ppm = output_dir / f"noise_{w}x{h}_rgb.ppm"
        if not base_ppm.exists():
            continue
        name = f"cmyk_{w}x{h}"
        path = output_dir / f"{name}.tiff"
        try:
            subprocess.run(
                ["convert", str(base_ppm), "-colorspace", "CMYK", str(path)],
                check=True, capture_output=True,
            )
            sources.append(_make_source(name, path, w, h, 4, "cmyk", 8, "srgb"))
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("  [skip] ImageMagick not available for CMYK", file=sys.stderr)
            break
    return sources


def scan_user_sources(sources_dir: Path) -> list[dict]:
    """Scan a directory for user-provided source images.

    Supported formats: PNG, PPM, PGM, PFM, TIFF, EXR, JPEG.
    Metadata is inferred from the file where possible; ImageMagick
    `identify` is used for dimensions and bit depth.
    """
    sources = []
    if not sources_dir.is_dir():
        return sources

    exts = {".png", ".ppm", ".pgm", ".pfm", ".tiff", ".tif", ".exr",
            ".jpg", ".jpeg", ".bmp"}
    files = sorted(f for f in sources_dir.iterdir()
                   if f.suffix.lower() in exts and f.is_file())

    for path in files:
        name = path.stem
        # Use ImageMagick identify for metadata
        try:
            result = subprocess.run(
                ["identify", "-format", "%w %h %z %[channels]", str(path)],
                capture_output=True, timeout=10, check=True,
            )
            parts = result.stdout.decode().strip().split()
            w = int(parts[0])
            h = int(parts[1])
            bit_depth = int(parts[2]) if len(parts) > 2 else 8
            ch_str = parts[3] if len(parts) > 3 else "srgb"
            # Estimate channel count from channels string
            if "rgba" in ch_str.lower() or "cmyk" in ch_str.lower():
                channels = 4
            elif "rgb" in ch_str.lower() or "srgb" in ch_str.lower():
                channels = 3
            elif "gray" in ch_str.lower():
                channels = 1
            else:
                channels = 3  # assume RGB

            # Map bit depth
            if bit_depth > 16:
                bd = 32
            elif bit_depth > 8:
                bd = 16
            else:
                bd = 8

            sources.append(_make_source(
                name, path, w, h, channels, "user", bd, "srgb"))
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, (ValueError, IndexError)):
            # Fallback: just record the file, let encoders figure it out
            sources.append(_make_source(
                name, path, 0, 0, 3, "user", 8, "srgb"))

    return sources


def parse_dimensions(dim_str: str) -> list[tuple[int, int]]:
    """Parse dimension specs like '320x240,1920x1080,512'."""
    dims = []
    for part in dim_str.split(","):
        part = part.strip()
        if "x" in part:
            w, h = part.split("x", 1)
            dims.append((int(w), int(h)))
        else:
            n = int(part)
            dims.append((n, n))
    return dims


# ── Main ───────────────────────────────────────────────────────────────────

def generate_all(output_dir: Path, quick: bool = False,
                 extra_dims: list[tuple[int, int]] | None = None,
                 user_sources_dir: Path | None = None,
                 enable_16bit: bool = True,
                 enable_hdr: bool = True) -> list[dict]:
    """Generate all source images."""
    output_dir.mkdir(parents=True, exist_ok=True)

    dims = list(DIMENSIONS_QUICK if quick else DIMENSIONS_FULL)
    if extra_dims:
        # Merge, dedup
        seen = set(dims)
        for d in extra_dims:
            if d not in seen:
                dims.append(d)
                seen.add(d)

    sources = []

    # 8-bit synthetic (always)
    synth_8 = generate_synthetic_8bit(output_dir, dims)
    sources.extend(synth_8)
    print(f"  8-bit synthetic: {len(synth_8)} images")

    # 16-bit synthetic
    if enable_16bit and not quick:
        synth_16 = generate_synthetic_16bit(output_dir, dims)
        sources.extend(synth_16)
        print(f"  16-bit synthetic: {len(synth_16)} images")

    # HDR float synthetic
    if enable_hdr and not quick:
        synth_hdr = generate_synthetic_hdr(output_dir, dims)
        sources.extend(synth_hdr)
        print(f"  HDR float synthetic: {len(synth_hdr)} images")

    # Wide gamut
    if enable_16bit and not quick:
        wide = generate_wide_gamut(output_dir, dims)
        sources.extend(wide)
        print(f"  Wide gamut: {len(wide)} images")

    # CMYK
    cmyk = generate_cmyk(output_dir, dims)
    sources.extend(cmyk)
    if cmyk:
        print(f"  CMYK: {len(cmyk)} images")

    # User-provided sources
    if user_sources_dir:
        user = scan_user_sources(user_sources_dir)
        sources.extend(user)
        if user:
            print(f"  User-provided: {len(user)} images from {user_sources_dir}")

    return sources


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Generate source images")
    parser.add_argument("--output", "-o", type=Path,
                        default=Path("/output/sources"),
                        help="Output directory for generated sources")
    parser.add_argument("--quick", action="store_true",
                        help="Reduced set for quick testing")
    parser.add_argument("--dimensions", type=str, default="",
                        help="Extra dimensions: '320x240,1920x1080,512'")
    parser.add_argument("--sources-dir", type=Path, default=None,
                        help="Directory of user-provided PNG/PPM/PFM images to include")
    parser.add_argument("--no-16bit", action="store_true",
                        help="Skip 16-bit source generation")
    parser.add_argument("--no-hdr", action="store_true",
                        help="Skip HDR float source generation")
    args = parser.parse_args()

    extra_dims = parse_dimensions(args.dimensions) if args.dimensions else None

    print(f"Generating source images in {args.output}")
    sources = generate_all(
        args.output, quick=args.quick,
        extra_dims=extra_dims,
        user_sources_dir=args.sources_dir,
        enable_16bit=not args.no_16bit,
        enable_hdr=not args.no_hdr,
    )
    print(f"Generated {len(sources)} source images")

    manifest_path = args.output / "sources.json"
    with open(manifest_path, "w") as f:
        json.dump(sources, f, indent=2)
    print(f"Source manifest: {manifest_path}")


if __name__ == "__main__":
    main()
