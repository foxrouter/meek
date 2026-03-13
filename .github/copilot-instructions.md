# Copilot Instructions

## Project Overview

This repository implements `rf_adapt_intel`, a C++20 RF signal processing worker that captures IQ samples via SoapySDR, classifies modulation (GMSK/FSK/PSK/QAM/OOK), and persists results to SQLite. It is deployed as a systemd service on embedded Linux (Raspberry Pi and Ubuntu server).

## Tech Stack

- **Language:** C++20
- **Build system:** CMake (≥ 3.25); build with `-DCMAKE_BUILD_TYPE=Release`
- **Key libraries:** SoapySDR (SDR hardware abstraction), SQLite3, nlohmann/json (FetchContent), cpp-httplib (FetchContent, optional Prometheus metrics), liquid-dsp (optional, for advanced demodulation)
- **Linting/formatting:** clang-format (v14), cpplint (via pre-commit hooks in `.pre-commit-config.yaml`), clang-tidy (CI)
- **Shell scripts:** Bash with `set -euo pipefail`

## Building

```bash
mkdir build && cd build
cmake -S .. -B . -DCMAKE_BUILD_TYPE=Release
cmake --build . -- -j$(nproc)
```

If liquid-dsp is installed, CMake will detect it automatically via pkg-config and enable `HAVE_LIQUID`. Always guard liquid-dsp code with `#ifdef HAVE_LIQUID`.

## Code Style

- Follow the clang-format style already configured in the repo; run `clang-format -i src/*.cpp include/meek/*.hpp` before committing.
- cpplint is enforced; suppress only the filters listed in `.pre-commit-config.yaml` (`-build/c++11,-build/c++17`). Line length limit is 100.
- Use C++20 features (structured bindings, `std::filesystem`, `if constexpr`, `std::span`, ranges, etc.) where they improve clarity.
- Prefer `[[nodiscard]]` on functions that return error codes or resources.
- Match the existing style: `snake_case` for variables and functions, `PascalCase` is not used.
- Include `<httplib.h>` at global scope (before any namespace) to avoid GCC 12+ two-phase lookup failures on aarch64.

## Key Patterns

- **Runtime configuration** is done exclusively via environment variables (e.g., `RF_BLOCK_LEN`, `RF_CONF_THRESHOLD`, `RF_SNAPSHOT_DIR`). Use the `env_ll`/`env_d`/`env_str` helpers in `include/meek/config.hpp` for new parameters.
- **Thread model:** one capture thread → lock-free `SpscRingBuffer<SampleBlock, 64>` → one processing thread → `SpscRingBuffer<ClassificationResult, 64>` → one output thread.
- **SQLite writes** happen only on the output thread; never write to DB from the capture thread or snapshot worker.
- **Snapshot worker** is a single background `snap_thread` (`std::jthread`) with a `std::deque<SnapTask>` task queue (guarded by a mutex); it writes IQ snapshots to disk without blocking the processing thread.
- **Signal handling:** `SIGINT`/`SIGTERM` set the `g_shutdown` atomic flag; all thread loops check `g_shutdown` or their `std::stop_token`.
- **Band profiles:** `kUkBands` (`constexpr std::array<BandProfile, 38>`) in `include/meek/band_profiles.hpp` defines 38 frequency bands with per-band SNR, BW, and `prior_boost` fields.

## Testing

Python integration tests live under `tests/` (e.g., `test_decode_candidates.py`, `test_iq_metrics.py`, `test_snr_sweep.py`). Run them with `pytest tests/`. Synthetic IQ vectors can be generated with `tests/gen_test_signals.py`. CTest also wires shell-based smoke tests; run with `ctest --test-dir build`.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs: Python lint + pytest, C++ build + clang-format + clang-tidy, ASAN/UBSAN sanitizer build, and pip-audit dependency scan. clang-tidy for `tools/iq_metrics.cpp` requires `-I build/_deps/nlohmann_json-src/include` for the FetchContent-downloaded header.

## Deployment

Use `scripts/deploy_and_restart.sh` for build and systemd service restart. The service is named `process-worker`. See `docs/rf-adapt-intel-plan.md` for full deployment and hardening details.

## Security / Secrets

- Do **not** commit secrets or environment-specific thresholds. Use `config/thresholds.env` (git-ignored) for local overrides.
- Run `gitleaks detect --source .` before pushing sensitive changes.

## Useful Copilot Chat Prompts

```
Explain the capture-to-classifier data flow in src/main.cpp.
Write a liquid-dsp FSK demod chain that follows the pattern used by classify_block().
Generate a pytest-style test for tools/decode_candidates.py that covers the --external flag.
Refactor the env_ll/env_d/env_str helpers in include/meek/config.hpp to reduce repetition.
Add a Doxygen comment block to the classify_block() function.
```

## Resources

- [SoapySDR API docs](https://pothosware.github.io/SoapySDR/doxygen/html/)
- [liquid-dsp manual](https://liquidsdr.org/doc/)
- [SQLite C API](https://www.sqlite.org/c3ref/intro.html)
