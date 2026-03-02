# Copilot Instructions

## Project Overview

This repository implements `rf_adapt_intel`, a C++17 RF signal processing worker that captures IQ samples via SoapySDR, classifies modulation (GMSK/FSK/PSK/QAM/OOK), and persists results to SQLite. It is deployed as a systemd service on embedded Linux (Raspberry Pi and Ubuntu server).

## Tech Stack

- **Language:** C++17
- **Build system:** CMake (≥ 3.10); build with `-DCMAKE_BUILD_TYPE=Release`
- **Key libraries:** SoapySDR (SDR hardware abstraction), SQLite3, liquid-dsp (optional, for advanced demodulation), pthreads
- **Linting/formatting:** clang-format (v14), cpplint (via pre-commit hooks in `.pre-commit-config.yaml`)
- **Shell scripts:** Bash with `set -euo pipefail`

## Building

```bash
mkdir build && cd build
cmake -S .. -B . -DCMAKE_BUILD_TYPE=Release
cmake --build . -- -j$(nproc)
```

If liquid-dsp is installed, CMake will detect it automatically and enable `HAVE_LIQUID`.

## Code Style

- Follow the clang-format style already configured in the repo; run `clang-format -i src/*.cpp` before committing.
- cpplint is enforced; suppress only the filters listed in `.pre-commit-config.yaml` (`-build/include_order,-build/c++11,-whitespace/line_length`).
- Use C++17 features (structured bindings, `std::filesystem`, `if constexpr`, etc.) where they improve clarity.
- Prefer `[[nodiscard]]` on functions that return error codes or resources.
- Match the existing style: `snake_case` for variables and functions, `PascalCase` is not used.

## Key Patterns

- **Runtime configuration** is done exclusively via environment variables (e.g., `RF_BLOCK_LEN`, `RF_CONF_THRESHOLD`, `RF_SNAPSHOT_DIR`). Use the `env_to_*` helpers in `src/main.cpp` for new parameters.
- **Thread model:** one capture thread pushes `SampleBlock` items onto a bounded `std::deque` (max 64); one processing thread consumes them. Synchronization uses `std::mutex` + `std::condition_variable`.
- **SQLite writes** happen only on the processing thread; never write to DB from the capture thread or detached threads.
- **Snapshot writes** are dispatched to detached `std::thread`s to avoid blocking the processing thread; keep detached work minimal and exception-safe.
- **Signal handling:** `SIGINT`/`SIGTERM` set the `running` atomic flag; always check `running` in thread loops.

## Testing

There are no automated unit tests yet. When adding tests, place them under `tests/` and use the existing CMake project. Synthetic IQ vectors can be generated with `gen_test_signals.py` (see `docs/rf-adapt-intel-plan.md`).

## Deployment

Use `scripts/deploy_and_restart.sh` for build and systemd service restart. The service is named `rf-adapt-intel`. See `docs/rf-adapt-intel-plan.md` for full deployment and hardening details.

## Security / Secrets

- Do **not** commit secrets or environment-specific thresholds. Use `config/thresholds.env` (git-ignored) for local overrides.
- Run `gitleaks detect --source .` before pushing sensitive changes.
