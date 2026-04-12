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
# Stage: libjpeg-classic — IJG libjpeg v9e
#
# v9e is the latest IJG release (2022-01-16). Supports arithmetic coding,
# block sizes 1-16 (-block N), RGB identity encoding (-rgb1), and big gamut
# YCC (-bgycc) — features no other implementation has.
# ============================================================================
FROM base AS libjpeg-classic

ARG LIBJPEG_VERSION=9e
RUN curl -fsSL "https://www.ijg.org/files/jpegsrc.v${LIBJPEG_VERSION}.tar.gz" \
        | tar xz -C /tmp \
    && cd /tmp/jpeg-${LIBJPEG_VERSION} \
    && ./configure --prefix=/opt/libjpeg-${LIBJPEG_VERSION} \
    && make -j"$(nproc)" \
    && make install

# ============================================================================
# Stage: libjpeg-turbo — 8-bit with arithmetic coding enabled
#
# Distro packages disable WITH_ARITH_ENC/DEC by default. We build from
# source to enable arithmetic-coded output.
# ============================================================================
FROM base AS libjpeg-turbo

ARG TURBO_VERSION=3.1.0
ARG TURBO_COMMIT=3.1.0
RUN git clone --depth 1 --branch ${TURBO_COMMIT} \
        https://github.com/libjpeg-turbo/libjpeg-turbo.git /tmp/libjpeg-turbo \
    && cd /tmp/libjpeg-turbo \
    && cmake -B build -G Ninja \
        -DCMAKE_INSTALL_PREFIX=/opt/libjpeg-turbo-${TURBO_VERSION} \
        -DCMAKE_BUILD_TYPE=Release \
        -DWITH_ARITH_ENC=1 \
        -DWITH_ARITH_DEC=1 \
        -DWITH_TURBOJPEG=0 \
    && cmake --build build -j"$(nproc)" \
    && cmake --install build

# ============================================================================
# Stage: libjpeg-turbo-12bit — separate 12-bit precision build
#
# 12-bit mode is a compile-time switch that changes the entire library.
# Can't have 8-bit and 12-bit in the same binary.
# ============================================================================
FROM base AS libjpeg-turbo-12bit

ARG TURBO_VERSION=3.1.0
ARG TURBO_COMMIT=3.1.0
RUN git clone --depth 1 --branch ${TURBO_COMMIT} \
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
    && cmake --build build -j"$(nproc)" --target cjpegli djpegli \
    && mkdir -p /opt/jpegli-${LIBJXL_VERSION}/bin \
           /opt/jpegli-${LIBJXL_VERSION}/lib \
    && cp build/tools/cjpegli build/tools/djpegli \
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
        imagemagick \
    && rm -rf /var/lib/apt/lists/*

# blake3 for content hashing
RUN pip3 install --no-cache-dir --break-system-packages blake3==1.0.4

# Copy encoder/decoder installations from build stages
COPY --from=libjpeg-classic  /opt/libjpeg-9e                    /opt/libjpeg-9e
COPY --from=libjpeg-turbo    /opt/libjpeg-turbo-3.1.0           /opt/libjpeg-turbo-3.1.0
COPY --from=libjpeg-turbo-12bit /opt/libjpeg-turbo-3.1.0-12bit  /opt/libjpeg-turbo-3.1.0-12bit
COPY --from=mozjpeg          /opt/mozjpeg-4.1.5                  /opt/mozjpeg-4.1.5
COPY --from=jpegli           /opt/jpegli-0.11.1                  /opt/jpegli-0.11.1
COPY --from=guetzli          /opt/guetzli-1.0.1                  /opt/guetzli-1.0.1

# Library paths for dynamically linked binaries
ENV LD_LIBRARY_PATH="/opt/jpegli-0.11.1/lib:/opt/libjpeg-turbo-3.1.0/lib:/opt/mozjpeg-4.1.5/lib64:/opt/mozjpeg-4.1.5/lib:${LD_LIBRARY_PATH}"

# Encoder binary aliases — fully qualified paths avoid $PATH conflicts.
# Scripts use these env vars, never bare "cjpeg".
ENV CJPEG_IJG="/opt/libjpeg-9e/bin/cjpeg" \
    DJPEG_IJG="/opt/libjpeg-9e/bin/djpeg" \
    CJPEG_TURBO="/opt/libjpeg-turbo-3.1.0/bin/cjpeg" \
    DJPEG_TURBO="/opt/libjpeg-turbo-3.1.0/bin/djpeg" \
    CJPEG_TURBO_12BIT="/opt/libjpeg-turbo-3.1.0-12bit/bin/cjpeg" \
    DJPEG_TURBO_12BIT="/opt/libjpeg-turbo-3.1.0-12bit/bin/djpeg" \
    CJPEG_MOZ="/opt/mozjpeg-4.1.5/bin/cjpeg" \
    DJPEG_MOZ="/opt/mozjpeg-4.1.5/bin/djpeg" \
    CJPEGLI="/opt/jpegli-0.11.1/bin/cjpegli" \
    DJPEGLI="/opt/jpegli-0.11.1/bin/djpegli" \
    GUETZLI="/opt/guetzli-1.0.1/bin/guetzli"

# Copy generation scripts and config
COPY scripts/ /app/scripts/
COPY manifest/ /app/manifest/
COPY sources/ /app/sources/

WORKDIR /app

ENV OUTPUT_DIR=/output \
    QUICK_MODE=0

ENTRYPOINT ["python3", "/app/scripts/generate.py"]
CMD ["--output", "/output"]
