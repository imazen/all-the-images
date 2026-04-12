# all-the-images: reproducible multi-codec corpus generator
#
# Multi-stage build compiling every encoder from pinned source with
# non-default flags enabled. Each encoder gets its own stage so builds
# are cached independently.
#
# Usage:
#   docker build -t all-the-images .
#   docker run --rm -v ./corpus:/output all-the-images
#
# The runtime stage contains all encoder/decoder binaries plus Python
# scripts for corpus generation and manifest assembly.

# ============================================================================
# Stage: base — shared build toolchain
# ============================================================================
FROM ubuntu:24.04 AS base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        ninja-build \
        nasm \
        yasm \
        autoconf \
        automake \
        libtool \
        pkg-config \
        git \
        ca-certificates \
        curl \
        wget \
    && rm -rf /var/lib/apt/lists/*

# ============================================================================
# Stage: libjpeg-classic — IJG libjpeg v9e + v10
#
# IJG libjpeg is the original reference implementation. v9+ supports
# arithmetic coding, block sizes 1-16 (-block N), RGB identity encoding
# (-rgb1), and big gamut YCC (-bgycc) — features no other implementation has.
#
# We build both v9e (in Ubuntu 24.04, most current deployments) and v10
# (released 2026-01-25, not in any Ubuntu yet) to catch compatibility
# differences between the two.
#
# Ubuntu libjpeg history:
#   14.04 Trusty:  turbo 1.3.0, IJG 6b
#   16.04 Xenial:  turbo 1.4.2, IJG 9b
#   18.04 Bionic:  turbo 1.5.2, IJG 9b
#   20.04 Focal:   turbo 2.0.3, IJG 9d
#   22.04 Jammy:   turbo 2.1.2, IJG 9d
#   24.04 Noble:   turbo 2.1.5, IJG 9e
#   25.04 Plucky:  turbo 2.1.5, IJG 9f
#   v10 released 2026-01-25 — not yet in any Ubuntu
# ============================================================================
FROM base AS libjpeg-classic

# Build IJG versions that were actually shipped in Ubuntu:
#   6b  — Ubuntu 14.04 (the ancient baseline, still widely deployed)
#   9b  — Ubuntu 16.04/18.04 (first v9 in Ubuntu)
#   9d  — Ubuntu 20.04/22.04
#   10  — released 2026-01-25, not yet in any Ubuntu
# Skipping 9e/9f (incremental between 9d and 10, less interesting).
RUN for v in 6b 9b 9d 10; do \
        curl -fsSL "https://www.ijg.org/files/jpegsrc.v${v}.tar.gz" \
            | tar xz -C /tmp \
        && cd /tmp/jpeg-${v}* \
        && ./configure --prefix=/opt/libjpeg-${v} \
        && make -j"$(nproc)" \
        && make install \
        && make clean \
        && cd /; \
    done

# ============================================================================
# Stage: libjpeg-turbo — multiple versions matching Ubuntu history
#
# Every Ubuntu LTS since 14.04 shipped a different turbo version. We build
# all of them with arithmetic coding enabled (distro packages disable it).
# Dedup in the manifest shows which versions produce identical output.
#
#   1.3.0 — Ubuntu 14.04 Trusty
#   1.4.2 — Ubuntu 16.04 Xenial
#   1.5.2 — Ubuntu 18.04 Bionic
#   2.0.3 — Ubuntu 20.04 Focal
#   2.1.2 — Ubuntu 22.04 Jammy
#   2.1.5 — Ubuntu 24.04 Noble / 25.04 Plucky
#   3.1.0 — latest upstream
# ============================================================================
FROM base AS libjpeg-turbo

# Build each version into its own prefix.
# Note: 1.3.0 uses autotools, not cmake. 1.4.2+ use cmake.
# v1.3.0 (autotools)
RUN git clone --depth 1 --branch 1.3.0 \
        https://github.com/libjpeg-turbo/libjpeg-turbo.git /tmp/turbo-1.3.0 \
    && cd /tmp/turbo-1.3.0 \
    && autoreconf -fiv \
    && ./configure --prefix=/opt/libjpeg-turbo-1.3.0 \
        --with-arith-enc --with-arith-dec \
    && make -j"$(nproc)" \
    && make install

# v1.4.2+ (cmake)
RUN for tag in 1.4.2 1.5.2 2.0.3 2.1.2 2.1.5 3.1.0; do \
        git clone --depth 1 --branch ${tag} \
            https://github.com/libjpeg-turbo/libjpeg-turbo.git /tmp/turbo-${tag} \
        && cd /tmp/turbo-${tag} \
        && cmake -B build -G Ninja \
            -DCMAKE_INSTALL_PREFIX=/opt/libjpeg-turbo-${tag} \
            -DCMAKE_BUILD_TYPE=Release \
            -DWITH_ARITH_ENC=1 \
            -DWITH_ARITH_DEC=1 \
            -DWITH_TURBOJPEG=0 \
        && cmake --build build -j"$(nproc)" \
        && cmake --install build \
        && rm -rf /tmp/turbo-${tag} \
        && cd /; \
    done

# ============================================================================
# Stage: libjpeg-turbo-12bit — separate 12-bit precision build (latest only)
#
# 12-bit mode is a compile-time switch that changes the entire library.
# Can't have 8-bit and 12-bit in the same binary.
# ============================================================================
FROM base AS libjpeg-turbo-12bit

ARG TURBO_VERSION=3.1.0
RUN git clone --depth 1 --branch ${TURBO_VERSION} \
        https://github.com/libjpeg-turbo/libjpeg-turbo.git /tmp/libjpeg-turbo \
    && cd /tmp/libjpeg-turbo \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/libjpeg-turbo-${TURBO_VERSION}-12bit \
        -DCMAKE_BUILD_TYPE=Release \
        -DWITH_ARITH_ENC=1 \
        -DWITH_ARITH_DEC=1 \
        -DWITH_TURBOJPEG=0 \
        -DWITH_12BIT=1 \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build

# ============================================================================
# Stage: mozjpeg — Mozilla's optimizing JPEG encoder
#
# Trellis quantization, progressive scan optimization, arithmetic coding.
# ============================================================================
FROM base AS mozjpeg

ARG MOZJPEG_VERSION=4.1.5
RUN git clone --depth 1 --branch v${MOZJPEG_VERSION} \
        https://github.com/mozilla/mozjpeg.git /tmp/mozjpeg \
    && cd /tmp/mozjpeg \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/mozjpeg-${MOZJPEG_VERSION} \
        -DCMAKE_BUILD_TYPE=Release \
        -DWITH_ARITH_ENC=1 \
        -DWITH_ARITH_DEC=1 \
        -DPNG_SUPPORTED=0 \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build

# ============================================================================
# Stage: jpegli — Google's JPEG encoder from libjxl
#
# XYB colorspace, adaptive quantization, progressive levels 0-2.
# Built from libjxl source — we only extract cjpegli/djpegli binaries.
# ============================================================================
FROM base AS jpegli

ARG LIBJXL_VERSION=0.11.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgif-dev libpng-dev zlib1g-dev libbrotli-dev \
    && rm -rf /var/lib/apt/lists/*

# libhwy (Highway SIMD library) — build from source for a known version
ARG HWY_VERSION=1.2.0
RUN git clone --depth 1 --branch ${HWY_VERSION} \
        https://github.com/google/highway.git /tmp/highway \
    && cd /tmp/highway \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_BUILD_TYPE=Release \
        -DHWY_ENABLE_TESTS=OFF \
        -DHWY_ENABLE_EXAMPLES=OFF \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build

RUN git clone --depth 1 --branch v${LIBJXL_VERSION} --recurse-submodules --shallow-submodules \
        https://github.com/libjxl/libjxl.git /tmp/libjxl \
    && cd /tmp/libjxl \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/jpegli-${LIBJXL_VERSION} \
        -DCMAKE_BUILD_TYPE=Release \
        -DJPEGXL_ENABLE_TOOLS=ON \
        -DJPEGXL_ENABLE_JPEGLI=ON \
        -DJPEGXL_ENABLE_MANPAGES=OFF \
        -DJPEGXL_ENABLE_BENCHMARK=OFF \
        -DJPEGXL_ENABLE_EXAMPLES=OFF \
        -DJPEGXL_ENABLE_DOXYGEN=OFF \
        -DBUILD_TESTING=OFF \
    && cmake --build build -j"$(nproc)" --target cjpegli djpegli cjxl djxl \
    && mkdir -p /opt/jpegli-${LIBJXL_VERSION}/bin \
           /opt/jpegli-${LIBJXL_VERSION}/lib \
    && cp build/tools/cjpegli build/tools/djpegli \
          build/tools/cjxl build/tools/djxl \
          /opt/jpegli-${LIBJXL_VERSION}/bin/ \
    && cp -a build/lib/*.so* /opt/jpegli-${LIBJXL_VERSION}/lib/ 2>/dev/null || true

# ============================================================================
# Stage: guetzli — Google's perceptual JPEG encoder
#
# Butteraugli-optimized, very slow but produces excellent quality.
# Only useful at Q84+ (its minimum). Limited to 8-bit sRGB input.
# ============================================================================
FROM base AS guetzli

ARG GUETZLI_VERSION=1.0.1
RUN apt-get update && apt-get install -y --no-install-recommends libpng-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch v${GUETZLI_VERSION} \
        https://github.com/google/guetzli.git /tmp/guetzli \
    && cd /tmp/guetzli \
    && make -j"$(nproc)" \
    && mkdir -p /opt/guetzli-${GUETZLI_VERSION}/bin \
    && cp bin/Release/guetzli /opt/guetzli-${GUETZLI_VERSION}/bin/

# ============================================================================
# Stage: libwebp — WebP encoder/decoder (lossy + lossless)
# ============================================================================
FROM base AS libwebp

ARG LIBWEBP_VERSION=1.5.0
RUN git clone --depth 1 --branch v${LIBWEBP_VERSION} \
        https://github.com/webmproject/libwebp.git /tmp/libwebp \
    && cd /tmp/libwebp \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/libwebp-${LIBWEBP_VERSION} \
        -DCMAKE_BUILD_TYPE=Release \
        -DWEBP_BUILD_CWEBP=ON \
        -DWEBP_BUILD_DWEBP=ON \
        -DWEBP_BUILD_GIF2WEBP=OFF \
        -DWEBP_BUILD_IMG2WEBP=OFF \
        -DWEBP_BUILD_WEBPINFO=OFF \
        -DWEBP_BUILD_WEBPMUX=OFF \
        -DWEBP_BUILD_EXTRAS=OFF \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build

# ============================================================================
# Stage: aom — AV1 codec (used by libavif)
# ============================================================================
FROM base AS aom

ARG AOM_VERSION=3.12.0
RUN git clone --depth 1 --branch v${AOM_VERSION} \
        https://aomedia.googlesource.com/aom /tmp/aom \
    && cd /tmp/aom \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/aom-${AOM_VERSION} \
        -DCMAKE_BUILD_TYPE=Release \
        -DENABLE_DOCS=OFF \
        -DENABLE_EXAMPLES=OFF \
        -DENABLE_TESTDATA=OFF \
        -DENABLE_TESTS=OFF \
        -DENABLE_TOOLS=OFF \
        -DBUILD_SHARED_LIBS=ON \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build

# ============================================================================
# Stage: libavif — AVIF encoder/decoder with aom backend
# ============================================================================
FROM aom AS libavif

ARG LIBAVIF_VERSION=1.2.1
ARG AOM_VERSION=3.12.0
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpng-dev libjpeg-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch v${LIBAVIF_VERSION} \
        https://github.com/AOMediaCodec/libavif.git /tmp/libavif \
    && cd /tmp/libavif \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/libavif-${LIBAVIF_VERSION} \
        -DCMAKE_BUILD_TYPE=Release \
        -DAVIF_CODEC_AOM=SYSTEM \
        -DAVIF_LIBYUV=OFF \
        -DAVIF_BUILD_APPS=ON \
        -DCMAKE_PREFIX_PATH=/opt/aom-${AOM_VERSION} \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build \
    && cp -a /opt/aom-${AOM_VERSION}/lib/*.so* /opt/libavif-${LIBAVIF_VERSION}/lib/ 2>/dev/null || true

# ============================================================================
# Stage: zopfli — Google's zopfli compression (zopflipng for PNG optimization)
# ============================================================================
FROM base AS zopfli

ARG ZOPFLI_VERSION=1.0.3
RUN git clone --depth 1 --branch zopfli-${ZOPFLI_VERSION} \
        https://github.com/google/zopfli.git /tmp/zopfli \
    && cd /tmp/zopfli \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/zopfli-${ZOPFLI_VERSION} \
        -DCMAKE_BUILD_TYPE=Release \
        -DZOPFLI_BUILD_SHARED=OFF \
    && cmake --build build -j"$(nproc)" \
    && mkdir -p /opt/zopfli-${ZOPFLI_VERSION}/bin \
    && cp build/zopflipng /opt/zopfli-${ZOPFLI_VERSION}/bin/

# ============================================================================
# Stage: gifsicle — GIF optimizer
# ============================================================================
FROM base AS gifsicle

ARG GIFSICLE_VERSION=1.94
RUN git clone --depth 1 --branch v${GIFSICLE_VERSION} \
        https://github.com/kohler/gifsicle.git /tmp/gifsicle \
    && cd /tmp/gifsicle \
    && autoreconf -i \
    && ./configure --prefix=/opt/gifsicle-${GIFSICLE_VERSION} \
    && make -j"$(nproc)" \
    && make install

# ============================================================================
# Stage: libtiff — TIFF tools (tiffcp, tiffinfo, etc.)
# ============================================================================
FROM base AS libtiff

ARG LIBTIFF_VERSION=4.7.0
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg-dev zlib1g-dev liblzma-dev libzstd-dev libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch v${LIBTIFF_VERSION} \
        https://gitlab.com/libtiff/libtiff.git /tmp/libtiff \
    && cd /tmp/libtiff \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/libtiff-${LIBTIFF_VERSION} \
        -DCMAKE_BUILD_TYPE=Release \
        -Dtiff-tools=ON \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build

# ============================================================================
# Stage: libheif — HEIC encoder/decoder with x265 backend
# ============================================================================
FROM base AS libheif

ARG X265_VERSION=4.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpng-dev libjpeg-dev libde265-dev \
    && rm -rf /var/lib/apt/lists/*

# Build x265
RUN git clone --depth 1 --branch ${X265_VERSION} \
        https://bitbucket.org/multicoreware/x265_git.git /tmp/x265 \
    && cd /tmp/x265/source \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DCMAKE_BUILD_TYPE=Release \
        -DENABLE_SHARED=ON \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build

ARG LIBHEIF_VERSION=1.19.7
RUN git clone --depth 1 --branch v${LIBHEIF_VERSION} \
        https://github.com/strukturag/libheif.git /tmp/libheif \
    && cd /tmp/libheif \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/libheif-${LIBHEIF_VERSION} \
        -DCMAKE_BUILD_TYPE=Release \
        -DWITH_EXAMPLES=ON \
        -DWITH_GDK_PIXBUF=OFF \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build

# ============================================================================
# Stage: runtime — all binaries + Python + generation scripts
# ============================================================================
FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        libpng16-16t64 \
        libgif7 \
        libbrotli1 \
        libde265-0 \
        liblzma5 \
        libzstd1 \
        imagemagick \
        optipng \
        pngcrush \
    && rm -rf /var/lib/apt/lists/*

# blake3 for content hashing
RUN pip3 install --no-cache-dir --break-system-packages blake3==1.0.4

# ── IJG libjpeg versions ──
COPY --from=libjpeg-classic     /opt/libjpeg-6b                    /opt/libjpeg-6b
COPY --from=libjpeg-classic     /opt/libjpeg-9b                    /opt/libjpeg-9b
COPY --from=libjpeg-classic     /opt/libjpeg-9d                    /opt/libjpeg-9d
COPY --from=libjpeg-classic     /opt/libjpeg-10                    /opt/libjpeg-10

# ── libjpeg-turbo versions (matching Ubuntu LTS history) ──
COPY --from=libjpeg-turbo       /opt/libjpeg-turbo-1.3.0           /opt/libjpeg-turbo-1.3.0
COPY --from=libjpeg-turbo       /opt/libjpeg-turbo-1.4.2           /opt/libjpeg-turbo-1.4.2
COPY --from=libjpeg-turbo       /opt/libjpeg-turbo-1.5.2           /opt/libjpeg-turbo-1.5.2
COPY --from=libjpeg-turbo       /opt/libjpeg-turbo-2.0.3           /opt/libjpeg-turbo-2.0.3
COPY --from=libjpeg-turbo       /opt/libjpeg-turbo-2.1.2           /opt/libjpeg-turbo-2.1.2
COPY --from=libjpeg-turbo       /opt/libjpeg-turbo-2.1.5           /opt/libjpeg-turbo-2.1.5
COPY --from=libjpeg-turbo       /opt/libjpeg-turbo-3.1.0           /opt/libjpeg-turbo-3.1.0
COPY --from=libjpeg-turbo-12bit /opt/libjpeg-turbo-3.1.0-12bit     /opt/libjpeg-turbo-3.1.0-12bit
COPY --from=mozjpeg             /opt/mozjpeg-4.1.5                  /opt/mozjpeg-4.1.5
COPY --from=jpegli              /opt/jpegli-0.11.1                  /opt/jpegli-0.11.1
COPY --from=guetzli             /opt/guetzli-1.0.1                  /opt/guetzli-1.0.1

# ── WebP ──
COPY --from=libwebp             /opt/libwebp-1.5.0                  /opt/libwebp-1.5.0

# ── AVIF ──
COPY --from=libavif             /opt/libavif-1.2.1                  /opt/libavif-1.2.1

# ── JPEG XL (cjxl/djxl shared with jpegli stage) ──
# Already copied via jpegli stage above (cjxl/djxl live alongside cjpegli/djpegli)

# ── PNG optimizers ──
COPY --from=zopfli              /opt/zopfli-1.0.3                   /opt/zopfli-1.0.3
# optipng and pngcrush are installed from apt above

# ── GIF ──
COPY --from=gifsicle            /opt/gifsicle-1.94                  /opt/gifsicle-1.94

# ── TIFF ──
COPY --from=libtiff             /opt/libtiff-4.7.0                  /opt/libtiff-4.7.0

# ── HEIC ──
COPY --from=libheif             /opt/libheif-1.19.7                 /opt/libheif-1.19.7
COPY --from=libheif             /usr/local/lib/libx265*              /usr/local/lib/

# Library paths for dynamically linked binaries
ENV LD_LIBRARY_PATH="/opt/jpegli-0.11.1/lib:/opt/libjpeg-turbo-3.1.0/lib:/opt/mozjpeg-4.1.5/lib64:/opt/mozjpeg-4.1.5/lib:/opt/libwebp-1.5.0/lib:/opt/libavif-1.2.1/lib:/opt/libtiff-4.7.0/lib:/opt/libheif-1.19.7/lib:/usr/local/lib"

# ── IJG libjpeg aliases ──
ENV CJPEG_IJG6B="/opt/libjpeg-6b/bin/cjpeg" \
    DJPEG_IJG6B="/opt/libjpeg-6b/bin/djpeg" \
    CJPEG_IJG9B="/opt/libjpeg-9b/bin/cjpeg" \
    DJPEG_IJG9B="/opt/libjpeg-9b/bin/djpeg" \
    CJPEG_IJG9D="/opt/libjpeg-9d/bin/cjpeg" \
    DJPEG_IJG9D="/opt/libjpeg-9d/bin/djpeg" \
    CJPEG_IJG10="/opt/libjpeg-10/bin/cjpeg" \
    DJPEG_IJG10="/opt/libjpeg-10/bin/djpeg"

# ── libjpeg-turbo aliases (one per Ubuntu LTS version) ──
ENV CJPEG_TURBO_1_3="/opt/libjpeg-turbo-1.3.0/bin/cjpeg" \
    CJPEG_TURBO_1_4="/opt/libjpeg-turbo-1.4.2/bin/cjpeg" \
    CJPEG_TURBO_1_5="/opt/libjpeg-turbo-1.5.2/bin/cjpeg" \
    CJPEG_TURBO_2_0="/opt/libjpeg-turbo-2.0.3/bin/cjpeg" \
    CJPEG_TURBO_2_1_2="/opt/libjpeg-turbo-2.1.2/bin/cjpeg" \
    CJPEG_TURBO_2_1_5="/opt/libjpeg-turbo-2.1.5/bin/cjpeg" \
    CJPEG_TURBO="/opt/libjpeg-turbo-3.1.0/bin/cjpeg" \
    DJPEG_TURBO="/opt/libjpeg-turbo-3.1.0/bin/djpeg" \
    CJPEG_TURBO_12BIT="/opt/libjpeg-turbo-3.1.0-12bit/bin/cjpeg" \
    DJPEG_TURBO_12BIT="/opt/libjpeg-turbo-3.1.0-12bit/bin/djpeg"

# ── Other JPEG encoder aliases ──
ENV CJPEG_MOZ="/opt/mozjpeg-4.1.5/bin/cjpeg" \
    DJPEG_MOZ="/opt/mozjpeg-4.1.5/bin/djpeg" \
    CJPEGLI="/opt/jpegli-0.11.1/bin/cjpegli" \
    DJPEGLI="/opt/jpegli-0.11.1/bin/djpegli" \
    GUETZLI="/opt/guetzli-1.0.1/bin/guetzli"

# ── WebP aliases ──
ENV CWEBP="/opt/libwebp-1.5.0/bin/cwebp" \
    DWEBP="/opt/libwebp-1.5.0/bin/dwebp"

# ── AVIF aliases ──
ENV AVIFENC="/opt/libavif-1.2.1/bin/avifenc" \
    AVIFDEC="/opt/libavif-1.2.1/bin/avifdec"

# ── JPEG XL aliases ──
ENV CJXL="/opt/jpegli-0.11.1/bin/cjxl" \
    DJXL="/opt/jpegli-0.11.1/bin/djxl"

# ── PNG optimizer aliases ──
ENV OPTIPNG="/usr/bin/optipng" \
    PNGCRUSH="/usr/bin/pngcrush" \
    ZOPFLIPNG="/opt/zopfli-1.0.3/bin/zopflipng"

# ── GIF aliases ──
ENV GIFSICLE="/opt/gifsicle-1.94/bin/gifsicle"

# ── TIFF aliases ──
ENV TIFFCP="/opt/libtiff-4.7.0/bin/tiffcp"

# ── HEIC aliases ──
ENV HEIF_ENC="/opt/libheif-1.19.7/bin/heif-enc"

# Copy generation scripts and config
COPY scripts/ /app/scripts/
COPY manifest/ /app/manifest/
COPY sources/ /app/sources/

WORKDIR /app

ENV OUTPUT_DIR=/output \
    QUICK_MODE=0

ENTRYPOINT ["python3", "/app/scripts/generate.py"]
CMD ["--output", "/output"]
