# RF Process Worker (staging)

This repo will host the RF worker hardening and modulation-detection pipeline.

## Layout (proposed)
- `docs/rf-adapt-intel-plan.md` — main plan (aligned/upgraded)
- `systemd/` — unit + drop-ins
- `scripts/` — ingest/metrics helpers
- `config/thresholds.env.example` — overridable knobs
- `ops/` — deploy/verify helpers
- `tests/` — (to add) SNR sweep, confusion/BER harness

## Quickstart (local)
1) Copy `config/thresholds.env.example` to `/etc/rf_worker/thresholds.env` (or keep in `config/` but **git-ignored**).
2) Install systemd unit/drop-ins: `sudo bash ops/deploy.sh`
3) Run `ops/verify.sh` to confirm hardening.
4) Ingest a sample IQ: `scripts/process_incoming.sh /path/to/file.raw`
5) Metrics/heartbeat: `scripts/heartbeat_and_metrics.sh` (optional background service)

## Sensitive-data guidance
- Keep secrets out of git. Use `config/thresholds.env` (ignored) for local overrides.
- Run `gitleaks detect --source .` before pushing.