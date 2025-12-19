Process-Worker Hardening & Modulation-Detection Plan (Aligned/Upgraded)
=======================================================================
Generated: 2025-12-17 (updated)
Author: foxrouter (via assistant)
Status: Active — classification prototype working; ops/deploy and demod pending

Purpose
-------
Replace naive amplitude thresholding with robust modulation detection/demodulation using liquid-dsp. Deploy hardened systemd drop-ins for process-worker.service and run a two-stage pipeline (presence → classification → modulation-specific demodulation). Use Ray (edge/RPi) for SDR/build/test; Brian (Ubuntu server) as central processing/canary host.

High-level sequence
-------------------
1. Prepare Ray (build liquid-dsp, create/generate IQ test vectors).
2. Prepare Brian (deploy hardened drop-ins, run process-worker.service in passive/canary mode).
3. Functional tests: send IQ vectors from Ray → Brian, collect telemetry.
4. Iterate and tune classifier/demod pipelines.
5. Canary active decoding, then full rollout when metrics meet acceptance criteria.
6. Keep rollback paths and backups.

Current status (delta)
----------------------
- Presence/classifier: ✅ heuristic prototype (peak/median presence, spectral entropy + peak_frac + PAPR heuristics). Synthetic fixtures classify tone→cw_like; OOK→am/ook_like; FSK2→fsk_like; BPSK/QPSK→psk/qam_like.
- Test fixtures: ✅ RRC-shaped generator in `gen_test_signals.py`.
- Service hardening/deploy: ⏳ pending in this session.
- liquid-dsp build (Ray): ⏳ pending.
- Demod pipelines (FSK/PSK/QAM): ⏳ not started.
- Telemetry/verify.sh/systemd drop-ins: ⏳ pending deployment.

Classifier / feature pipeline (upgraded)
----------------------------------------
- AGC sanity with watchdog; reject failed convergence.
- SNR gate >0 dB unless canary/benchmark.
- PAPR + spectral flatness sliding windows with p50/p90; burst-length hysteresis; time-occupancy for CW vs OOK.
- RRC consistency check; resample via `msresamp`.
- Confidence scoring/logistic per class; emit `confidence` + `decision_trace`.
- Band guardrails: reject demod if BW deviates >25% from expected per band.

Demod paths (liquid-dsp)
------------------------
- FSK: `fskdem` with explicit `k`/`sps`/`BT`; DC offset removal; coarse FFT peak + fine PLL for CFO.
- PSK/QAM: default `qpsk`, fallback `bpsk/8psk` on phase error; symsync timing; Costas loop; carrier recovery watchdog with re-init.
- OOK/AM: envelope detect with MAD-based adaptive threshold; duty-cycle consistency to avoid CW confusion.

Bands and defaults
------------------
Core narrowband ISM targets:
- 433 MHz ISM: RSYM=128k, FDEV=50 kHz
- 868/915 MHz ISM: RSYM=250k, FDEV=62.5 kHz
- Guardrails (all bands): reject demod if observed BW deviates >25% from expected; require SNR >0 dB unless canary mode.

Additional RTL-SDR v3 coverage profiles (for Skyscan 25–2000 MHz):
- 315 MHz ISM/TPMS/keyfobs (OOK/FSK): RSYM=20–50 ksym/s (start 32k); FDEV=12.5–25 kHz (start 16 kHz); OOK 2–8 ksym/s with envelope+MAD
- 137 MHz WX (NOAA/Meteor): FM wide; RSYM ~30–40 ksym/s; guardrail ~34–36 kHz BW
- 118–137 MHz Airband (AM): AM; classifier bypass; guardrail ~6–12 kHz BW
- 131.55 MHz ACARS: RSYM=2.4 ksym/s; FDEV≈1.2 kHz; BW ~5–8 kHz
- 150–174 MHz VHF (LMR/pager): RSYM=4.8–6.4 ksym/s; FDEV=2.4–3.2 kHz; BW ~8–16 kHz
- 162 MHz NOAA WX voice: NFM; classifier bypass
- 240–400 MHz UHF mil/air: AM/NFM; classifier bypass except presence
- 300–512 MHz UHF LMR/telemetry: RSYM=12.5–25 ksym/s; FDEV=6.25–12.5 kHz; BW 20–50 kHz
- 868 MHz EU ISM alt: RSYM=150k start; FDEV=37.5 kHz; BW 150–250 kHz
- 902–928 MHz ISM: RSYM=250k; FDEV=62.5 kHz; allow ±20%
- 960–1215 MHz (ADS-B 1090): PPM (special-case decoder)
- 1.2–1.3 GHz ham/telemetry: RSYM=200–400 ksym/s FSK; FDEV=50–100 kHz; BW 200–500 kHz

Receiver/front-end (RTL-SDR v3)
- Use 2.048–2.4 MSPS for narrow sweeps; 3.2–3.84 MSPS only if CPU allows.
- DC removal on; bias-T off unless LNA needed.
- For wide scans, step center freqs with overlap.

Operational guardrails
- SNR gate >0 dB unless canary.
- BW gate ±25%.
- Lock watchdog + CFO re-init; PSK/QAM downshift (QPSK→BPSK) on phase error.

Automation / scripts
--------------------
- `process_incoming.sh`: filename→band map; set env (BAND/RSYM/FDEV/MOD_HINT/SNR_MIN/PAPR_MAX/BW_TOL_PCT); log JSON with decision_trace/confidence/features to `/var/lib/rf_worker/worker.log`; outputs to `/var/lib/rf_worker/processed/<basename>.raw`.

Hardening (process-worker.service)
----------------------------------
- ProtectSystem=full, ProtectHome=yes, NoNewPrivileges=yes, PrivateTmp=yes, ProtectClock=yes, ProtectKernelLogs=yes
- SystemCallFilter=@system-service @chown @file-system
- MemoryDenyWriteExecute=yes
- CapabilityBoundingSet=~CAP_SYS_ADMIN CAP_SYS_MODULE
- RestrictSUIDSGID=yes, RestrictNamespaces=yes
- LimitNOFILE=4096, TasksMax=2048
- ReadWritePaths=/var/lib/rf_worker
- Drop-ins: hardening.conf (syscall/caps), override.conf (env/thresholds), processor.conf (threads/queues)
- Backups in /root with timestamps; hash-check on deploy.

Observability
-------------
- Heartbeat FIFO/file writing `ok <timestamp>`.
- Prometheus textfile `/var/lib/rf_worker/metrics.prom` with counters (frames, rejects, confidence averages).
- Logs: decision_trace, confidence, feature stats, rejection reasons.

Validation & benchmarks
-----------------------
- Matrix: bands above; mods BPSK/QPSK/8PSK, 2/4-FSK, OOK/ASK, CW; SNR +10/+3/0/-3/-6/-10 dB; payload 70–90 B + CRC32; symbol rates default ±20%.
- Acceptance: classifier ≥95% @ ≥0 dB; ≥85% @ -6 dB; FP <3% @ ≥0 dB; lock <50 ms PSK, <30 ms FSK; throughput ≥100 frames/min (Brian), ≥20 frames/min (Ray).
- Guardrail tests: wrong band/RSYM/BW reject; verify BER/CRC vs known payloads.

Rollout & rollback
------------------
- Rollout: start SNR_MIN=0; canary subset; promote when FP within target; relax SNR gate only if acceptable.
- Rollback: remove drop-ins, restore backups, daemon-reload/restart.

Tasks (execution-ready)
----------------------
Brian (ops/central)
- [ ] Deploy systemd unit + drop-ins; daemon-reload/restart.
- [ ] Run verify.sh; capture ProtectSystem/ProtectHome/ReadWritePaths/LimitNOFILE/TasksMax; status + journal tail.
- [ ] Ensure JSON logging with decision_trace/confidence/features/rejections.
- [ ] Add heartbeat + Prometheus textfile.
- [ ] Enforce SNR/BW guardrails; lock watchdog with CFO re-init.

Ray (edge)
- [ ] Build/install liquid-dsp (NEON if supported); verify pkg-config.
- [ ] Generate RRC-shaped vectors across mods/SNRs/bands; include 315/433/868/915 and a VHF sample set.
- [ ] Transfer IQs to Brian (scp/stream).

Pipeline bring-up
- [ ] Integrate upgraded presence/classifier into worker; enforce guardrails.
- [ ] Implement FSK demod (DC removal; FFT peak + PLL CFO; explicit k/sps/BT).
- [ ] Implement PSK/QAM (symsync, Costas, watchdog+reinit; QPSK→BPSK fallback).
- [ ] Implement OOK/AM (envelope + MAD threshold; duty-cycle consistency).

Tuning & validation
- [ ] Run SNR sweep (+10…-10 dB); confusion matrix; BER/CRC stats.
- [ ] Guardrail tests: wrong band/RSYM/BW reject with reason.
- [ ] Throughput ≥100 fpm (Brian); ≥20 fpm (Ray).

Canary & promotion
- [ ] Canary with SNR_MIN=0; monitor FP/FN, CPU/mem, lock-fail.
- [ ] Promote when targets met; optionally relax SNR gate.

Appendices (quick refs)
-----------------------
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
--------------------
- Build errors on Ray (liquid-dsp): collect ./configure output, make -j4 tail.
- Service fails on Brian: systemctl status -l and journalctl -u process-worker.service -n 200.