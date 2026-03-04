# RF Process Worker (staging)

This repo will host the RF worker hardening and modulation-detection pipeline.

## Layout (proposed)
- `docs/rf-adapt-intel-plan.md` — main plan (aligned/upgraded)
- `systemd/` — unit + drop-ins
- `scripts/` — ingest/metrics helpers
- `config/thresholds.env.example` — overridable knobs
- `ops/` — deploy/verify/setup helpers
- `tests/` — (to add) SNR sweep, confusion/BER harness

## Quickstart (local)
1) Copy `config/thresholds.env.example` to `/etc/rf_worker/thresholds.env` (or keep in `config/` but **git-ignored**).
2) *(Optional)* Install optional decoders: `sudo bash ops/setup.sh`
3) Install systemd unit/drop-ins: `sudo bash ops/deploy.sh`
4) Run `ops/verify.sh` to confirm hardening.
5) Ingest a sample IQ: `scripts/process_incoming.sh /path/to/file.raw`
6) Metrics/heartbeat: `scripts/heartbeat_and_metrics.sh` (optional background service)

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

## Sensitive-data guidance
- Keep secrets out of git. Use `config/thresholds.env` (ignored) for local overrides.
- Run `gitleaks detect --source .` before pushing.