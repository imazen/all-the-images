# Source Images

Source images for corpus generation are either:

1. **Synthetic** — generated deterministically by `scripts/generate_sources.py`
2. **Photographic** — CC0-licensed images committed here (v1+)

## Synthetic patterns

| Pattern | Description |
|---------|-------------|
| noise | Uniform random pixels (fixed LCG seed) |
| patches | Noise + solid-color rectangles |
| checkerboard | High-contrast alternating blocks |
| edges | Horizontal/vertical gradients |
| bands | Irregular-width stripes |

## Planned photographic sources (v1)

- Small (<100 KB) CC0 images covering: skin tones, foliage, sky, text,
  high-contrast, low-light, bokeh
- Wide-gamut (Display P3, Rec.2020) with ICC profiles
- CMYK sources for print-oriented testing
