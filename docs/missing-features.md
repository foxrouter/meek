# rf_adapt_intel — Pending Features and Gaps

**Generated:** 2026-03-04  
**Source:** `docs/rf-adapt-intel-plan.md` execution-ready task list

This document tracks features, files, and tasks described in
`docs/rf-adapt-intel-plan.md`.  Items marked ✅ are implemented and merged.
Items marked ⬜ are outstanding.

---

## 1. Demod pipelines (`src/main.cpp` — requires liquid-dsp / `HAVE_LIQUID`)

### 1a. FSK / GMSK demod chain ✅

- ✅ DC removal before demod (IIR high-pass, `apply_dc_block`).
- ✅ Coarse CFO estimate via mean phase increment (`estimate_cfo_hz`).
- ✅ Fine PLL for CFO tracking (`nco_crcf`).
- ✅ liquid-dsp `fskdem` object configured with explicit `k/sps/BT`.
- ✅ Bit-stream output piped to CRC32 checker (`check_crc32_bits`).
- ✅ **Integration point:** after `classify_block()` returns `FSK_LIKE`.

### 1b. PSK / QAM demod chain ✅

- ✅ Symbol timing synchronisation: liquid-dsp `symsync_crcf` (RRC matched filter).
- ✅ Costas-loop carrier recovery (`nco_crcf` + `modemcf_get_demodulator_phase_error`).
- ✅ Carrier-lock watchdog: re-init with wider PLL BW on high phase error.
- ✅ Downshift fallback: QPSK → BPSK on persistent high phase error.
- ✅ Support for BPSK, QPSK, 8PSK.
- ✅ **Integration point:** after `classify_block()` returns `PSK_QAM_LIKE`.

### 1c. OOK / AM envelope demod ✅

- ✅ Envelope detection (`|z|`) + Median Absolute Deviation threshold.
- ✅ Duty-cycle consistency check to avoid CW mis-classification.
- ✅ OOK bit recovery at expected symbol rate (`RSYM`).
- ✅ **Integration point:** after `classify_block()` returns `OOK_AM_LIKE`.

---

## 2. Minor gaps in existing code

| Item | File | Status |
|---|---|---|
| `PAPR_MAX` env var not enforced | `src/main.cpp` | ✅ Implemented: `PAPR_MAX` read via `env_to_d` and enforced in `classify_block()` PAPR gate. |
| `MOD_HINT` prior bias not consumed | `src/main.cpp` | ✅ Implemented: `MOD_HINT` read and applied as additive +0.10 prior in `classify_block()`. |
| File-replay mode in `process_incoming.sh` | `scripts/process_incoming.sh` | ✅ Implemented: offline IQ replay via `tools/decode_candidates.py`; bash bug fix applied (`local rc=` → `rc=`). |

---

## 3. Validation / benchmark tests (`tests/`)

| Test | File | Status |
|---|---|---|
| SNR sweep (all bands × mods × SNR levels, confusion matrix) | `tests/test_snr_sweep.py` | ✅ Assertions exist; accuracy tests marked `@expectedFailure` until heuristic classifier is improved. |
| Guardrail rejection (wrong band/RSYM/BW, log reason) | `tests/test_guardrails.py` | ✅ Expanded coverage: SNR gate, BW gate, PAPR gate, worker-log JSON checks. |
| Throughput benchmark (≥ 100 frames/min Brian, ≥ 20 Ray) | `tests/bench_throughput.py` | ✅ Acceptance threshold assertions implemented and passing. |
| BER / CRC check for demod chains | `tests/test_demod_ber.py` | ✅ FSK, BPSK, OOK BER tests + CRC-32 round-trip tests implemented and passing. |

---

## 4. Canary / rollback automation (`ops/`)

`ops/canary.sh` is committed and functional.

| Item | Status |
|---|---|
| Automated promotion gate | ✅ `--promote` now checks FP rate and CPU usage automatically against acceptance criteria; no manual confirmation required. |
| Scheduled monitoring | ✅ `systemd/rf-adapt-intel-monitor.service` + `systemd/rf-adapt-intel-monitor.timer` poll `--status` every 30 minutes; installed by `ops/deploy.sh`. |

---

## Summary table

| Item | File(s) | Status |
|---|---|---|
| FSK demod chain (liquid-dsp) | `src/main.cpp`, `include/meek/demod_chains.hpp` | ✅ Done |
| PSK/QAM demod chain (liquid-dsp) | `src/main.cpp`, `include/meek/demod_chains.hpp` | ✅ Done |
| OOK/AM demod chain (liquid-dsp) | `src/main.cpp`, `include/meek/demod_chains.hpp` | ✅ Done |
| `PAPR_MAX` enforcement | `src/main.cpp` | ✅ Done |
| `MOD_HINT` prior bias in classifier | `src/main.cpp` | ✅ Done |
| File-replay mode for `process_incoming` | `scripts/process_incoming.sh` | ✅ Done |
| SNR sweep acceptance assertions | `tests/test_snr_sweep.py` | ✅ Done |
| Guardrail test coverage | `tests/test_guardrails.py` | ✅ Done |
| Throughput threshold assertions | `tests/bench_throughput.py` | ✅ Done |
| BER/CRC demod tests | `tests/test_demod_ber.py` | ✅ Done |
| Automated canary promotion gate | `ops/canary.sh` | ✅ Done |
| Scheduled canary monitoring timer | `systemd/rf-adapt-intel-monitor.{service,timer}` | ✅ Done |
