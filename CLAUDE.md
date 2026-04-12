# all-the-images Project Guide

Reproducible multi-codec test corpus for zen image codecs.

## Quick Reference

```bash
# Build Docker image
docker compose build

# Generate full corpus
docker compose run --rm generate

# Generate quick subset
docker compose run --rm quick

# Interactive shell with all encoders
docker compose run --rm shell

# Local smoke test (no Docker, uses system encoders)
CJPEG_TURBO=/usr/bin/cjpeg DJPEG_TURBO=/usr/bin/djpeg \
CJPEGLI=/usr/local/bin/cjpegli \
python3 scripts/generate.py --output /tmp/corpus --quick --skip-reference
```

## Architecture

```
scripts/generate.py          # Orchestrator (Docker ENTRYPOINT)
  ├── generate_sources.py    # Synthetic source images (PPM/PGM)
  ├── encode_jpeg.py         # JPEG encoding permutations
  ├── build_manifest.py      # Manifest assembly
  └── compute_reference.py   # Reference decoder pixel hashes
```

Pipeline: sources → encode → manifest → reference hashes → final manifest.json

## Encoders

All encoders are compiled from pinned source inside Docker stages.
Environment variables hold fully-qualified paths to avoid $PATH conflicts:

| Env var | Encoder | Version |
|---------|---------|---------|
| CJPEG_IJG | libjpeg (IJG) | 9e |
| CJPEG_TURBO | libjpeg-turbo | 3.1.0 |
| CJPEG_TURBO_12BIT | libjpeg-turbo 12-bit | 3.1.0 |
| CJPEG_MOZ | mozjpeg | 4.1.5 |
| CJPEGLI | jpegli | 0.11.1 (libjxl) |
| GUETZLI | guetzli | 1.0.1 |

## Adding a new encoder

1. Add a build stage in `Dockerfile` (pin version via git tag)
2. Add COPY + ENV in the runtime stage
3. Add a `build_<encoder>_tasks()` function in `scripts/encode_jpeg.py`
4. Add encoder metadata in `scripts/build_manifest.py`
5. Rebuild: `docker compose build`

## Adding a new format

1. Create `scripts/encode_<format>.py` following `encode_jpeg.py` pattern
2. Add encoder build stages to `Dockerfile`
3. Wire into `scripts/generate.py` orchestrator
4. Update `manifest/schema.json` format enum

## Known Issues

- guetzli is extremely slow (minutes per image). Only used on sources ≤64x64.
- cjpegli rejects small images (<8px) with 4:2:0 subsampling.
- 12-bit libjpeg-turbo produces 12-bit JPEGs that most decoders can't read.
  These are valuable test cases, not bugs.
