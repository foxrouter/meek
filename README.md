# RF Process Worker (`rf_adapt_intel`)

A C++17 RF signal-processing worker that captures IQ samples via SoapySDR,
classifies modulation (GMSK/FSK/PSK/QAM/OOK), and persists results to SQLite.
Deployed as a hardened systemd service on embedded Linux (Raspberry Pi and Ubuntu server).

## Table of contents

- [Layout](#layout)
- [Prerequisites](#prerequisites)
- [Build](#build)
- [Quickstart](#quickstart)
- [Optional decoder setup](#optional-decoder-setup-opssetupsh)
- [Offline IQ analysis](#offline-iq-analysis-toolsdecode_candidatespy)
- [Canary procedure](#canary-procedure-opscanarysh)
- [IQ file transfer](#iq-file-transfer-scriptstransfer_iqsh)
- [Test harnesses](#test-harnesses)
- [Sensitive-data guidance](#sensitive-data-guidance)

## Layout

```
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
| `docs/rf-adapt-intel-plan.md` | Full design plan and execution status |
| `docs/missing-features.md` | Gaps and pending implementation items |
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

## Prerequisites

| Component | Minimum version | Notes |
|---|---|---|
| GCC or Clang | C++17 capable | GCC 8+, Clang 7+ |
| CMake | 3.10 | Build system |
| SoapySDR | any | `libsoapysdr-dev` on Debian/Ubuntu |
| SQLite 3 | any | `libsqlite3-dev` |
| Python 3 | 3.8+ | For tests and tools (`numpy` required) |
| liquid-dsp | optional | Advanced demodulation — see [Optional decoder setup](#optional-decoder-setup-opssetupsh) |

On Debian/Ubuntu:

```bash
sudo apt install -y build-essential cmake pkg-config \
    libsoapysdr-dev libsqlite3-dev python3 python3-numpy
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
rsync with retries, bandwidth limiting, and logging.

```bash
# One-shot transfer of all files in the snapshot directory
IQ_DEST=brian@192.168.1.10:/var/lib/rf-adapt-intel/incoming/ \
  bash scripts/transfer_iq.sh

# Continuous watcher: transfer new files as they arrive (requires inotify-tools)
IQ_DEST=brian@192.168.1.10:/var/lib/rf-adapt-intel/incoming/ \
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

## Sensitive-data guidance

- Keep secrets out of git. Use `config/thresholds.env` (ignored) for local overrides.
- Run `gitleaks detect --source .` before pushing sensitive changes.

## Development

- C++ source is formatted with **clang-format v14** (`clang-format -i src/*.cpp`).
- **cpplint** runs as a pre-commit hook; install hooks with `pre-commit install`.
- See `.cpplint-rationale.md` for the reasoning behind enabled/disabled checks.
- See `docs/missing-features.md` for a list of pending implementation items.