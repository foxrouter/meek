# rf_adapt_intel — Pending Features and Gaps

**Generated:** 2026-03-04  
**Source:** `docs/rf-adapt-intel-plan.md` execution-ready task list

This document lists features, files, and tasks that are described in
`docs/rf-adapt-intel-plan.md` but are **not yet present** in the repository.
Each entry notes where in the codebase it belongs and what is needed.

---

## 1. Demod pipelines (`src/main.cpp` — requires liquid-dsp / `HAVE_LIQUID`)

### 1a. FSK / GMSK demod chain

- DC removal before demod.
- Coarse FFT peak detection for initial CFO estimate.
- Fine PLL for carrier-frequency-offset (CFO) tracking.
- liquid-dsp `fskdem` object configured with explicit `k/sps/BT`.
- Bit-stream output piped to CRC32 checker.
- **Integration point:** after `classify_block()` returns `FSK_LIKE`.

### 1b. PSK / QAM demod chain

- Symbol timing synchronisation: liquid-dsp `symsync` object.
- Costas-loop carrier recovery.
- Carrier-lock watchdog: detect unlock condition; re-init with coarse CFO estimate on failure.
- Downshift fallback: QPSK → BPSK on persistent high phase error.
- Support for BPSK, QPSK, 8PSK at minimum.
- **Integration point:** after `classify_block()` returns `PSK_QAM_LIKE`.

### 1c. OOK / AM envelope demod

- Envelope detection (`|z|`) + Median Absolute Deviation threshold.
- Duty-cycle consistency check to avoid CW mis-classification.
- OOK bit recovery at expected symbol rate (`RSYM`).
- **Integration point:** after `classify_block()` returns `OOK_AM_LIKE`.

---

## 2. Minor gaps in existing code

| Item | File | Notes |
|---|---|---|
| `PAPR_MAX` env var not enforced | `src/main.cpp` | Documented in `config/thresholds.env.example` and exported by `process_incoming.sh` but not read in `classify_block()`. |
| `MOD_HINT` prior bias not consumed | `src/main.cpp` | Exported by `process_incoming.sh`, but `classify_block()` only applies `BandProfile::prior_boost`; `MOD_HINT` is ignored. |
| File-replay mode in `process_incoming.sh` | `scripts/process_incoming.sh` | `rf_adapt_intel` reads from SoapySDR, not from a file; a named-pipe relay or dedicated replay mode is needed for true offline testing. |

---

## 3. Validation / benchmark tests (`tests/`)

| Test | File | Status |
|---|---|---|
| SNR sweep (all bands × mods × SNR levels, confusion matrix) | `tests/test_snr_sweep.py` | File exists; assertions against acceptance criteria pending |
| Guardrail rejection (wrong band/RSYM/BW, log reason) | `tests/test_guardrails.py` | File exists; expand coverage |
| Throughput benchmark (≥ 100 frames/min Brian, ≥ 20 Ray) | `tests/bench_throughput.py` | File exists; acceptance threshold assertions pending |
| BER / CRC check for demod chains | `tests/test_demod_ber.py` | File exists; requires demod pipelines (item 1 above) |

---

## 4. Canary / rollback automation (`ops/`)

`ops/canary.sh` is committed and functional.  Outstanding items:

- Automated promotion gate: script-driven check of Prometheus metrics against
  acceptance criteria (currently requires manual confirmation).
- Scheduled monitoring: cron or systemd timer to poll `--status` and alert on
  threshold breach.

---

## Summary table

| Item | File(s) to add / modify |
|---|---|
| FSK demod chain (liquid-dsp) | `src/main.cpp` |
| PSK/QAM demod chain (liquid-dsp) | `src/main.cpp` |
| OOK/AM demod chain (liquid-dsp) | `src/main.cpp` |
| `PAPR_MAX` enforcement | `src/main.cpp` |
| `MOD_HINT` prior bias in classifier | `src/main.cpp` |
| File-replay mode for `process_incoming` | `scripts/process_incoming.sh` |
| SNR sweep acceptance assertions | `tests/test_snr_sweep.py` |
| Guardrail test coverage | `tests/test_guardrails.py` |
| Throughput threshold assertions | `tests/bench_throughput.py` |
| BER/CRC demod tests | `tests/test_demod_ber.py` |
| Automated canary promotion gate | `ops/canary.sh` |
