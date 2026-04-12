#!/usr/bin/env python3
"""PNG encoding permutations — stub for v1.

Planned encoders:
  - libpng (multiple versions, including 1.2 series)
  - oxipng (Rust optimizer)
  - pngcrush
  - pngout
  - ECT (Efficient Compression Tool)
  - zopflipng
  - lodepng (header-only C)
  - zenpng, zenzop

Parameter axes:
  - Compression level
  - Filter strategy (none, sub, up, average, paeth, adaptive)
  - Interlace (none, Adam7)
  - Bit depth (1, 2, 4, 8, 16)
  - Color type (grayscale, RGB, indexed, grayscale+alpha, RGBA)
  - zlib vs zlib-ng compression backend
"""

def main():
    print("PNG encoding: not yet implemented (v1)")
    print("See scripts/encode_png.py for planned scope")

if __name__ == "__main__":
    main()
