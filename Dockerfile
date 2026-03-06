# Dockerfile — reproducible build and test image for rf_adapt_intel
#
# Build (from repo root):
#   docker build -t rf-adapt-intel:latest .
#
# Run tests:
#   docker run --rm rf-adapt-intel:latest ctest --test-dir /build -V
#
# NOTE: This image targets the iq_metrics standalone tool and Python test
# harness only.  Building rf_adapt_intel itself requires SoapySDR, which
# is installed via a separate stage below.

FROM ubuntu:22.04 AS base
LABEL maintainer="foxrouter"
LABEL description="rf_adapt_intel build and test environment"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Core build tools + Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    ninja-build \
    pkg-config \
    clang \
    clang-format-14 \
    clang-tidy-14 \
    python3 \
    python3-pip \
    python3-numpy \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

# Symlink clang-format/tidy versioned binaries to unversioned names
RUN ln -sf /usr/bin/clang-format-14 /usr/local/bin/clang-format && \
    ln -sf /usr/bin/clang-tidy-14   /usr/local/bin/clang-tidy

# ── Source copy ─────────────────────────────────────────────────────────────
WORKDIR /src
COPY . .

# ── Python deps ─────────────────────────────────────────────────────────────
RUN pip3 install --no-cache-dir -r requirements.txt

# ── Build iq_metrics (no SoapySDR needed) ───────────────────────────────────
RUN cmake -S /src -B /build \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_HARDWARE_TARGETS=OFF \
      -G Ninja && \
    cmake --build /build -t iq_metrics

# ── Run Python tests + iq_metrics validation ────────────────────────────────
# Tests run at container build time to ensure the image is always green.
RUN cd /src && python3 tests/test_autotune.py -v
RUN cd /src && python3 tests/test_decode_candidates.py -v
RUN cd /src && python3 tests/test_guardrails.py -v
RUN cd /src && python3 tests/test_demod_ber.py -v
RUN cd /src && python3 tests/test_snr_sweep.py -v
RUN cd /src && python3 tests/test_iq_metrics.py /build/iq_metrics -v

# Default command: run all CTest tests
CMD ["ctest", "--test-dir", "/build", "-V", "--output-on-failure"]

# ── (Optional) Hardware build stage — requires SoapySDR ─────────────────────
# Uncomment and run `docker build --target hardware .` to build rf_adapt_intel.
# FROM base AS hardware
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     libsoapysdr-dev && rm -rf /var/lib/apt/lists/*
# RUN cmake -S /src -B /build-hw \
#       -DCMAKE_BUILD_TYPE=Release \
#       -DBUILD_HARDWARE_TARGETS=ON \
#       -G Ninja && \
#     cmake --build /build-hw
