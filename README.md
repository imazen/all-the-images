# all-the-images

Reproducible multi-codec test corpus for the zen image codec family.

Every encoder is built from a pinned source commit inside a Docker image, so
rebuilding on any machine produces byte-identical output. Downstream zen crates
consume the corpus via release tarballs — no local encoder builds required.

## Quick start

```bash
# Build the Docker image (compiles all encoders from source)
docker compose build

# Generate the full corpus (writes to ./corpus/)
docker compose run --rm generate

# Generate a quick subset for smoke testing
docker compose run --rm generate-quick
```

## What's inside

### JPEG encoders (v0)

| Encoder | Version | Source | Notable flags |
|---------|---------|--------|---------------|
| libjpeg (IJG) | 9e | ijg.org | arithmetic, block sizes 1-16 |
| libjpeg-turbo | 3.1.0 | GitHub | `WITH_ARITH_ENC/DEC`, 12-bit |
| libjpeg-turbo (12-bit) | 3.1.0 | GitHub | Separate 12-bit precision build |
| mozjpeg | 4.1.5 | GitHub | Trellis quant, arithmetic, progressive |
| jpegli (cjpegli) | libjxl v0.11.1 | GitHub | XYB, adaptive quant, progressive levels |
| guetzli | 1.0.1 | GitHub | Perceptual, butteraugli-optimized |

### Planned (v1+)

- **PNG**: libpng, oxipng, pngcrush, zopflipng, lodepng, zenpng
- **WebP**: libwebp (lossy + lossless), zenwebp
- **AVIF**: libavif+aom, libavif+rav1e, libavif+SVT-AV1, zenavif
- **JPEG XL**: libjxl (cjxl), zenjxl
- **GIF**: gifsicle, ImageMagick
- **TIFF**: libtiff (LZW, Deflate, JPEG-in-TIFF, CCITT G3/G4)
- **HEIC**: libheif+x265

### Source images

Synthetic test patterns generated deterministically:

| Pattern | Description | Exercises |
|---------|-------------|-----------|
| noise | Uniform random pixels | General codec stress |
| patches | Noise + solid-color rectangles | Block boundary handling |
| checkerboard | High-contrast alternating blocks | DCT energy distribution |
| edges | Horizontal/vertical gradients | Directional frequency response |
| bands | Irregular-width stripes | Chroma subsampling boundaries |

Dimensions include MCU-aligned (16, 32, 64, 128), non-aligned (17, 33, 65),
odd asymmetric (23x29, 31x17), and minimal (7x7, 9x9). Both RGB and grayscale.

### Parameter permutations (JPEG)

Each source image is encoded with a matrix of parameters per encoder:

- **Quality**: 10 levels (Q1 through Q100)
- **Chroma subsampling**: 4:4:4, 4:2:2, 4:2:0, 4:4:0, 4:1:1
- **Progressive**: on/off (+ jpegli levels 0-2)
- **Huffman optimization**: on/off
- **Arithmetic coding**: on/off (libjpeg v9+, turbo with flag, mozjpeg)
- **Restart markers**: 0, 1, 8 MCU intervals
- **DCT method**: int, fast, float
- **Encoder-specific**: XYB colorspace, trellis quant, smoothing, baseline mode

### Output structure

```
corpus/
├── manifest.json              # Full corpus manifest with per-file metadata
├── jpeg/
│   ├── libjpeg-turbo-3.1.0/
│   │   ├── <hash>.jpg         # Sharded by first 2 hex chars
│   │   └── ...
│   ├── mozjpeg-4.1.5/
│   ├── jpegli-0.11.1/
│   ├── libjpeg-9e/
│   └── guetzli-1.0.1/
└── sources/
    ├── noise_32x32_rgb.ppm
    └── ...
```

### Manifest

`manifest.json` contains per-file metadata:

```json
{
  "schema_version": "0.1.0",
  "generated_at": "2026-04-11T12:00:00Z",
  "docker_image_hash": "sha256:...",
  "encoders": { ... },
  "files": [
    {
      "path": "jpeg/libjpeg-turbo-3.1.0/ab/ab3f...c2.jpg",
      "blake3": "...",
      "bytes": 1234,
      "format": "jpeg",
      "encoder": "libjpeg-turbo-3.1.0",
      "source": "noise_32x32_rgb",
      "params": { "quality": 85, "subsampling": "4:2:0", ... },
      "reference_decodes": {
        "djpeg-turbo-3.1.0": "blake3-of-decoded-pixels",
        "djpeg-mozjpeg-4.1.5": "blake3-of-decoded-pixels"
      }
    }
  ]
}
```

## Consumption

Downstream zen crates pull the corpus via the `codec-corpus` crate, which
fetches release tarballs from GitHub. A `corpus-tests` feature flag gates
expensive tests so they're opt-in:

```rust
#[cfg(feature = "corpus-tests")]
#[test]
fn decode_all_corpus_jpegs() {
    let corpus = all_the_images::jpeg_corpus().unwrap();
    for entry in corpus.files() {
        let decoded = my_decoder.decode(&entry.bytes);
        // Compare pixel hash against reference
        assert_eq!(
            blake3::hash(&decoded.pixels),
            entry.reference_decodes["djpeg-turbo-3.1.0"]
        );
    }
}
```

## Docker image

Published to `ghcr.io/imazen/all-the-images`. CI in any zen repo can pull and
re-run generation on demand:

```bash
docker pull ghcr.io/imazen/all-the-images:0.1.0
docker run --rm -v ./corpus:/output ghcr.io/imazen/all-the-images:0.1.0
```

## Versioning

The corpus is a versioned release artifact. Consumers pin to a version.
Bumping the version means re-generating the entire corpus — output is
immutable per version.

## License

Source images are synthetic (generated) or CC0. Encoder source code retains
its original license (IJG, BSD, Apache-2.0, etc.) within the Docker build
stages. The corpus output (encoded images) and scripts in this repo are
MIT-licensed.
