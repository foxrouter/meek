# Dockerfile — reproducible build and test image for rf_adapt_intel
#
# Build (from repo root):
#   docker build -t rf-adapt-intel:latest .
#
# Run tests:
#   docker run --rm rf-adapt-intel:latest ctest --test-dir /build -V

FROM ubuntu:24.04 AS base
LABEL maintainer="foxrouter"
LABEL description="rf_adapt_intel build and test environment"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC

# Core build tools + Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ninja-build \
    cmake \
    pkg-config \
    clang-18 \
    clang-format-18 \
    clang-tidy-18 \
    python3 \
    python3-venv \
    libsqlite3-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated virtual environment so pip installs don't touch the system Python
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Symlink clang-format/tidy versioned binaries to unversioned names
RUN ln -sf /usr/bin/clang-format-18 /usr/local/bin/clang-format && \
    ln -sf /usr/bin/clang-tidy-18   /usr/local/bin/clang-tidy

# ── Source copy ─────────────────────────────────────────────────────────────
WORKDIR /src
COPY . .

# ── Python deps ─────────────────────────────────────────────────────────────
RUN pip install --no-cache-dir -r requirements.txt

# ── Build iq_metrics and rf_audit (no SoapySDR needed) ──────────────────────
RUN cmake -S /src -B /build \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_HARDWARE_TARGETS=OFF \
      -G Ninja && \
    cmake --build /build -t iq_metrics rf_audit

# ── Run Python tests + iq_metrics validation ────────────────────────────────
RUN cd /src && bash tests/test_scan_incoming.sh -v
RUN cd /src && python3 tests/test_autotune.py -v
RUN cd /src && python3 tests/test_decode_candidates.py -v
RUN cd /src && python3 tests/test_guardrails.py -v
RUN cd /src && python3 tests/test_demod_ber.py -v
RUN cd /src && python3 tests/test_snr_sweep.py -v
RUN cd /src && python3 tests/test_iq_metrics.py /build/iq_metrics -v

# Default command: run all CTest tests
CMD ["ctest", "--test-dir", "/build", "-V", "--output-on-failure"]

# ── (Optional) Hardware build stage — requires SoapySDR ─────────────────────
# FROM base AS hardware
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     libsoapysdr-dev && rm -rf /var/lib/apt/lists/*
# RUN cmake -S /src -B /build-hw \
#       -DCMAKE_BUILD_TYPE=Release \
#       -DBUILD_HARDWARE_TARGETS=ON \
#       -G Ninja && \
#     cmake --build /build-hw -t rf_adapt_intel
