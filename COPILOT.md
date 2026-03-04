# GitHub Copilot in this repository

This repository is configured for GitHub Copilot.  The notes below describe
how to get the best results when using Copilot with this codebase.

## Language and style

- **C++17** — structured bindings, `std::filesystem`, `if constexpr`, etc.
- `snake_case` for variables and functions throughout `src/main.cpp`.
- Prefer `[[nodiscard]]` on functions returning error codes or resources.
- Lines formatted to ≤ 100 characters; `clang-format -i src/*.cpp` before committing.
- cpplint pre-commit hook enforced — see `.cpplint-rationale.md`.

## Useful Copilot Chat prompts

```
Explain the capture-to-classifier data flow in src/main.cpp.
Write a liquid-dsp FSK demod chain that follows the pattern used by classify_block().
Generate a pytest-style test for tools/decode_candidates.py that covers the --external flag.
Refactor the env_to_* helpers in src/main.cpp to reduce repetition.
Add a Doxygen comment block to the classify_block() function.
```

## Key architecture notes Copilot should know

- **Thread model:** one capture thread → bounded `std::deque` (max 64 items) → one processing thread.
- **Snapshot worker:** single joinable `snapshot_worker` thread with a task queue — no detached threads.
- **SQLite writes:** processing thread only; never from the capture thread.
- **Signal handling:** `SIGINT`/`SIGTERM` set the `running` atomic flag; all thread loops check it.
- **Env vars:** all runtime configuration comes from environment variables; use the `env_to_*` helpers for new parameters.
- **liquid-dsp:** guarded by `#ifdef HAVE_LIQUID`; CMake auto-detects via pkg-config or CMake config.
- **Band profiles:** `UK_BANDS[]` in `src/main.cpp` — 18 entries with per-band SNR, BW, and prior_boost.

## Resources

- [SoapySDR API docs](https://pothosware.github.io/SoapySDR/doxygen/html/)
- [liquid-dsp manual](https://liquidsdr.org/doc/)
- [SQLite C API](https://www.sqlite.org/c3ref/intro.html)
