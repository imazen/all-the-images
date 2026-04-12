# all-the-images

Reproducible multi-codec test corpus generator. A Docker image compiles
every encoder from pinned source so the same inputs always produce
byte-identical outputs, regardless of what machine runs it.

The primary output is a **corpus directory** containing thousands of
encoded images across 8 formats plus a **manifest.json** that records
the encoder, version, parameters, content hash, and reference decoder
pixel hashes for every file. When multiple encoder versions produce
identical output, only one copy is stored — the manifest records all
co-producers so you can see exactly which versions agree and which diverge.

## Generating a corpus

```bash
# Build the Docker image (compiles ~20 encoders from source — takes a while the first time)
docker compose build

# Generate the full corpus into ./corpus/
docker compose run --rm generate

# Quick subset (~2k files, 15 seconds) for smoke testing
docker compose run --rm quick

# Only specific formats
docker compose run --rm generate --formats jpeg,png,webp

# Interactive shell with every encoder on $PATH
docker compose run --rm shell
```

### Re-encoding your own images

Drop PNG, PPM, PFM, TIFF, or EXR files into a directory and point `--sources-dir`
at it. Every file gets encoded through the full parameter matrix of every encoder:

```bash
# Re-encode a directory of PNGs through all encoders
docker compose run --rm generate --sources-dir /input

# Mount your image directory into the container
SOURCES_DIR=/path/to/my/images docker compose run --rm generate --sources-dir /input

# Direct docker run (no compose)
docker run --rm \
  -v ./corpus:/output \
  -v /path/to/my/images:/input:ro \
  all-the-images:latest --output /output --sources-dir /input
```

Image metadata (dimensions, bit depth, channels) is detected automatically via
ImageMagick `identify`. Supported formats: PNG, PPM, PGM, PFM, TIFF, EXR, JPEG, BMP.

### Custom dimensions

Add arbitrary dimensions beyond the built-in set. Useful for testing specific
resolution targets or non-standard aspect ratios:

```bash
# Add 1080p, 4K, and a square 512
docker compose run --rm generate --dimensions 1920x1080,3840x2160,512

# Combine with user sources and format selection
docker compose run --rm generate \
  --sources-dir /input \
  --dimensions 320x240,640x480 \
  --formats jpeg,avif,jxl
```

Dimensions are specified as `WxH` (e.g., `1920x1080`) or just `N` for square (e.g., `512` = `512x512`). Comma-separated, merged with the built-in set.

### Bit depths and HDR

Full-mode generation (not `--quick`) automatically includes:

| Bit depth | Format | Source type | Encoders that use it |
|-----------|--------|-------------|---------------------|
| 8-bit | PPM/PGM | sRGB synthetic + user images | All encoders |
| 16-bit | PNG | sRGB, Display P3 | PNG, AVIF, JXL, TIFF |
| 32-bit float | PFM | Linear, PQ (HDR highlights up to 10.0) | JXL, jpegli, TIFF |

Encoders automatically skip sources they can't handle — JPEG encoders (except
jpegli) skip 16-bit and HDR sources, WebP and GIF skip anything above 8-bit.
No manual configuration needed.

To disable higher bit depths (faster generation):

```bash
docker compose run --rm generate --no-16bit --no-hdr
```

After generation, `./corpus/` contains:

```
corpus/
├── manifest.json                    # everything about every file
├── sources/                         # synthetic PPM/PGM input images
├── jpeg/
│   ├── libjpeg-turbo-3.1.0/ab/ab3f…c2.jpg
│   ├── libjpeg-6b/…
│   ├── mozjpeg-4.1.5/…
│   └── …
├── png/
│   ├── imagemagick-convert/…
│   ├── optipng/…
│   └── …
├── webp/…
├── avif/…
├── jxl/…
├── gif/…
├── tiff/…
└── heic/…
```

Files are sharded by content hash: `<format>/<encoder-id>/<hash[0:2]>/<hash>.<ext>`.

## What's in the manifest

Every file gets a JSON entry with full provenance:

```json
{
  "path": "jpeg/libjpeg-turbo-3.1.0/ab/ab3f…c2.jpg",
  "blake3": "ab3f…c2…64hex",
  "bytes": 1234,
  "format": "jpeg",
  "encoder": "libjpeg-turbo-3.1.0",
  "source": "noise_32x32_rgb",
  "params": {
    "quality": 85,
    "subsampling": "2x2",
    "progressive": true,
    "optimize": true,
    "arithmetic": false,
    "restart": 0,
    "dct": "int"
  },
  "reference_decodes": {
    "djpeg-turbo-3.1.0": "…blake3 of decoded pixels…",
    "djpeg-mozjpeg-4.1.5": "…",
    "djpegli-0.11.1": "…"
  },
  "also_produced_by": [
    { "encoder": "libjpeg-turbo-2.1.5", "source": "noise_32x32_rgb", "params": { … } },
    { "encoder": "libjpeg-turbo-2.1.2", "source": "noise_32x32_rgb", "params": { … } }
  ]
}
```

The `also_produced_by` field lists every other encoder+params combination that
produced byte-identical output. This is the primary tool for answering "which
encoder versions are interchangeable and which aren't."

Top-level `manifest.json` also contains:
- `encoders` — metadata for every encoder (name, version, source URL, compile flags, Ubuntu version it shipped with)
- `sources` — metadata for every source image (dimensions, channels, pattern type)
- `decoders` — metadata for reference decoders used for pixel hashing
- `stats` — totals, unique count, failure count, co-production count

## Encoders

### JPEG (12 encoder versions)

| Encoder | Versions | Why these versions |
|---------|----------|-------------------|
| libjpeg-turbo | 1.3.0, 2.0.3, 2.1.2, 2.1.5, 3.1.0 | Every Ubuntu LTS since 14.04 shipped a different turbo |
| libjpeg-turbo (12-bit) | 3.1.0 | Separate 12-bit precision build |
| IJG libjpeg | 6b, 9b, 9d, 10 | 6b is the 1998 baseline still deployed everywhere; 9b-9d match Ubuntu 16.04-22.04; 10 is the latest (2026-01-25) |
| mozjpeg | 4.1.5 | Trellis quantization, progressive scan optimization |
| jpegli | 0.11.1 (libjxl) | XYB colorspace, adaptive quantization |
| guetzli | 1.0.1 | Butteraugli-optimized perceptual encoder |

All libjpeg-turbo builds enable `WITH_ARITH_ENC` and `WITH_ARITH_DEC` (distro
packages disable these). IJG v6b has no arithmetic coding or block size support;
v9+ adds both plus RGB identity encoding.

### PNG (4 encoders)

| Encoder | Source |
|---------|--------|
| ImageMagick convert | system (apt) — baseline PNG creation with depth/color/interlace/zlib-level axes |
| OptiPNG | system (apt) — optimization levels 0-7, interlace, strip |
| pngcrush | system (apt) — brute-force, method/filter matrix |
| zopflipng | 1.0.3 (from source) — iterations, filter strategies, lossy modes |

### WebP, AVIF, JXL, GIF, TIFF, HEIC

| Format | Encoder | Version |
|--------|---------|---------|
| WebP | cwebp (libwebp) | 1.5.0 — lossy + lossless parameter matrices |
| AVIF | avifenc (libavif + aom) | 1.2.1 — quality, speed, YUV, bit depth, lossless |
| JXL | cjxl (libjxl) | 0.11.1 — VarDCT + modular modes, distance/effort/progressive |
| GIF | gifsicle | 1.94 — optimization, colors, lossy, dither |
| GIF | ImageMagick | system — dither, ordered-dither, palette sizes |
| TIFF | ImageMagick | system — LZW, Zip, JPEG, Fax, Group4, PackBits, LZMA |
| TIFF | tiffcp (libtiff) | 4.7.0 — strip/tile layouts, recompression |
| HEIC | heif-enc (libheif + x265) | 1.19.7 — quality, lossless, bit depth |

## Source images

### Synthetic patterns

Deterministic test patterns — no external dependencies, no licensing concerns:

| Pattern | Description | What it exercises |
|---------|-------------|-------------------|
| noise | Uniform random pixels (fixed LCG seed) | General codec stress, entropy coding |
| patches | Noise + solid-color rectangles | Block boundary handling, DC prediction |
| checkerboard | High-contrast alternating blocks | DCT energy distribution |
| edges | Horizontal/vertical gradients | Directional frequency response |
| bands | Irregular-width stripes | Chroma subsampling boundaries |

Generated at three precision levels:
- **8-bit** (PPM/PGM) — all patterns, all dimensions, RGB + grayscale
- **16-bit** (PNG) — noise + checkerboard subset, sRGB + Display P3
- **32-bit float** (PFM) — noise + checkerboard, linear sRGB + PQ/HDR
  (highlights up to 10.0 nits for HDR encoder testing)

Built-in dimensions cover MCU-aligned (16, 32, 64, 128), non-aligned (17, 33, 65),
odd asymmetric (23×29, 31×17, 47×63), and minimal (7×7, 9×9). Extend with `--dimensions`.

### User-provided images

Any PNG, PPM, PGM, PFM, TIFF, EXR, JPEG, or BMP file in `--sources-dir` is
included alongside synthetics. Metadata (dimensions, bit depth, channels) is
detected via ImageMagick `identify`. Each user image gets encoded through the
full parameter matrix of every compatible encoder.

### Derived variants

CMYK (TIFF, via ImageMagick) and wide-gamut (Display P3, 16-bit PNG) sources
are derived automatically from the base synthetic images.

## Using the corpus in tests

Download a release tarball and validate your decoder against the reference
pixel hashes — no FFI or encoder builds needed:

```rust
use std::collections::HashMap;

/// For each JPEG in the corpus, decode it and check the pixel hash
/// matches what the reference decoder (djpeg from libjpeg-turbo) produced.
fn validate_decoder(corpus_dir: &Path) {
    let manifest: Manifest = serde_json::from_reader(
        File::open(corpus_dir.join("manifest.json")).unwrap()
    ).unwrap();

    for file in &manifest.files {
        if file.format != "jpeg" { continue; }

        let data = std::fs::read(corpus_dir.join(&file.path)).unwrap();
        let pixels = my_decoder::decode(&data).unwrap();
        let hash = blake3::hash(&pixels);

        // Compare against reference decoder output
        if let Some(expected) = file.reference_decodes.get("djpeg-turbo-3.1.0") {
            assert_eq!(
                hash.to_hex().to_string(), *expected,
                "Pixel mismatch: {} (encoder: {}, params: {:?})",
                file.path, file.encoder, file.params,
            );
        }
    }
}
```

The `also_produced_by` field answers version-compatibility questions:

```python
import json

manifest = json.load(open("corpus/manifest.json"))
for f in manifest["files"]:
    if f["format"] != "jpeg":
        continue
    coprod = f.get("also_produced_by", [])
    if coprod:
        versions = [f["encoder"]] + [c["encoder"] for c in coprod]
        print(f"{f['source']} q={f['params'].get('quality')}: "
              f"{', '.join(versions)} produce identical output")
```

## Deduplication

When multiple encoder versions produce byte-identical output for the same
source and parameters, only one file is stored. The manifest's
`also_produced_by` records every co-producer. This keeps the corpus small
while preserving the version-compatibility data that's the whole point of
running multiple versions.

Quick-mode example: 2,030 JPEG encoding tasks across 12 encoder versions
→ 1,718 succeeded → **701 unique files** on disk (285 with co-producers,
1,017 co-productions).

## Docker image

The image is published to GHCR on each release. CI in any repo can pull
it to regenerate or extend the corpus:

```bash
docker pull ghcr.io/imazen/all-the-images:latest
docker run --rm -v ./corpus:/output ghcr.io/imazen/all-the-images:latest --output /output
docker run --rm -v ./corpus:/output ghcr.io/imazen/all-the-images:latest --output /output --quick
docker run --rm -v ./corpus:/output ghcr.io/imazen/all-the-images:latest --output /output --formats jpeg,png
```

## Versioning

The corpus is a versioned release artifact with an immutable manifest schema.
Consumers pin to a version. Bumping the version regenerates the entire corpus.

## License

Scripts and generated source images are MIT-licensed. Encoder source code
retains its original license within the Docker build stages (IJG, BSD,
Apache-2.0, etc.). The encoded corpus output is unencumbered.
