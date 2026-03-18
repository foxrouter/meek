# RF Process Worker (`rf_adapt_intel`)

[![CI](https://github.com/foxrouter/meek/actions/workflows/ci.yml/badge.svg)](https://github.com/foxrouter/meek/actions/workflows/ci.yml)

A C++20 RF signal-processing worker that captures IQ samples via SoapySDR,
classifies modulation (GMSK/FSK/PSK/QAM/OOK), and persists results to SQLite.
Deployed as a hardened systemd service on embedded Linux (Raspberry Pi and Ubuntu server).

## Installation

> **New here?** See the step-by-step [Installation Guide](docs/INSTALL.md) for
> detailed instructions for **Raspberry Pi OS Bookworm 64-bit** and
> **Ubuntu Server Noble 24.04**.

### Quick install (automated)

```bash
git clone https://github.com/foxrouter/meek.git
cd meek
sudo bash install.sh          # detects OS, installs deps, builds, deploys service
```

Add `--all-decoders` to also install multimon-ng, rtl_433, and liquid-dsp:

```bash
sudo bash install.sh --all-decoders
```

Use `--dry-run` to preview every step without making changes:

```bash
bash install.sh --dry-run
```

See `bash install.sh --help` or [docs/INSTALL.md](docs/INSTALL.md) for full options
and troubleshooting.

---

## Table of contents

- [Architecture](#architecture)
- [Layout](#layout)
- [Prerequisites](#prerequisites)
- [Build](#build)
- [Quickstart](#quickstart)
- [Optional decoder setup](#optional-decoder-setup-opssetupsh)
- [Offline IQ metrics](#offline-iq-metrics-toolsiq_metricscpp)
- [Offline IQ analysis](#offline-iq-analysis-toolsdecode_candidatespy)
- [Canary procedure](#canary-procedure-opscanarysh)
- [IQ file transfer](#iq-file-transfer-scriptstransfer_iqsh)
- [Test harnesses](#test-harnesses)
- [Container build](#container-build)
- [Sensitive-data guidance](#sensitive-data-guidance)

## Layout

```
benchmarks/               Python-vs-C++ benchmark scripts and results
config/                   runtime configuration example
docs/                     design plan and gap tracking
ops/                      deploy, verify, setup, and canary helpers
scripts/                  IQ ingest, metrics, heartbeat, and transfer helpers
src/                      C++ worker source
systemd/                  systemd unit and drop-in files
tests/                    Python and shell test harnesses
tools/                    offline decode/audit utilities
```

Key files:

| Path | Purpose |
|---|---|
| `src/main.cpp` | Core capture + classification + DB persistence |
| `tools/iq_metrics.cpp` | Standalone C++ IQ metrics tool (avg_power, snr_db, spectral_flatness, est_bw_hz) |
| `docs/INSTALL.md` | **Step-by-step installation guide** (Bookworm & Noble) |
| `docs/rf-adapt-intel-plan.md` | Full design plan and execution status |
| `docs/missing-features.md` | Gaps and pending implementation items |
| `docs/audit.md` | Production-readiness audit, dependency map, and migration notes |
| `install.sh` | **Automated one-shot installer** (detects OS, builds, deploys) |
| `config/thresholds.env.example` | Runtime knobs (copy to `/etc/rf_worker/thresholds.env`) |
| `systemd/process-worker.service` | Hardened systemd unit |
| `systemd/process-worker.service.d/` | Drop-in overrides (hardening, env, processor) |
| `ops/deploy.sh` | Atomic service install |
| `ops/setup.sh` | Optional decoder installer |
| `ops/verify.sh` | Hardening verification |
| `ops/canary.sh` | Canary lifecycle (enable/status/promote/rollback) |
| `scripts/process_incoming.sh` | Band-aware offline IQ file processor |
| `scripts/heartbeat_and_metrics.sh` | Standalone heartbeat + Prometheus metrics writer |
| `scripts/transfer_iq.sh` | rsync IQ snapshot files from edge (Ray) to server (Brian) |
| `scripts/deploy_and_restart.sh` | Build, install binary, and restart service |
| `tools/decode_candidates.py` | Offline modulation decode + JSON audit report |
| `tests/gen_test_signals.py` | Synthetic RRC-shaped IQ vector generator |
| `tests/test_iq_metrics.py` | Validates C++ `iq_metrics` output against the Python reference |
| `benchmarks/bench_iq_metrics.py` | Python-vs-C++ throughput benchmark for IQ metrics |

## Architecture

The worker runs a three-stage lock-free pipeline:

```mermaid
flowchart LR
    subgraph Capture
        SDR["SDR hardware\n(SoapySDR)"]
        cap["cap_thread"]
        SDR -->|IQ samples| cap
    end

    subgraph Processing
        rb1(["SpscRingBuffer\n<SampleBlock, 64>"])
        proc["proc_thread\nclassify_block()"]
        rb2(["SpscRingBuffer\n<ClassificationResult, 64>"])
        cap --> rb1 --> proc --> rb2
    end

    subgraph Output
        out["out_thread\noutput_loop()"]
        db[("SQLite\nrf_adapt_intel.db")]
        prom["Prometheus\ntextfile metrics"]
        rb2 --> out
        out -->|"db.insert_signal\ndb.insert_example"| db
        out -->|"every 5 s"| prom
    end

    subgraph Snapshot
        sq[/"snap_queue\n(deque, max 64)"/]
        snap["snap_thread\n(std::jthread)"]
        sq -->|".cf32 IQ files"| snap
    end

    proc -->|"enqueue SnapTask"| sq
```

Key design decisions:
- **Lock-free ring buffers** (`SpscRingBuffer<T, 64>`) decouple capture, processing, and output at runtime.
- **SQLite writes** happen exclusively on the output thread, preventing contention with the capture thread.
- **Snapshot worker** (`snap_thread`, `std::jthread`) handles IQ snapshot I/O from a bounded task queue (capped at 64 entries) without blocking the processing thread.
- **Cooperative shutdown** via the `g_shutdown` atomic flag and `std::stop_token` lets all threads drain cleanly on `SIGINT`/`SIGTERM`.
- **38 band profiles** (`kUkBands`) provide per-band SNR, bandwidth, and prior-boost parameters for the classifier.

## Prerequisites

> For a guided setup walkthrough see [docs/INSTALL.md](docs/INSTALL.md).

| Component | Minimum version | Notes |
|---|---|---|
| GCC or Clang | C++20 capable | see [Compiler requirements](#compiler-requirements) |
| CMake | 3.25 | Build system |
| SoapySDR | any | `libsoapysdr-dev` on Debian/Ubuntu |
| SQLite 3 | any | `libsqlite3-dev` |
| Python 3 | 3.8+ | For tests and tools (`numpy` required) |
| liquid-dsp | optional | Advanced demodulation — see [Optional decoder setup](#optional-decoder-setup-opssetupsh) |

### Compiler requirements
- GCC ≥ 12 or Clang ≥ 14 (C++20 stdlib required)
- On Raspberry Pi OS Bookworm the default GCC (12) satisfies this requirement

On Raspberry Pi OS Bookworm:

```bash
sudo apt install -y build-essential cmake pkg-config \
    libsoapysdr-dev soapysdr-tools soapysdr-module-rtlsdr \
    libsqlite3-dev python3 python3-numpy \
    rtl-sdr librtlsdr-dev inotify-tools rsync
```

On Ubuntu Noble (24.04) or later (default GCC is already ≥ 12):

```bash
sudo apt install -y build-essential cmake pkg-config \
    libsoapysdr-dev soapysdr-tools soapysdr-module-rtlsdr \
    libsqlite3-dev python3 python3-numpy \
    rtl-sdr librtlsdr-dev inotify-tools rsync
```

## Build

```bash
mkdir build && cd build
cmake -S .. -B . -DCMAKE_BUILD_TYPE=Release
cmake --build . -- -j$(nproc)
```

If liquid-dsp is installed, CMake will detect it automatically and enable `HAVE_LIQUID`.

Install the binary to `/usr/local/bin/`:

```bash
sudo cmake --install .
```

### CMake build options

| Option | Default | Description |
|---|---|---|
| `BUILD_HARDWARE_TARGETS` | `ON` | Build `rf_adapt_intel` and `soapy_read_test` (requires SoapySDR). Set `OFF` in CI or on machines without SDR hardware. |
| `ENABLE_SANITIZERS` | `OFF` | Compile with AddressSanitizer and UndefinedBehaviorSanitizer (Clang recommended). |

Build only `iq_metrics` (no SDR hardware required):

```bash
cmake -S . -B build -DBUILD_HARDWARE_TARGETS=OFF
cmake --build build -t iq_metrics
```

## Quickstart

1. Copy `config/thresholds.env.example` to `/etc/rf_worker/thresholds.env` (or keep in `config/` — **git-ignored**).
2. *(Optional)* Install optional decoders: `sudo bash ops/setup.sh`
3. Install systemd unit/drop-ins: `sudo bash ops/deploy.sh`
4. Run `ops/verify.sh` to confirm hardening.
5. Ingest a sample IQ: `bash scripts/process_incoming.sh /path/to/file.raw`
6. Metrics/heartbeat: `bash scripts/heartbeat_and_metrics.sh` (optional background service)

## Optional decoder setup (`ops/setup.sh`)

`ops/setup.sh` guides you through installing optional decoders that extend the
classification pipeline.  It can be run interactively or fully non-interactively
via CLI flags.

### Platform requirements
- Linux (Raspberry Pi OS Bookworm 64-bit recommended)
- Minimum 4 GB RAM (required for the liquid-dsp build step)

### Available decoders

| Decoder | Flag | Method | Adds |
|---|---|---|---|
| **multimon-ng** | `--install-multimon-ng` | `apt install multimon-ng` | POCSAG / FLEX / OOK protocol decoding |
| **rtl_433** | `--install-rtl_433` | `apt install rtl-433` | OOK/ASK ISM-433 device packets |
| **liquid-dsp** | `--install-liquid-dsp` | build from source | Advanced GMSK / PSK demodulation |

### Usage examples

```bash
# Interactive mode (TTY required — prompts for each decoder)
sudo bash ops/setup.sh

# CLI flags — fully non-interactive, install specific decoders
sudo bash ops/setup.sh --non-interactive --install-multimon-ng --install-rtl_433

# Install all optional decoders non-interactively
sudo bash ops/setup.sh --non-interactive \
  --install-multimon-ng --install-rtl_433 --install-liquid-dsp

# Preview what would happen without making changes
sudo bash ops/setup.sh --non-interactive --install-liquid-dsp --dry-run

# Deploy service and run setup in one step
sudo bash ops/deploy.sh --setup
```

After setup, decoder binary paths are written to `/etc/rf_worker/thresholds.env`
so the `process-worker` systemd service picks them up automatically.

## Offline IQ metrics (`tools/iq_metrics.cpp`)

`iq_metrics` is a standalone C++20 tool that reads raw CF32 IQ snapshot files
and computes four signal metrics: `avg_power`, `snr_db`, `spectral_flatness`,
and `est_bw_hz`.  It mirrors the Python reference in
`tools/autotune_thresholds.py` and is validated against it by
`tests/test_iq_metrics.py`.

### Build

```bash
cmake -S . -B build -DBUILD_HARDWARE_TARGETS=OFF
cmake --build build -t iq_metrics
```

### Usage

```bash
# Analyse a single IQ snapshot (default sample rate: 2 048 000 Hz)
./build/iq_metrics /var/lib/rf-adapt-intel/snapshots/snap_*.cf32

# Specify sample rate and block size
./build/iq_metrics --sample-rate 2048000 --block-size 4096 file.cf32

# Batch: analyse multiple files
./build/iq_metrics snap1.cf32 snap2.cf32

# Show usage
./build/iq_metrics --help
```

Output is JSON on stdout, for example:

```json
[
  {
    "file": "snap_1234_c85.cf32",
    "n_samples": 65536,
    "avg_power": -12.3,
    "snr_db": 18.7,
    "spectral_flatness": 0.21,
    "est_bw_hz": 145000.0
  }
]
```

### Benchmark

```bash
python3 benchmarks/bench_iq_metrics.py build/iq_metrics \
    --repetitions 20 \
    --block-sizes 1024,4096,16384,65536
```

The benchmark compares per-block latency of the C++ tool against the Python
reference and writes JSON results to `benchmarks/results/`.

---

## Offline IQ analysis (`tools/decode_candidates.py`)

`decode_candidates.py` retrieves candidate signal records from the SQLite
database, locates matching IQ snapshot files, and attempts modulation decoding
using built-in Python decoders (and optional external tools).  It produces a
verifiable JSON audit report.

### Basic usage

```bash
# Analyse candidates in the default database with default snapshot directory
python3 tools/decode_candidates.py

# Specify a custom database, snapshot dir, and output file
python3 tools/decode_candidates.py \
    --db rf_adapt_intel.db \
    --snapshot-dir /var/lib/rf-adapt-intel/snapshots \
    --out /tmp/audit_report.json \
    --sample-rate 2048000 \
    --min-confidence 0.6

# Also invoke external decoders (multimon-ng, rtl_433) where installed
python3 tools/decode_candidates.py --external

# Process only the 20 most recent candidates
python3 tools/decode_candidates.py --limit 20
```

### Output format

The tool writes a JSON file with an array of `candidate` objects.  Each entry
includes: `signal_id`, `timestamp`, `mod_class`, `confidence`, `snapshot_file`,
`decoder_used`, `result`, and `notes`.

### Offline file-replay via `process_incoming.sh`

`scripts/process_incoming.sh` runs offline analysis automatically when
`tools/decode_candidates.py` and `python3` are available:

```bash
# Analyse an existing IQ snapshot file offline
OUTPUT_DIR=/tmp/processed bash scripts/process_incoming.sh /path/to/433_signal.raw

# Point at an existing database for candidate lookup
REPLAY_DB=/var/lib/rf-adapt-intel/rf_adapt_intel.db \
  bash scripts/process_incoming.sh /path/to/433_signal.raw
```

## Canary procedure (`ops/canary.sh`)

`ops/canary.sh` manages the canary deployment lifecycle — enabling passive
capture mode, monitoring FP/FN rates, promoting to production, and rolling back.

```bash
# Enable canary mode (sets RF_SNR_MIN_DB=0, restarts service)
sudo bash ops/canary.sh

# Check current FP/FN counters, CPU/memory usage, and lock-fail metrics
sudo bash ops/canary.sh --status

# Promote canary config to production (removes the canary drop-in)
sudo bash ops/canary.sh --promote

# Roll back to the last backed-up production config
sudo bash ops/canary.sh --rollback
```

Promotion criteria (all must be met before `--promote`):
- Classifier >= 95 % accuracy at >= 0 dB SNR.
- False-positive / rejection rate < 3 % over the monitoring window.
- CPU usage < 80 % on target host.
- No lock-fail counter increases in the last 30 minutes.

## IQ file transfer (`scripts/transfer_iq.sh`)

Transfer IQ snapshot files from Ray (edge SDR) to Brian (central server) via
rsync with retries, bandwidth limiting, and logging.  After each transfer batch
the SQLite classifications DB is synced to Brian so that the reporting node
receives recent classification data.  When `sqlite3` is available a consistent
snapshot is created via `sqlite3 .backup` (online backup API) before rsyncing;
this holds only brief shared locks and avoids a full DB rewrite.  Otherwise
the script falls back to rsyncing the live DB and WAL/SHM sidecars (which may
be racy under write load).  DB sync failures are logged but do not abort IQ
transfers.

Key environment variables and CLI flags for DB sync:
- `DB_SOURCE` / `--db-source` — local path to the SQLite DB (default: `/var/lib/rf-adapt-intel/rf_adapt_intel.db`)
- `DB_DEST` / `--db-dest` — rsync destination for the DB snapshot; DB sync is skipped when unset
- `DB_SYNC_INTERVAL` *(env only)* — minimum seconds between DB syncs in watch mode (default: 60)

```bash
# One-shot transfer of all files in the snapshot directory
IQ_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/incoming/ \
  bash scripts/transfer_iq.sh

# Also sync the classifications DB to Brian after the sweep
IQ_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/incoming/ \
DB_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/rf_adapt_intel.db \
  bash scripts/transfer_iq.sh

# Continuous watcher: transfer new files as they arrive (requires inotify-tools)
# DB is synced to Brian periodically (at most once every DB_SYNC_INTERVAL seconds)
IQ_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/incoming/ \
DB_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/rf_adapt_intel.db \
  bash scripts/transfer_iq.sh --watch

# Limit bandwidth to 512 kbps and use 5 retries
bash scripts/transfer_iq.sh --dest user@host:path --bwlimit 512 --retries 5

# Dry-run (prints rsync commands without executing)
bash scripts/transfer_iq.sh --dest user@host:path --dry-run
```

## Test harnesses

All Python tests use the standard `unittest` module and require only `numpy`.

```bash
# SNR sweep: accuracy and FP rate across all modulations and SNR levels
python3 tests/test_snr_sweep.py -v

# Guardrail rejection tests (SNR gate, BW gate, PAPR_MAX)
python3 tests/test_guardrails.py -v

# BER and CRC-32 validation for each demod chain
python3 tests/test_demod_ber.py -v

# Throughput benchmark (frames per minute)
python3 tests/bench_throughput.py -v

# Validate C++ iq_metrics output against the Python reference
cmake -S . -B build -DBUILD_HARDWARE_TARGETS=OFF && cmake --build build -t iq_metrics
python3 tests/test_iq_metrics.py build/iq_metrics -v

# Shell tests for ops/setup.sh
bash tests/test_setup.sh -v

# Run all tests via CTest (after cmake build)
cmake --build build --target test
```

Generate synthetic IQ test vectors:

```bash
# Generate RRC-shaped vectors for all supported bands and modulations
python3 tests/gen_test_signals.py
```

## Container build

A `Dockerfile` is provided for reproducible builds and testing without
installing any host dependencies.  It builds `iq_metrics` and runs all Python
tests at image build time.

```bash
# Build the image (runs all tests as part of the build)
docker build -t rf-adapt-intel:latest .

# Run the full CTest suite inside the container
docker run --rm rf-adapt-intel:latest ctest --test-dir /build -V
```

### Running `iq_metrics` inside the container

```bash
# Analyse IQ snapshot files from the host (mount the directory read-only)
docker run --rm \
  -v /var/lib/rf-adapt-intel/snapshots:/snapshots:ro \
  rf-adapt-intel:latest \
  /build/iq_metrics /snapshots/snap_*.cf32

# Override sample rate
docker run --rm \
  -v /var/lib/rf-adapt-intel/snapshots:/snapshots:ro \
  rf-adapt-intel:latest \
  /build/iq_metrics --sample-rate 2048000 /snapshots/snap_*.cf32
```

### Running `decode_candidates` inside the container

```bash
# Mount the database and snapshots directory; write the report to a host path
docker run --rm \
  -v /var/lib/rf-adapt-intel:/data:rw \
  rf-adapt-intel:latest \
  python3 /src/tools/decode_candidates.py \
    --db /data/rf_adapt_intel.db \
    --snapshot-dir /data/snapshots \
    --out /data/audit_report.json
```

### Runtime configuration via environment variables

Environment variables can be passed to any `docker run` command with `-e`.
For example, to run `iq_metrics` with a custom confidence threshold:

```bash
docker run --rm \
  -e RF_CONF_THRESHOLD=0.7 \
  -e RF_SNR_MIN_DB=6.0 \
  -e RF_SNAPSHOT_RETENTION_DAYS=7 \
  -v /var/lib/rf-adapt-intel/snapshots:/snapshots:ro \
  rf-adapt-intel:latest \
  /build/iq_metrics /snapshots/snap_*.cf32
```

> **Note:** The Docker image targets the `iq_metrics` tool and Python test
> harness only.  Building `rf_adapt_intel` with SoapySDR requires the
> commented-out `hardware` stage in the `Dockerfile`.

---

## Sensitive-data guidance

- Keep secrets out of git. Use `config/thresholds.env` (ignored) for local overrides.
- Run `gitleaks detect --source .` before pushing sensitive changes.

## Snapshot file retention

IQ snapshot files (`.cf32`) accumulate in `RF_SNAPSHOT_DIR` and can fill disk on
long-running deployments. Set `RF_SNAPSHOT_RETENTION_DAYS` to automatically prune
files older than *N* days (checked once per day by the worker):

```bash
# /etc/rf_worker/thresholds.env
RF_SNAPSHOT_RETENTION_DAYS=7   # keep 7 days of snapshots; 0 = keep forever (default)
```

## Development

- C++ source is formatted with **clang-format v14** (`clang-format -i src/*.cpp tools/iq_metrics.cpp`).
- **clang-tidy** (v14) is run in CI on `tools/iq_metrics.cpp` with `clang-analyzer-*`, `bugprone-*`, `modernize-*`, `performance-*`, and `readability-*` checks.
- **cpplint** runs as a pre-commit hook; install hooks with `pre-commit install`.
- See `.cpplint-rationale.md` for the reasoning behind enabled/disabled checks.
- The CI pipeline (`.github/workflows/ci.yml`) runs five jobs: Python lint + tests, C++ build + format + static analysis + test + benchmark, ASAN/UBSAN sanitizer build, Python dependency vulnerability scan (pip-audit), and Docker image build.
- See `docs/missing-features.md` for a list of pending implementation items.
- See `docs/audit.md` for the production-readiness audit and migration decision log.