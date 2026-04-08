# Codebase Guide — `rf_adapt_intel` (`meek`)

A directory-by-directory reference to every file in this repository.
Read alongside [docs/rf-adapt-intel-plan.md](rf-adapt-intel-plan.md) for
design rationale, and [docs/INSTALL.md](INSTALL.md) for deployment steps.

---

## Table of contents

1. [Top-level files](#1-top-level-files)
2. [src/ — C++ daemon sources](#2-src--c-daemon-sources)
3. [include/meek/ — C++ headers](#3-includemeek--c-headers)
4. [tools/ — CLI utilities](#4-tools--cli-utilities)
5. [scripts/ — Bash helpers](#5-scripts--bash-helpers)
6. [ops/ — Deployment and operations](#6-ops--deployment-and-operations)
7. [systemd/ — Service units and drop-ins](#7-systemd--service-units-and-drop-ins)
8. [config/ — Runtime configuration](#8-config--runtime-configuration)
9. [tests/ — Test harnesses](#9-tests--test-harnesses)
10. [benchmarks/ — Throughput benchmarks](#10-benchmarks--throughput-benchmarks)
11. [docs/ — Documentation](#11-docs--documentation)

---

## 1. Top-level files

| File | Purpose |
|---|---|
| `CMakeLists.txt` | CMake build (≥ 3.25). Defines four executables when hardware targets are enabled (`rf_adapt_intel`, `soapy_read_test`, `rf_audit`, `iq_metrics`), FetchContent downloads for nlohmann/json and cpp-httplib, optional liquid-dsp detection, ASAN/UBSAN opt-in, `.deb` packaging via CPack, and CTest integration. |
| `install.sh` | One-shot automated installer. Detects Raspberry Pi OS Bookworm or Ubuntu Noble, installs system packages, builds all targets, deploys the systemd service, and creates `/etc/rf_worker/`. Supports `--all-decoders`, `--dry-run`, `--help`. |
| `Dockerfile` | Reproducible build image for `iq_metrics` and all Python tests. Hardware targets (SoapySDR) are in a commented-out `hardware` stage. |
| `requirements.txt` | Python runtime dependencies (`numpy`, `scipy`). |
| `.pre-commit-config.yaml` | Pre-commit hooks: clang-format, cpplint, and gitleaks. |
| `.clang-format` | clang-format v14 style configuration applied to all C++ sources. |
| `.cpplint-rationale.md` | Explains every suppressed cpplint filter with a justification. |
| `.git-blame-ignore-revs` | Bulk-format commits excluded from `git blame`. |

---

## 2. src/ — C++ daemon sources

### `src/main.cpp` (~1 350 lines)

The entire production daemon in a single translation unit.  It wires
together all headers from `include/meek/` into the four-thread pipeline:

```
capture_loop  →  SpscRingBuffer<SampleBlock, 64>
              →  proc_loop
              →  SpscRingBuffer<ClassificationResult, 64>
              →  output_loop
```

A fifth thread (`snap_thread`, `std::jthread`) drains a `std::deque<SnapTask>`
and writes CF32 IQ snapshot files to `RF_SNAPSHOT_DIR` without blocking
the processing thread.

Key static functions:

| Function | Lines | Responsibility |
|---|---|---|
| `capture_loop` | ~414 | Calls `ISdrSource::read_samples`, packs `SampleBlock`, pushes to ring buffer. Integrates `BandScheduler` for multi-band rotation. |
| `proc_loop` | ~524 | Pops `SampleBlock`, calls `classify_block()`, optionally calls `demod_fsk` / `demod_psk_qam` / `demod_ook_am` (`HAVE_LIQUID`), pushes `ClassificationResult`. |
| `output_loop` | ~784 | Pops `ClassificationResult`, writes to SQLite via `Database`, updates `ProcMetrics`, writes Prometheus textfile every 5 s, writes heartbeat every 30 s, prunes old snapshots once per 24 h (`RF_SNAPSHOT_RETENTION_DAYS`). |
| `main` | ~1 013 | Calls `parse_config(argc, argv)`, opens the SDR, initialises the DB, launches all threads, waits for `g_shutdown`, drains and joins cleanly. |

**Compile-time feature guards:**

| Guard | Enabled when | Effect |
|---|---|---|
| `HAVE_SOAPY` | SoapySDR found (always set for `rf_adapt_intel`) | Activates `SoapySdrSource` in `isdr_source.hpp` |
| `HAVE_LIQUID` | liquid-dsp found via CMake/pkg-config | Activates demod chains and per-demod `ClassificationResult` fields |
| `HAVE_SYSTEMD` | libsystemd found | Uses `<systemd/sd-daemon.h>` for `sd_notify`; otherwise a socket-based fallback is compiled in |
| `HAVE_HTTPLIB` | cpp-httplib available | Starts an HTTP `/metrics` endpoint on `RF_PROMETHEUS_PORT` |

---

### `src/rf_audit.cpp` (~222 lines)

Standalone CLI tool.  No SoapySDR or SQLite dependency; builds with
`-DBUILD_HARDWARE_TARGETS=OFF`.  Reads raw CF32 IQ files, calls the same
`classify_block()` as the daemon, matches the centre frequency against
`kUkBands`, and writes one JSON object per file to stdout.

Output fields: `file`, `n_samples`, `sample_rate_hz`, `center_freq_hz`,
`mod`, `confidence`, `snr_db`, `avg_power`, `papr_db`, `spectral_flatness`,
`occupied_bw_hz`, `time_occupancy`, `avg_abs_phase`, `trans_ratio`, `p50`,
`p90`, `snr_gate_pass`, `bw_gate_pass`, `is_candidate`, `decision_trace`,
plus `band` and `band_notes` when the centre frequency matches a profile.

Exit codes: `0` = all files processed, `1` = one or more files failed,
`2` = usage error.

---

## 3. include/meek/ — C++ headers

All headers are `#pragma once`, zero-cost on inclusion in translation units
that do not instantiate their templates.

### `sample_types.hpp`

Defines the core pipeline data types shared by all components:

| Type | Description |
|---|---|
| `ModClass` | Enum: `UNKNOWN`, `CW_LIKE`, `FSK_LIKE`, `PSK_QAM_LIKE`, `OOK_AM_LIKE` |
| `DemodStatus` | Enum: `UNKNOWN`, `SKIPPED`, `OK`, `CRC_FAIL`, `LOCK_FAIL` |
| `SampleBlock` | Captured IQ block: `std::vector<std::complex<float>>`, `center_freq_hz`, `sample_rate_hz`, `timestamp_ns` |
| `ClassificationResult` | Full classifier + demod output: `mod_class`, `confidence`, `snr_db`, `decision_trace`, `demod_status`, `demod_lock_ms`, `demod_soft_bits`, `demod_cfo_hz`, `demod_phase_error`, etc. |

`timestamp_ns` is epoch-based (system clock) when the system clock returns
≥ 0; otherwise a monotonic steady-clock fallback is used.

---

### `config.hpp`

Runtime configuration read **once** at startup from environment variables.

```cpp
struct Config { /* all fields with defaults */ };
[[nodiscard]] Config parse_config(int argc, char** argv);   // reads all RF_* env vars; argv[1] may override RF_DB_PATH
```

Helper parsers (in `namespace meek::detail`):

| Helper | Parses | Fallback on error |
|---|---|---|
| `env_ll(name, default)` | `std::stoll` | returns default |
| `env_d(name, default)` | `std::stod` | returns default |
| `env_str(name, default)` | raw string | returns default |

> **Note:** `env_ll`/`env_d` accept trailing junk (e.g. `"123abc"` → `123`) and
> only fall back to the default on an exception.  Use exact numeric strings in
> config files.

Full list of recognised environment variables — see
[`config/thresholds.env.example`](../config/thresholds.env.example) for
descriptions and defaults:

`RF_BLOCK_LEN`, `RF_ANALYSIS_LEN`, `RF_READ_TIMEOUT_US`, `RF_MIN_POWER`,
`RF_SNR_MIN_DB`, `RF_EXPECTED_BW_HZ`, `RF_PAPR_MAX`, `RF_CONF_THRESHOLD`,
`RF_CONSOLE_CONF`, `RF_SNAPSHOT_CONF`, `RF_SCHED_BANDS`, `RF_SCHED_DWELL_MS`,
`RF_DB_PATH`, `RF_SNAPSHOT_DIR`, `RF_SNAPSHOT_RETENTION_DAYS`,
`RF_WORKER_LOG`, `RF_METRICS_FILE`, `RF_HEARTBEAT_FILE`, `RF_PROMETHEUS_PORT`.

---

### `sample_types.hpp` → `ring_buffer.hpp`

### `ring_buffer.hpp`

`SpscRingBuffer<T, Capacity>` — lock-free single-producer / single-consumer
ring buffer.  Capacity must be a power of two.  Head, tail, and the backing
array are each cache-line aligned to prevent false sharing.

- `push(const T&)` / `push(T&&)` — returns `false` when full (non-blocking).
- `pop(T&)` — returns `false` when empty (non-blocking).
- Effective usable capacity is `Capacity - 1` (one sentinel slot).

Used with `Capacity = 64` for both inter-thread queues in the daemon.

---

### `isdr_source.hpp`

SDR hardware abstraction.

| Symbol | Kind | Description |
|---|---|---|
| `SdrSourceConcept` | C++20 concept | Static duck-type check: `read_samples`, `center_freq_hz`, `sample_rate_hz`, `is_open` |
| `ISdrSource` | Abstract base | Virtual interface for runtime polymorphism |
| `ISdrSource::read_samples` | Pure virtual | Returns `0` on timeout, `-2` on overflow, `-1` on fatal error |
| `ISdrSource::set_center_freq` | Virtual (default `false`) | Retune; overridden by `SoapySdrSource` |
| `SoapySdrSource` | Concrete (`HAVE_SOAPY`) | Wraps the SoapySDR C API; calls `SoapySDRDevice_setFrequency` for retuning |

---

### `band_profiles.hpp`

Compile-time UK frequency band table.

```cpp
inline constexpr std::array<BandProfile, 39> kUkBands = {{ ... }};
std::optional<const BandProfile*> find_band(double center_hz) noexcept;
```

Each `BandProfile` carries: `name`, `description`, `center_hz`,
`tolerance_hz`, `expected_bw_hz`, `expected_mod`, `snr_min_db`
(`kBandSnrUseDefault = -999` means use global `RF_SNR_MIN_DB`),
`prior_boost`, and `notes`.

`find_band` breaks ties by smallest `tolerance_hz` (most specific match wins).

Notable profiles (representative sample):

| Name | Frequency | `expected_mod` |
|---|---|---|
| ADS-B | 1090 MHz | `UNKNOWN` (decode externally) |
| VDL2 | 136.9 MHz | `PSK_QAM_LIKE` |
| ISM-433 | 433.92 MHz | `FSK_LIKE` |
| TPMS-433 | 433.92 MHz | `FSK_LIKE` |
| DAB | 174–240 MHz | `PSK_QAM_LIKE` |
| NOAA-WX | 162.5 MHz | `FSK_LIKE` |

---

### `band_scheduler.hpp`

Multi-band rotation scheduler.

```cpp
struct BandSlot { double center_hz; std::chrono::milliseconds dwell; };

class BandScheduler {
  static BandScheduler from_env();   // reads RF_SCHED_BANDS, RF_SCHED_DWELL_MS
  bool enabled() const noexcept;     // true only when >= 2 valid slots
  bool dwell_elapsed(time_point) const noexcept;
  const BandSlot& peek_next() const noexcept;
  void advance(time_point) noexcept;
  void reset_dwell(time_point) noexcept;
};
```

Owned by `capture_loop` — entirely single-threaded; no mutex required.
Scheduling is disabled silently when fewer than two valid frequency slots
are parsed from `RF_SCHED_BANDS`.

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `RF_SCHED_BANDS` | *(unset)* | Comma-separated Hz, e.g. `433920000,868100000` |
| `RF_SCHED_DWELL_MS` | `10000` | Dwell per band in ms (same for all slots) |

---

### `classifier.hpp`

Feature extraction and heuristic modulation classifier.

Top-level entry point used by both the daemon (`proc_loop`) and the
`rf_audit` CLI:

```cpp
ClassificationResult classify_block(
    std::span<const std::complex<float>> samples,
    const Config& cfg,
    const BandProfile* band = nullptr,
    std::vector<float>& scratch = /* thread-local */);
```

Feature helpers (all `[[nodiscard]] inline`):

| Helper | Metric computed |
|---|---|
| `compute_avg_power` | Mean instantaneous power E[|z|²] |
| `compute_snr_db` | SNR: median power as noise floor, top-25% mean as signal |
| `compute_papr_db` | Peak-to-Average Power Ratio |
| `compute_spectral_flatness` | Spectral flatness (Wiener entropy) |
| `compute_occupied_bw_hz` | Occupied bandwidth at −10 dB |
| `compute_time_occupancy` | Fraction of samples above power threshold |
| `compute_avg_abs_phase` | Mean |∠z| (phase spread indicator) |
| `compute_transition_ratio` | Fraction of samples crossing zero (FSK indicator) |

Gating order inside `classify_block`:

1. **Power gate** — rejects blocks below `RF_MIN_POWER`.
2. **SNR gate** — rejects blocks below `RF_SNR_MIN_DB` (or band override).
3. **BW gate** — rejects if occupied BW deviates >25 % from `expected_bw_hz`.
4. **PAPR gate** — rejects if PAPR exceeds `RF_PAPR_MAX` (when > 0).
5. **Classifier** — scores each `ModClass`, applies `prior_boost` from the
   matched `BandProfile` and any `MOD_HINT` additive prior (+0.10).
6. Returns the winning class + confidence + `decision_trace` string.

---

### `demod_chains.hpp` (compiled only with `HAVE_LIQUID`)

Three `noexcept` demodulation functions called by `proc_loop` after
`classify_block` selects a modulation class:

| Function | Triggered by | Algorithm |
|---|---|---|
| `demod_fsk` | `FSK_LIKE` | DC-block IIR → coarse CFO (mean phase increment) → `nco_crcf` PLL → liquid-dsp `fskdem` → CRC-32 check |
| `demod_psk_qam` | `PSK_QAM_LIKE` | `symsync_crcf` RRC timing → Costas-loop carrier recovery (`nco_crcf`) → `modemcf` (QPSK → BPSK fallback on high phase error) → CRC-32 check |
| `demod_ook_am` | `OOK_AM_LIKE` | Envelope detect (`|z|`) → p10/p90 percentile threshold → duty-cycle consistency check → OOK bit recovery at `RSYM` |

All three set `cr.demod_status`, `cr.demod_lock_ms`, and `cr.demod_soft_bits`.
All liquid objects are destroyed on every return path via scope guards.

---

### `db.hpp`

SQLite3 RAII wrapper.  All public methods are called from the output
thread only.

```cpp
class Database {
  static std::unique_ptr<Database> open(const std::string& path);
  std::int64_t insert_signal(source, notes, timestamp_ns);
  std::int64_t upsert_method(name, params_json);
  int insert_example(signal_id, method_id, confidence, notes, result);
};
```

Schema is created on first open via `CREATE TABLE IF NOT EXISTS`.  All writes
use prepared statements (no SQL injection risk).  WAL mode is enabled for
concurrent reads.  `timestamp_ns` is stored as `INTEGER` (epoch ns or
monotonic fallback).

---

### `metrics.hpp`

Prometheus metrics for the daemon.

```cpp
struct ProcMetrics {
  std::atomic<uint64_t> frames_total;
  std::atomic<uint64_t> frames_rejected;
  std::atomic<uint64_t> detections_total;
  // ... additional counters / gauges
};

void write_prometheus_textfile(const ProcMetrics&, const std::string& path);

// HAVE_HTTPLIB only:
std::unique_ptr<httplib::Server> start_prometheus_http(const ProcMetrics&, uint16_t port);
```

The textfile is written by `output_loop` every 5 seconds to
`RF_METRICS_FILE`.  An optional HTTP server on `RF_PROMETHEUS_PORT` serves
`GET /metrics` for pull-based scraping (compiled only when `HAVE_HTTPLIB` is
defined; requires cpp-httplib from FetchContent).

---

## 4. tools/ — CLI utilities

### `tools/iq_metrics.cpp`

Standalone C++20 IQ metrics tool.  No SoapySDR or SQLite required
(`-DBUILD_HARDWARE_TARGETS=OFF`).

Computes four metrics per CF32 file: `avg_power`, `snr_db`,
`spectral_flatness`, `est_bw_hz`.  Output is a JSON array on stdout.

```bash
./build/iq_metrics [--sample-rate FS] [--block-size N] file1.cf32 [file2.cf32 ...]
```

Validated against the Python reference by `tests/test_iq_metrics.py`.

---

### `tools/decode_candidates.py`

Offline modulation decode and JSON audit report generator.

1. Queries `rf_adapt_intel.db` for candidate signal records (by confidence
   threshold and optional time range).
2. Locates matching CF32 snapshot files.
3. Attempts decoding with built-in Python decoders; optionally invokes
   `multimon-ng` and `rtl_433` via subprocess (`--external`).
4. Writes a JSON array of audit objects to stdout or `--out`.

```bash
python3 tools/decode_candidates.py \
    --db rf_adapt_intel.db \
    --snapshot-dir /var/lib/rf-adapt-intel/snapshots \
    --min-confidence 0.6 \
    --limit 20 \
    --external \
    --out /tmp/audit_report.json
```

---

### `tools/meek_report.py`

Self-contained HTML signal intelligence report.  Queries
`rf_adapt_intel.db` and renders an HTML file with detection summaries,
per-band breakdowns, confidence histograms, and decision-trace samples.
No external web framework required — the output is a single `.html` file.

```bash
python3 tools/meek_report.py
python3 tools/meek_report.py --db /path/to/rf_adapt_intel.db --days 7 --out ~/report.html
```

---

### `tools/autotune_thresholds.py`

Automated threshold optimisation.  Analyses CF32 IQ snapshots (or
generates synthetic test vectors when no snapshots are available),
sweeps `RF_CONF_THRESHOLD`, `RF_SNR_MIN_DB`, and `RF_PAPR_MAX` over a
grid, and writes updated values to `config/thresholds.env`.

```bash
python3 tools/autotune_thresholds.py \
    --snapshot-dir /var/lib/rf-adapt-intel/snapshots \
    --out config/thresholds.env
```

---

### `tools/soapy_read_test.cpp`

Minimal SoapySDR stream diagnostic.  Opens the default SDR device, reads
a fixed number of blocks, and prints `readStream()` return values, error
strings, and elapsed time per block.  Useful for confirming USB bandwidth
and overflow rates before deploying the daemon.

```bash
./build/soapy_read_test
```

---

## 5. scripts/ — Bash helpers

All scripts use `set -euo pipefail`.

### `scripts/process_incoming.sh`

Band-aware offline IQ file processor.

- Detects the frequency band from the filename (e.g. `433_signal.raw` →
  ISM-433 band).
- Exports `BAND`, `RSYM`, `FDEV`, `MOD_HINT`, `SNR_MIN`, `PAPR_MAX` into
  the environment.
- Invokes `tools/decode_candidates.py` for offline replay when
  `REPLAY_DB` is set.
- Writes JSON logs with `decision_trace`, `confidence`, and feature stats
  to `RF_WORKER_LOG`.

```bash
OUTPUT_DIR=/tmp/processed bash scripts/process_incoming.sh /path/to/433_signal.raw
REPLAY_DB=/var/lib/rf-adapt-intel/rf_adapt_intel.db \
  bash scripts/process_incoming.sh /path/to/signal.raw
```

---

### `scripts/scan_incoming.sh`

Batch processor for a watched directory.

Configured entirely via environment variables:

| Variable | Description |
|---|---|
| `INCOMING_DIR` | Directory to scan for `*.cf32` and `*.raw` files |
| `PROCESSED_DIR` | Destination for processed files (created with `mkdir -p`) |
| `PROCESS_SCRIPT` | Script to invoke per file (default: `scripts/process_incoming.sh`) |

Always moves files to `PROCESSED_DIR` even when processing fails.
Prints a `[scan_incoming] complete: X processed, Y failed` summary line.

---

### `scripts/heartbeat_and_metrics.sh`

Standalone heartbeat and Prometheus textfile writer.  Can run as a
separate systemd service (`systemd/rf-adapt-intel-monitor.service`) or
manually.  Writes:

- `RF_HEARTBEAT_FILE` — `ok <timestamp>` every 30 seconds.
- `RF_METRICS_FILE` — Prometheus text exposition every 30 seconds.

---

### `scripts/transfer_iq.sh`

rsync IQ snapshot files from the edge node (Ray) to the central server
(Brian) with retries, bandwidth limiting, and logging.

Optionally syncs the SQLite database via `sqlite3 .backup` (online backup
API) when `DB_DEST` is set.  DB sync failures are logged but do not abort
IQ transfers.

```bash
# One-shot
IQ_DEST=rf_worker@brian:/var/lib/rf-adapt-intel/incoming/ bash scripts/transfer_iq.sh

# Continuous watcher (requires inotify-tools)
IQ_DEST=...  DB_DEST=rf_worker@brian:/var/lib/rf-adapt-intel/rf_adapt_intel.db \
  bash scripts/transfer_iq.sh --watch

# Limit bandwidth, set retries
bash scripts/transfer_iq.sh --dest user@host:path --bwlimit 512 --retries 5 --dry-run
```

Key environment variables: `IQ_DEST`, `DB_DEST`, `DB_SOURCE`,
`DB_SYNC_INTERVAL`.

---

### `scripts/check_ssh_permissions.sh`

Verifies that the SSH key and `authorized_keys` permissions for the
IQ-transfer user are correct (`600`/`700`).

---

### `scripts/deploy_and_restart.sh`

Build, install binary to `/usr/local/bin/`, and restart the
`process-worker` systemd service.  Convenience wrapper for iterative
development cycles.

---

## 6. ops/ — Deployment and operations

### `ops/deploy.sh`

Atomic systemd service deployment.

- Copies `systemd/process-worker.service` and all drop-ins to
  `/etc/systemd/system/`.
- Creates `/var/lib/rf-adapt-intel/` and the snapshot subdirectory.
- Runs `systemctl daemon-reload && systemctl restart process-worker`.
- Installs the scheduled monitoring timer/service.
- Supports `--dry-run`, `--setup` (runs `ops/setup.sh` first).

---

### `ops/setup.sh`

Optional decoder and tool installer.

| Flag | Installs |
|---|---|
| `--install-multimon-ng` | `apt install multimon-ng` |
| `--install-rtl_433` | `apt install rtl-433` |
| `--install-liquid-dsp` | Build liquid-dsp from source (NEON auto-detected on ARM) |
| `--non-interactive` | Skip all interactive prompts |
| `--dry-run` | Print commands without executing |

After installation, decoder binary paths are appended to
`/etc/rf_worker/thresholds.env`.

---

### `ops/verify.sh`

Hardening verification.  Checks that all `systemd show` properties match
expected values (`ProtectSystem=full`, `ProtectHome=yes`,
`ReadWritePaths`, `LimitNOFILE`, `TasksMax`) and that the service is
running.

---

### `ops/canary.sh`

Canary lifecycle manager.

```bash
sudo bash ops/canary.sh              # enable canary (RF_SNR_MIN_DB=0)
sudo bash ops/canary.sh --status     # FP/FN counters, CPU/memory, lock-fail metrics
sudo bash ops/canary.sh --promote    # auto-check acceptance criteria, promote to prod
sudo bash ops/canary.sh --rollback   # restore last backed-up production config
```

Automated promotion checks (all must pass):

- Classifier ≥ 95 % accuracy at ≥ 0 dB SNR.
- False-positive rate < 3 % over the monitoring window.
- CPU usage < 80 %.
- No lock-fail counter increases in the last 30 minutes.

---

### `ops/autotune.sh`

Thin wrapper that runs `tools/autotune_thresholds.py` against the
production snapshot directory and writes updated thresholds to
`/etc/rf_worker/thresholds.env`, then restarts the service.

---

## 7. systemd/ — Service units and drop-ins

### `systemd/process-worker.service`

Main hardened unit for the `rf_adapt_intel` daemon.  Loads environment
from both `/etc/rf_worker/thresholds.env` and
`/etc/default/rf-adapt-intel`.

Key hardening properties:

| Property | Value |
|---|---|
| `ProtectSystem` | `full` |
| `ProtectHome` | `yes` |
| `NoNewPrivileges` | `yes` |
| `PrivateTmp` | `yes` |
| `MemoryDenyWriteExecute` | `yes` |
| `SystemCallFilter` | `@system-service @chown @file-system` |
| `ReadWritePaths` | `/var/lib/rf-adapt-intel` |
| `LimitNOFILE` | `4096` |
| `TasksMax` | `2048` |

---

### `systemd/process-worker.service.d/`

Drop-in overrides applied after the base unit:

| File | Purpose |
|---|---|
| `hardening.conf` | Additional syscall/capability restrictions |
| `override.conf` | Environment / threshold overrides |
| `processor.conf` | Thread/queue tuning |

---

### `systemd/rf-adapt-intel-monitor.{service,timer}`

Scheduled canary monitor.  The timer unit triggers `--status` on
`ops/canary.sh` every 30 minutes.  Installed by `ops/deploy.sh`.

---

### `systemd/rf-incoming-processor.{path,service,@.service}`

Path-activated batch processor.  Watches `RF_SNAPSHOT_DIR/incoming/` for
new files and invokes `scripts/scan_incoming.sh` on each batch.

---

### `systemd/iq-transfer-watcher.service`

Continuous IQ transfer watcher service wrapping
`scripts/transfer_iq.sh --watch`.

---

## 8. config/ — Runtime configuration

### `config/thresholds.env.example`

Template for `/etc/rf_worker/thresholds.env`.  Copy and customise:

```bash
sudo cp config/thresholds.env.example /etc/rf_worker/thresholds.env
```

Full reference of all `RF_*` environment variables with descriptions and
defaults — see the file itself for inline comments.

### `config/iq-transfer.env.example`

Template for the IQ transfer service environment (`IQ_DEST`, `DB_DEST`,
`SSH_KEY`, `BWLIMIT`, etc.).

### `config/logrotate.d/`

logrotate configuration for `RF_WORKER_LOG` and the Prometheus textfile.

---

## 9. tests/ — Test harnesses

### Python tests (`unittest`)

All Python tests use only the standard library plus `numpy`.

| File | What it tests |
|---|---|
| `test_snr_sweep.py` | Classifier accuracy and FP rate across all modulations × SNR levels (confusion matrix). Acceptance gates: ≥ 95 % @ ≥ 0 dB, ≥ 85 % @ −6 dB, FP < 3 %. |
| `test_guardrails.py` | SNR gate, BW gate, PAPR gate rejections; worker-log JSON checks. |
| `test_demod_ber.py` | BER and CRC-32 validation for FSK, BPSK, and OOK demod chains. |
| `test_demod_timing.py` | Demod lock convergence time gates (DEM-04, DEM-05). |
| `test_classifier_correctness.py` | Classifier output gate tests (CLF-01 through CLF-04). |
| `test_band_profiles.py` | Static correctness of `kUkBands` (count, field ranges, `find_band` tie-break). |
| `test_db_wal.py` | SQLite WAL mode availability, `timestamp_ns` schema, `examples.result` column. |
| `test_decode_candidates.py` | `tools/decode_candidates.py` CLI including `--external` flag. |
| `test_iq_metrics.py` | C++ `iq_metrics` output vs. Python reference (per-metric tolerance). |
| `test_band_scheduler.py` | `BandScheduler` rotation, dwell timing, `from_env` parsing. |
| `test_autotune.py` | `tools/autotune_thresholds.py` threshold optimisation. |
| `test_meek_report.py` | `tools/meek_report.py` HTML generation. |
| `test_meek_report_queries.py` | SQL queries used by `meek_report.py`. |
| `test_rf_audit.py` | `rf_audit` CLI JSON output fields and exit codes. |
| `test_heartbeat_and_metrics.sh` | `scripts/heartbeat_and_metrics.sh` output format. |
| `bench_throughput.py` | Processing throughput (frames/minute) acceptance gate: ≥ 100 on Brian, ≥ 20 on Ray. |

---

### Shell tests (`bash`)

| File | What it tests |
|---|---|
| `test_setup.sh` | `ops/setup.sh` flag parsing, `--dry-run`, non-interactive mode. |
| `test_install.sh` | `install.sh` flag parsing, `--dry-run`. |
| `test_deploy.sh` | `ops/deploy.sh` flag parsing, `--dry-run`. |
| `test_canary.sh` | `ops/canary.sh` `--status` / `--dry-run`. |
| `test_scan_incoming.sh` | `scripts/scan_incoming.sh` file processing and move behaviour. |
| `test_check_ssh_permissions.sh` | `scripts/check_ssh_permissions.sh` permission checks. |

Shell tests share a minimal harness: `VERBOSE` flag, `ok`/`fail` helpers,
`assert_contains`/`assert_exit` helpers.  Temp dirs use
`mktemp -d /tmp/<prefix>.XXXXXX`.

---

### `tests/gen_test_signals.py`

Synthetic IQ vector generator.  Produces RRC-shaped CF32 test vectors for
all supported bands and modulations at configurable SNR levels.  Used by
the Python demod and classifier tests.

---

## 10. benchmarks/ — Throughput benchmarks

### `benchmarks/bench_iq_metrics.py`

Compares per-block latency of the C++ `iq_metrics` tool against the
Python reference implementation.  Sweeps configurable block sizes and
repetitions, writes JSON results to `benchmarks/results/`.

```bash
python3 benchmarks/bench_iq_metrics.py build/iq_metrics \
    --repetitions 20 \
    --block-sizes 1024,4096,16384,65536
```

---

## 11. docs/ — Documentation

| File | Contents |
|---|---|
| `codebase.md` | This file — per-directory reference guide. |
| `INSTALL.md` | Step-by-step installation for Bookworm and Noble; hardware requirements, build, deploy, optional decoders, two-node setup, troubleshooting. |
| `rf-adapt-intel-plan.md` | Full design plan (classifier, demod pipelines, hardening, observability, validation matrix) with execution status. |
| `missing-features.md` | Implementation gap tracker — maps every planned feature to its current status (✅ / ⬜) and source file. |
| `audit.md` | Production-readiness audit: dependency map, C++ and Python dep tables, security observations, CI gaps, migration notes. |
| `db_queries.sql` / `sql-queries.md` | Reference SQL queries for the `rf_adapt_intel.db` schema. |
