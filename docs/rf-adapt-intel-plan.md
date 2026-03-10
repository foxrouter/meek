# RF Adaptation & Intelligence Plan (Aligned/Upgraded, AI-Readable)

**Generated:** 2025-12-19 (updated 2026-03-04)  
**Author:** foxrouter (via assistant)  
**Status:** Active — classifier, service hardening, observability, test infrastructure, band profiles, and ops scripts all committed; demod pipelines and full validation matrix pending

---

## Purpose
- Replace naive amplitude thresholding with robust modulation detection/demodulation using **liquid-dsp**.
- Harden `process-worker.service` with systemd drop-ins.
- Run a two-stage pipeline: presence → classification → modulation-specific demodulation.

## Systems
- **Ray (Raspberry Pi)**: Edge/SDR build + test, generate RRC-shaped IQ vectors, liquid-dsp build with NEON if supported.
- **Brian (Ubuntu server)**: Central processing/canary, systemd deployment, telemetry, metrics.

## Current status (delta)
- Classifier: ✅ heuristic prototype (spectral entropy/flatness, peak_frac, PAPR, presence). Fixtures: tone→cw_like; OOK→am/ook_like; FSK2→fsk_like; BPSK/QPSK→psk/qam_like.
- Test fixtures: ✅ `gen_test_signals.py` with RRC-shaped generators; `tests/test_decode_candidates.py` + `tools/decode_candidates.py` committed.
- Code hygiene: ✅ cpplint re-enabled; latest lint fixes committed; `tests/test_setup.sh` shell test suite committed.
- Service hardening/deploy: ✅ `systemd/process-worker.service` + drop-ins (`hardening.conf`, `override.conf`, `processor.conf`) committed; `ops/deploy.sh`, `ops/verify.sh`, `ops/setup.sh` all committed.
- liquid-dsp build (Ray): ✅ `ops/setup.sh --install-liquid-dsp` builds from source (NEON auto-detected by configure); HAVE_LIQUID conditional integration in `main.cpp` is ⏳ pending.
- UK band profile table: ✅ 33 profiles in `kUkBands` (`include/meek/band_profiles.hpp`) with `find_band()`, per-band SNR overrides, BW hints, and prior_boost committed.
- Heartbeat + Prometheus textfile: ✅ written by output thread (`output_loop`) in `main.cpp`; Prometheus textfile every 5 s, heartbeat every 30 s; also `scripts/heartbeat_and_metrics.sh` for standalone use.
- JSON logging: ✅ `write_json_log()` emits `decision_trace`, `confidence`, all features, band name/notes to `worker.log`.
- SNR / BW guardrails: ✅ `classify_block()` enforces SNR gate and ±25% BW guardrail; per-band overrides via `BandProfile`.
- Snapshot worker: ✅ single background thread with task queue; clean shutdown (no detached threads).
- process_incoming.sh: ✅ filename-based band detection, env var export, offline IQ file processing.
- Demod pipelines (FSK/PSK/QAM): ⏳ not started — liquid-dsp demod chains absent from `main.cpp`.
- Canary / rollback: ✅ `ops/canary.sh` committed with --status/--promote/--rollback/--dry-run; automated promotion gate pending.

## High-level sequence
1) Prepare Ray (build liquid-dsp, generate IQ test vectors).  
2) Prepare Brian (deploy hardened drop-ins, run process-worker in passive/canary mode).  
3) Functional tests: send IQ vectors Ray → Brian; capture telemetry.  
4) Iterate classifier/demod pipelines.  
5) Canary active decoding; promote when metrics hit acceptance.  
6) Keep rollback paths and backups.

## Classifier / feature pipeline
- AGC sanity + watchdog; SNR gate >0 dB (unless canary).  
- PAPR + spectral flatness (p50/p90), burst-length hysteresis, time-occupancy for CW vs OOK.  
- RRC consistency check; resample via `msresamp`.  
- Confidence/logit per class; emit `confidence` + `decision_trace`.  
- Band guardrails: reject demod if BW deviates >25% from expected.

## Demod paths (liquid-dsp)
- FSK: `fskdem` with explicit `k/sps/BT`; DC removal; coarse FFT peak + fine PLL for CFO.  
- PSK/QAM: default `qpsk`, fallback `bpsk/8psk` on phase error; symsync timing; Costas; carrier recovery watchdog + re-init.  
- OOK/AM: envelope detect + MAD threshold; duty-cycle consistency to avoid CW confusion.

## Bands and defaults
- Core ISM: 433 MHz (RSYM=128k, FDEV=50 kHz); 868/915 MHz (RSYM=250k, FDEV=62.5 kHz); guardrails BW ±25%, SNR >0 dB (unless canary).  
- Additional RTL-SDR v3 profiles (Skyscan 25–2000 MHz): 315 ISM/TPMS; 137 WX (APT/Meteor); Airband AM; ACARS 131.55; VHF LMR/pagers; NOAA WX voice; UHF AM/NFM; UHF LMR/telemetry; 868 EU alt; 902–928 ISM (±20% RSYM sweep); 1090 ADS-B (PPM, ext decoder best); 1.2–1.3 GHz telemetry.  
- Receiver: 2.048–2.4 MSPS (narrow sweeps); 3.2–3.84 if CPU allows; DC offset removal; bias-T off by default; overlap for wide scans.

## Operational guardrails
- SNR gate >0 dB (unless canary).  
- BW gate ±25% expected.  
- Lock watchdog: re-init with coarse CFO on failure.  
- Downshift PSK/QAM: QPSK→BPSK on high phase error.

## Automation / scripts
- `process_incoming.sh`: filename → band map; set env (`BAND`, `RSYM`, `FDEV`, `MOD_HINT`, `SNR_MIN`, `PAPR_MAX`); JSON logs with `decision_trace`/`confidence`/features to `/var/lib/rf-adapt-intel/worker.log`; outputs to `/var/lib/rf-adapt-intel/processed/<basename>.raw`.

## Hardening (process-worker.service)
- `ProtectSystem=full`, `ProtectHome=yes`, `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectClock=yes`, `ProtectKernelLogs=yes`
- `SystemCallFilter=@system-service @chown @file-system`
- `MemoryDenyWriteExecute=yes`
- `CapabilityBoundingSet=~CAP_SYS_ADMIN CAP_SYS_MODULE`
- `RestrictSUIDSGID=yes`, `RestrictNamespaces=yes`
- `LimitNOFILE=4096`, `TasksMax=2048`
- Read-only binds except `/var/lib/rf-adapt-intel` and private `/tmp`
- Drop-ins: `hardening.conf` (syscall/caps), `override.conf` (env/thresholds), `processor.conf` (threads/queues)
- Backups in `/root` with timestamps; hash-check on deploy.

## Observability
- Heartbeat FIFO/file: `ok <timestamp>`.
- Prometheus textfile: `/var/lib/rf-adapt-intel/metrics.prom` (frames, rejects, confidence averages).
- Logs: include `decision_trace`, `confidence`, feature stats, rejection reasons.

## Validation & benchmarks
- Matrix: bands 433/915 + added profiles; mods BPSK/QPSK/8PSK, 2/4-FSK, OOK/ASK, CW; SNR +10/+3/0/-3/-6/-10 dB; payload 70–90 B with CRC32; symbol rates default ±20%.  
- Acceptance: classifier ≥95% @ ≥0 dB; ≥85% @ -6 dB; FP <3% @ ≥0 dB; lock <50 ms PSK, <30 ms FSK; throughput ≥100 frames/min (Brian), ≥20 frames/min (Ray).  
- Guardrail tests: wrong band/RSYM/BW rejected; BER/CRC checked vs known payloads.

## Rollout & rollback
- Rollout: canary subset, `SNR_MIN=0` initial; promote when FP within targets; relax later if acceptable.  
- Rollback: remove drop-ins, restore backups, daemon-reload/restart.

## Execution-ready tasks (updated)
Brian (ops/central)
- [x] Deploy systemd unit + drop-ins (`process-worker.service.d/{hardening.conf,override.conf,processor.conf}`); `ops/deploy.sh` committed and tested with `--dry-run`.
- [x] Run `verify.sh`; `ops/verify.sh` checks ProtectSystem, ProtectHome, ReadWritePaths, LimitNOFILE, TasksMax, and service status.
- [x] Enable JSON logging: `write_json_log()` in `main.cpp` emits `decision_trace`, `confidence`, feature stats, rejection reasons to `worker.log`.
- [x] Add heartbeat writer and Prometheus textfile: `write_heartbeat()` + `write_prometheus_metrics()` in `main.cpp`; standalone `scripts/heartbeat_and_metrics.sh`.
- [x] Wire guardrails: SNR gate >0 dB (except canary via `RF_SNR_MIN_DB`), BW gate ±25%, per-band overrides via `kUkBands`.

Ray (edge)
- [x] Build/install liquid-dsp: `ops/setup.sh --install-liquid-dsp` builds from source (NEON via `./configure`); smoke-tested via pkg-config.
- [x] Generate RRC-shaped vectors across mods/SNRs with `gen_test_signals.py`; bands 315, 433, 868/915, 137, 150 MHz all supported.
- [x] Transfer IQs to Brian via `scripts/transfer_iq.sh` (rsync with retries, bandwidth limiting, inotify watch mode); automated triggering on file-arrival not yet wired to `process_incoming.sh`.

Pipeline bring-up
- [x] Integrate upgraded presence/classifier into worker: `classify_block()` in `main.cpp` with SNR/BW guardrails, per-band prior_boost, full feature pipeline.
- [ ] Implement FSK demod chain: DC removal, coarse FFT peak + PLL CFO, explicit `k/sps/BT` (requires HAVE_LIQUID).
- [ ] Implement PSK/QAM chain: symsync, Costas, carrier watchdog + re-init; fallback QPSK→BPSK on high phase error (requires HAVE_LIQUID).
- [ ] Implement OOK/AM envelope: MAD threshold; duty-cycle consistency to reject CW (requires HAVE_LIQUID).

Tuning & validation
- [ ] Run full SNR sweep (+10…-10 dB) on synthetic IQs; produce confusion matrix and BER/CRC stats.
- [ ] Guardrail tests: wrong band/RSYM/BW rejected; log reason.
- [ ] Throughput check: ≥100 frames/min (Brian), ≥20 frames/min (Ray).

Canary & promotion
- [x] `ops/canary.sh` committed with `--status`/`--promote`/`--rollback`/`--dry-run` actions.
- [ ] Automated promotion gate: script-driven Prometheus metric check against acceptance criteria.
- [ ] Promote when targets met; optionally relax SNR gate.

## Appendices (quick refs)
Build liquid-dsp (Ray):
  sudo apt install -y build-essential pkg-config libfftw3-dev autoconf automake libtool git
  git clone https://github.com/jgaeddert/liquid-dsp.git
  cd liquid-dsp
  ./bootstrap.sh && ./configure --enable-neon && make -j4
  sudo make install && sudo ldconfig
  pkg-config --cflags --libs liquid-dsp
  ls /usr/local/lib/pkgconfig/liquid-dsp.pc

Create package tarball (workstation):
  tar -czf process-worker-package.tar.gz process-worker.service hardening.conf override.conf processor.conf deploy.sh verify.sh transfer_manifest.json README.md
  sha256sum process-worker-package.tar.gz > process-worker-package.tar.gz.sha256

Atomic install (Brian — from ~/pw_package):
  sudo install -m 644 -o root -g root process-worker.service /etc/systemd/system/process-worker.service
  tmpdir=$(mktemp -d /tmp/pwdrop.$(date -u +%Y%m%dT%H%M%SZ).XXXX)
  sudo install -m 644 -o root -g root hardening.conf "$tmpdir"/hardening.conf
  sudo install -m 644 -o root -g root override.conf "$tmpdir"/override.conf
  sudo install -m 644 -o root -g root processor.conf "$tmpdir"/processor.conf
  sudo mv "$tmpdir" /etc/systemd/system/process-worker.service.d
  sudo systemctl daemon-reload
  sudo systemctl restart process-worker.service

Contact / escalation
- If liquid-dsp build errors on Ray: capture `./configure` output and tail of `make -j4`.
- If service fails on Brian: `systemctl status -l` and `journalctl -u process-worker.service -n 200`.

With the commit message: merge of expanded system plan