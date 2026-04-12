#!/usr/bin/env python3
"""Synthetic source image generator for all-the-images corpus.

Generates deterministic test patterns as PPM (RGB) and PGM (grayscale) files.
These are the same patterns used by zenjpeg's gen_permutation_corpus.rs,
reimplemented in Python for Docker portability (no Rust toolchain needed).

Source images exercise:
- MCU-aligned dimensions (8, 16, 32, 64, 128)
- Non-MCU-aligned dimensions (7, 9, 17, 33, 65)
- Odd asymmetric dimensions (23x29, 31x17, 47x63)
- Both RGB (3-channel) and grayscale (1-channel)

All random generators use a fixed LCG seed for reproducibility.
"""

import os
import struct
import sys
from pathlib import Path


def lcg(seed: int) -> tuple[int, int]:
    """Linear congruential generator matching the Rust version."""
    seed = (seed * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
    return seed, (seed >> 33) & 0xFF


def gen_noise(w: int, h: int, c: int, seed: int) -> bytearray:
    """Uniform random pixels."""
    out = bytearray(w * h * c)
    s = seed
    for i in range(w * h * c):
        s, val = lcg(s)
        out[i] = val & 0xFF
    return out


def gen_noise_patches(w: int, h: int, c: int, seed: int) -> bytearray:
    """Random noise with solid-color rectangular patches overlaid."""
    out = gen_noise(w, h, c, seed)
    s = seed ^ 0xDEADBEEF

    s, n_extra = lcg(s)
    n_patches = 6 + (n_extra % 6)

    for _ in range(n_patches):
        s, px = lcg(s)
        px = px % w
        s, py = lcg(s)
        py = py % h
        s, pw = lcg(s)
        pw = 1 + (pw % max(w // 3, 1))
        s, ph = lcg(s)
        ph = 1 + (ph % max(h // 3, 1))

        col = [0, 0, 0]
        for k in range(3):
            s, v = lcg(s)
            col[k] = v & 0xFF

        for y in range(py, min(py + ph, h)):
            for x in range(px, min(px + pw, w)):
                idx = (y * w + x) * c
                for k in range(c):
                    out[idx + k] = col[min(k, 2)]

    return out


def gen_checkerboard(w: int, h: int, c: int, block: int) -> bytearray:
    """High-contrast alternating blocks."""
    out = bytearray(w * h * c)
    for y in range(h):
        for x in range(w):
            v = 240 if ((x // block) ^ (y // block)) & 1 == 0 else 16
            idx = (y * w + x) * c
            for k in range(c):
                out[idx + k] = v
    return out


def gen_edges(w: int, h: int, c: int, direction: str) -> bytearray:
    """Gradient patterns for directional frequency response."""
    out = bytearray(w * h * c)
    for y in range(h):
        for x in range(w):
            if direction == "x":
                v = int(255 * x / max(w - 1, 1))
            else:
                v = int(255 * y / max(h - 1, 1))
            idx = (y * w + x) * c
            for k in range(c):
                out[idx + k] = v
    return out


def gen_bands(w: int, h: int, c: int, horizontal: bool) -> bytearray:
    """Irregular-width stripes at non-uniform positions."""
    out = bytearray(w * h * c)
    # Fixed stripe positions (irregular spacing exercises subsampling boundaries)
    stripes = [0, 3, 7, 8, 15, 16, 23, 24, 31, 39, 47, 48, 63, 64, 95, 127]
    stripe_set = set(stripes)

    for y in range(h):
        for x in range(w):
            pos = y if horizontal else x
            v = 220 if pos in stripe_set else 35
            idx = (y * w + x) * c
            for k in range(c):
                out[idx + k] = v
    return out


def write_ppm(path: Path, w: int, h: int, data: bytearray):
    """Write a binary PPM (P6) file."""
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode())
        f.write(data)


def write_pgm(path: Path, w: int, h: int, data: bytearray):
    """Write a binary PGM (P5) file."""
    with open(path, "wb") as f:
        f.write(f"P5\n{w} {h}\n255\n".encode())
        f.write(data)


# Dimension configurations matching gen_permutation_corpus.rs
# (w, h) tuples — covers MCU-aligned, non-aligned, asymmetric, minimal
DIMENSIONS_FULL = [
    (7, 7),     # Below single MCU
    (9, 9),     # Below single MCU
    (16, 16),   # Exact single MCU (4:2:0)
    (17, 17),   # 1px overhang
    (23, 29),   # Odd asymmetric
    (31, 17),   # Odd asymmetric
    (32, 32),   # 2 MCUs
    (33, 33),   # 2 MCUs + 1px
    (47, 63),   # Odd asymmetric
    (64, 64),   # 4 MCUs
    (65, 65),   # 4 MCUs + 1px
    (96, 96),   # 6 MCUs
    (128, 128), # 8 MCUs
]

DIMENSIONS_QUICK = [
    (7, 7),
    (16, 16),
    (32, 32),
    (64, 64),
]

# Seed base values for deterministic generation
SEED_NOISE = 0x12345678_9ABCDEF0
SEED_PATCHES = 0xFEDCBA98_76543210


def generate_all(output_dir: Path, quick: bool = False):
    """Generate all synthetic source images."""
    output_dir.mkdir(parents=True, exist_ok=True)

    dims = DIMENSIONS_QUICK if quick else DIMENSIONS_FULL
    sources = []

    for w, h in dims:
        for channels, ext, label in [(3, "ppm", "rgb"), (1, "pgm", "gray")]:
            # Noise
            name = f"noise_{w}x{h}_{label}"
            path = output_dir / f"{name}.{ext}"
            data = gen_noise(w, h, channels, SEED_NOISE ^ (w * 10000 + h))
            writer = write_ppm if channels == 3 else write_pgm
            writer(path, w, h, data)
            sources.append({"name": name, "path": str(path), "w": w, "h": h,
                            "channels": channels, "type": "noise"})

            # Noise + patches (skip very small sizes — patches need room)
            if w >= 16 and h >= 16:
                name = f"patches_{w}x{h}_{label}"
                path = output_dir / f"{name}.{ext}"
                data = gen_noise_patches(w, h, channels,
                                         SEED_PATCHES ^ (w * 10000 + h))
                writer(path, w, h, data)
                sources.append({"name": name, "path": str(path), "w": w, "h": h,
                                "channels": channels, "type": "patches"})

            # Checkerboard (block size 4 for small, 8 for large)
            block = 4 if w < 32 else 8
            name = f"checker_{w}x{h}_{label}"
            path = output_dir / f"{name}.{ext}"
            data = gen_checkerboard(w, h, channels, block)
            writer(path, w, h, data)
            sources.append({"name": name, "path": str(path), "w": w, "h": h,
                            "channels": channels, "type": "checkerboard"})

            # Edges (both directions, RGB only — grayscale gradient is boring)
            if channels == 3:
                for direction in ["x", "y"]:
                    name = f"edges_{direction}_{w}x{h}_{label}"
                    path = output_dir / f"{name}.{ext}"
                    data = gen_edges(w, h, channels, direction)
                    writer(path, w, h, data)
                    sources.append({"name": name, "path": str(path), "w": w, "h": h,
                                    "channels": channels, "type": "edges"})

            # Bands (horizontal + vertical, skip tiny sizes)
            if w >= 16 and h >= 16:
                for horiz in [True, False]:
                    orient = "h" if horiz else "v"
                    name = f"bands_{orient}_{w}x{h}_{label}"
                    path = output_dir / f"{name}.{ext}"
                    data = gen_bands(w, h, channels, horiz)
                    writer(path, w, h, data)
                    sources.append({"name": name, "path": str(path), "w": w, "h": h,
                                    "channels": channels, "type": "bands"})

    # Generate CMYK source via ImageMagick (if available)
    # CMYK JPEGs are important for testing but PPM can't represent CMYK.
    # We create a TIFF intermediate that cjpeg can read.
    try:
        import subprocess
        for w, h in [(32, 32), (64, 64)]:
            name = f"cmyk_{w}x{h}"
            ppm_path = output_dir / f"noise_{w}x{h}_rgb.ppm"
            tiff_path = output_dir / f"{name}.tiff"
            if ppm_path.exists():
                subprocess.run(
                    ["convert", str(ppm_path), "-colorspace", "CMYK",
                     str(tiff_path)],
                    check=True, capture_output=True
                )
                sources.append({"name": name, "path": str(tiff_path),
                                "w": w, "h": h, "channels": 4, "type": "cmyk"})
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("  [skip] ImageMagick not available, skipping CMYK sources",
              file=sys.stderr)

    return sources


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate synthetic source images")
    parser.add_argument("--output", "-o", type=Path,
                        default=Path("/output/sources"),
                        help="Output directory for source images")
    parser.add_argument("--quick", action="store_true",
                        help="Generate reduced dimension set for quick testing")
    args = parser.parse_args()

    print(f"Generating source images in {args.output}")
    sources = generate_all(args.output, quick=args.quick)
    print(f"Generated {len(sources)} source images")

    # Write source manifest for downstream scripts
    import json
    manifest_path = args.output / "sources.json"
    with open(manifest_path, "w") as f:
        json.dump(sources, f, indent=2)
    print(f"Source manifest: {manifest_path}")


if __name__ == "__main__":
    main()
