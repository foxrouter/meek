# Installation Guide — rf_adapt_intel (`meek`)

Step-by-step instructions for installing and running `rf_adapt_intel` on the two
preferred platforms:

- **Raspberry Pi OS Bookworm 64-bit** (Raspberry Pi 3B+ / 4 / 5) — **Ray** (edge SDR node)
- **Ubuntu Server Noble 24.04 LTS** (x86-64 or ARM64) — **Brian** (decode-only node)

---

## Table of contents

1. [Hardware requirements](#1-hardware-requirements)
2. [Raspberry Pi OS Bookworm 64-bit](#2-raspberry-pi-os-bookworm-64-bit)
3. [Ubuntu Server Noble 24.04](#3-ubuntu-server-noble-2404)
4. [Build the worker](#4-build-the-worker)
5. [Deploy the systemd service](#5-deploy-the-systemd-service)
6. [Optional decoders](#6-optional-decoders)
7. [Configuration](#7-configuration)
8. [Verify the installation](#8-verify-the-installation)
9. [Two-node deployment (Ray + Brian)](#9-two-node-deployment-ray--brian)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Hardware requirements

| Item | Minimum | Notes |
|---|---|---|
| SDR receiver | RTL-SDR v3 (RTL2832U) | Any SoapySDR-compatible device works |
| USB port | USB 2.0 | The RTL-SDR dongle draws ~250 mA |
| RAM | 1 GB (Raspberry Pi) / 2 GB (server) | 4 GB recommended if building liquid-dsp from source |
| Storage | 4 GB free | IQ snapshot files can grow quickly |
| OS | 64-bit kernel | `uname -m` must return `aarch64` or `x86_64` |

### Plug in the RTL-SDR dongle

Connect the RTL-SDR USB dongle **before** installing the driver.  If the dongle
LED lights up and `lsusb` shows a Realtek device, the kernel module has loaded.

```bash
lsusb | grep -i realtek
# Example: Bus 001 Device 004: ID 0bda:2838 Realtek Semiconductor Corp.
```

---

## 2. Raspberry Pi OS Bookworm 64-bit

> Tested on: Raspberry Pi OS Bookworm (Debian 12) — 64-bit, kernel 6.x.

### 2.1 Update the system

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

### 2.2 Install build dependencies

```bash
sudo apt install -y \
    git build-essential cmake pkg-config \
    libsoapysdr-dev soapysdr-tools \
    libsqlite3-dev \
    python3 python3-numpy \
    inotify-tools rsync
```

### 2.3 Install RTL-SDR driver (SoapySDR plugin)

```bash
sudo apt install -y rtl-sdr librtlsdr-dev soapysdr-module-rtlsdr
```

Confirm SoapySDR can see the dongle:

```bash
SoapySDRUtil --probe
# Should list a device section for RTL-SDR
```

If you see `"usb_claim_interface error -6"`, add the udev rule:

```bash
sudo bash -c 'echo "SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"0bda\", ATTRS{idProduct}==\"2838\", GROUP=\"plugdev\", MODE=\"0664\"" \
    > /etc/udev/rules.d/99-rtlsdr.rules'
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev "$USER"
# Re-login or run: newgrp plugdev
```

### 2.4 Clone the repository

```bash
git clone https://github.com/foxrouter/meek.git
cd meek
```

### 2.5 (Optional) Install inotify-tools for IQ file watching

```bash
sudo apt install -y inotify-tools
```

---

## 3. Ubuntu Server Noble 24.04

> Tested on: Ubuntu Server 24.04 LTS (Noble Numbat) — x86-64 and ARM64.

### 3.1 Update the system

```bash
sudo apt update && sudo apt upgrade -y
```

### 3.2 Install build dependencies

```bash
sudo apt install -y \
    git build-essential cmake pkg-config \
    libsoapysdr-dev soapysdr-tools \
    libsqlite3-dev \
    python3 python3-numpy \
    inotify-tools rsync
```

### 3.3 Install RTL-SDR driver (SoapySDR plugin)

```bash
sudo apt install -y rtl-sdr librtlsdr-dev soapysdr-module-rtlsdr
```

Confirm SoapySDR can see the dongle:

```bash
SoapySDRUtil --probe
```

If `SoapySDRUtil` is not found, it may be in `soapysdr-tools`:

```bash
sudo apt install -y soapysdr-tools
```

### 3.4 Clone the repository

```bash
git clone https://github.com/foxrouter/meek.git
cd meek
```

---

## 4. Build the worker

Run these commands from the repository root (`meek/`):

```bash
mkdir build && cd build
cmake -S .. -B . -DCMAKE_BUILD_TYPE=Release
cmake --build . -- -j$(nproc)
```

Expected output ends with something like:

```
[100%] Linking CXX executable rf_adapt_intel
[100%] Built target rf_adapt_intel
```

Install the binary to `/usr/local/bin/`:

```bash
sudo cmake --install .
```

Verify:

```bash
rf_adapt_intel --help 2>&1 | head -5 || echo "binary installed"
which rf_adapt_intel
# /usr/local/bin/rf_adapt_intel
```

### Build without SDR hardware (`BUILD_HARDWARE_TARGETS=OFF`)

On machines without SoapySDR (e.g. a CI server or a laptop), you can still
build the standalone `iq_metrics` tool and run all Python tests:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_HARDWARE_TARGETS=OFF
cmake --build build -t iq_metrics
```

### Automated build via `scripts/deploy_and_restart.sh`

If the service is already deployed and you want to rebuild and restart in one step:

```bash
sudo bash scripts/deploy_and_restart.sh
```

---

## 5. Deploy the systemd service

This installs the hardened `process-worker.service` unit and all drop-ins.

```bash
sudo bash ops/deploy.sh
```

What `ops/deploy.sh` does:

1. Backs up any existing `process-worker.service` to `/root/`.
2. Installs the unit file and drop-in configuration.
3. Creates `/var/lib/rf-adapt-intel/{snapshots,incoming,processed}`.
4. Creates the `rf_worker` system account if it does not exist.
5. Installs and enables the canary monitor timer.
6. Runs `systemctl daemon-reload` and starts the service.

### Create the `rf_worker` service account

`ops/deploy.sh` automatically creates the `rf_worker` system account if it is
missing.  To create it manually before running the deploy script:

```bash
sudo useradd -r -s /sbin/nologin rf_worker
```

### Check service status

```bash
sudo systemctl status process-worker
sudo journalctl -u process-worker -n 50 --no-pager
```

### Dry-run (preview changes without making them)

```bash
sudo bash ops/deploy.sh --dry-run
```

---

## 6. Optional decoders

`ops/setup.sh` installs optional decoders that extend the classification pipeline.

```bash
# Interactive mode (prompts for each decoder)
sudo bash ops/setup.sh

# Non-interactive: install specific decoders
sudo bash ops/setup.sh --non-interactive --install-multimon-ng --install-rtl_433

# Install all decoders
sudo bash ops/setup.sh --non-interactive \
    --install-multimon-ng --install-rtl_433 --install-liquid-dsp

# Preview (dry-run)
sudo bash ops/setup.sh --non-interactive --install-liquid-dsp --dry-run
```

| Decoder | What it adds | RAM needed |
|---|---|---|
| **multimon-ng** | POCSAG / FLEX / OOK protocol decoding | ~15 MB disk |
| **rtl_433** | 433 MHz sensor and remote-control packets | ~10 MB disk |
| **liquid-dsp** | Advanced GMSK / PSK demodulation (build from source) | 4 GB RAM recommended |

> **Note on liquid-dsp build time:**
> - Raspberry Pi 4 (4 GB): ~8 minutes
> - Pi 5: ~3 minutes
> - Ubuntu server (4-core VM): ~2 minutes

Decoder paths are automatically written to `/etc/rf_worker/thresholds.env` so
the service picks them up on next restart.

---

## 7. Configuration

### 7.1 Create the runtime config file

```bash
sudo mkdir -p /etc/rf_worker
sudo cp config/thresholds.env.example /etc/rf_worker/thresholds.env
sudo chmod 640 /etc/rf_worker/thresholds.env
```

Edit it with your preferred editor:

```bash
sudo nano /etc/rf_worker/thresholds.env
```

Key settings to review:

| Variable | Default | Description |
|---|---|---|
| `RF_BLOCK_LEN` | `4096` | Samples per SDR read call |
| `RF_CONF_THRESHOLD` | `0.6` | Min confidence to write to database |
| `RF_SNR_MIN_DB` | `0.0` | Minimum SNR gate (dB); set negative for passive mode |
| `RF_SNAPSHOT_DIR` | `/var/lib/rf-adapt-intel/snapshots` | IQ snapshot output directory |
| `RF_SNAPSHOT_RETENTION_DAYS` | `0` | Days to keep IQ snapshots; `0` = keep forever |
| `RF_WORKER_LOG` | `/var/lib/rf-adapt-intel/worker.log` | JSON log file |

### 7.2 Restart after config changes

```bash
sudo systemctl restart process-worker
```

---

## 8. Verify the installation

Run the full hardening verification script:

```bash
sudo bash ops/verify.sh
```

This checks:
- `ProtectSystem`, `ProtectHome`, `ReadWritePaths` are set correctly.
- `LimitNOFILE` and `TasksMax` are within limits.
- The `process-worker` service is active.

### Run the test suite

```bash
# Python tests (no SDR hardware required)
python3 tests/test_guardrails.py -v
python3 tests/test_snr_sweep.py -v
python3 tests/test_demod_ber.py -v

# Validate C++ iq_metrics output against the Python reference
cmake -S . -B build -DBUILD_HARDWARE_TARGETS=OFF && cmake --build build -t iq_metrics
python3 tests/test_iq_metrics.py build/iq_metrics -v

# Shell tests (ops/setup.sh behaviour)
bash tests/test_setup.sh -v

# All via CTest
cmake --build build --target test
```

---

## 9. Two-node deployment (Ray + Brian)

This section covers a split-node setup where:

- **Ray** — Raspberry Pi 4 (64-bit Bookworm). Has the RTL-SDR dongle. Captures IQ
  data and classifies modulation via `process-worker.service`. Writes `.cf32` snapshots
  to `/var/lib/rf-adapt-intel/snapshots/` and rsync-transfers them to Brian.
- **Brian** — Ubuntu Server. No SDR hardware. Receives IQ files from Ray, runs offline
  decode analysis via `tools/decode_candidates.py`, and writes audit reports to
  `/var/lib/rf-adapt-intel/processed/`.

### 9.1 Ray setup

Run the standard installer as usual:

```bash
git clone https://github.com/foxrouter/meek.git && cd meek
sudo bash install.sh --all-decoders   # or without --all-decoders
```

Then configure and enable the IQ transfer watcher so snapshots are automatically
sent to Brian as they are written:

```bash
# Copy the example config and set the destination
sudo cp config/iq-transfer.env.example /etc/rf_worker/iq-transfer.env
sudo nano /etc/rf_worker/iq-transfer.env
# Set: IQ_DEST=rf_worker@brian.local:/var/lib/rf-adapt-intel/incoming/

# Install the watcher service and its hardening drop-in
sudo install -m 644 systemd/iq-transfer-watcher.service \
    /etc/systemd/system/iq-transfer-watcher.service
sudo mkdir -p /etc/systemd/system/iq-transfer-watcher.service.d
sudo install -m 644 systemd/iq-transfer-watcher.service.d/hardening.conf \
    /etc/systemd/system/iq-transfer-watcher.service.d/hardening.conf

# Install scripts to the shared directory
sudo mkdir -p /usr/local/share/rf-adapt-intel/scripts
sudo install -m 755 scripts/transfer_iq.sh \
    /usr/local/share/rf-adapt-intel/scripts/transfer_iq.sh

sudo systemctl daemon-reload
sudo systemctl enable --now iq-transfer-watcher
sudo systemctl status iq-transfer-watcher
```

The `iq-transfer-watcher` service is bound to `process-worker` — it starts and
stops together with the capture daemon.

### 9.2 Brian setup

Run the installer with `--no-sdr` to skip RTL-SDR packages and udev rules:

```bash
git clone https://github.com/foxrouter/meek.git && cd meek
sudo bash install.sh --no-sdr
# With optional decoders (multimon-ng, rtl_433, liquid-dsp):
# sudo bash install.sh --no-sdr --all-decoders
```

What `--no-sdr` does differently from the default:

| Action | Default (Ray) | `--no-sdr` (Brian) |
|---|---|---|
| Install `librtlsdr-dev`, `soapysdr-module-rtlsdr` | ✓ | ✗ |
| Set up udev rules + DVB blacklist | ✓ | ✗ |
| Add `rf_worker` to `plugdev` group | ✓ | ✗ |
| Build and install `rf_adapt_intel` binary | ✓ | ✗ |
| Enable `process-worker.service` | ✓ | ✗ |
| Enable `rf-incoming-processor.path` | ✗ | ✓ |

After installation, verify the path unit is watching for incoming files:

```bash
sudo systemctl status rf-incoming-processor.path
# Logs from each file processed:
sudo journalctl -u rf-incoming-processor -f
```

### 9.3 Configure SSH key trust (Ray → Brian)

The IQ transfer uses rsync over SSH.  Ray's `rf_worker` user must be able to
reach Brian's `rf_worker` account without a password:

**On Ray:**

```bash
# Generate a key for rf_worker (if not already present)
sudo -u rf_worker ssh-keygen -t ed25519 -f /var/lib/rf-adapt-intel/.ssh/id_ed25519 -N ''

# Copy the public key to Brian
sudo -u rf_worker ssh-copy-id -i /var/lib/rf-adapt-intel/.ssh/id_ed25519.pub \
    rf_worker@brian.local
```

**On Brian:** ensure `rf_worker`'s `~/.ssh/authorized_keys` contains Ray's public key
and that `sshd` is running.

Test the connection from Ray:

```bash
sudo -u rf_worker rsync --dry-run \
    /var/lib/rf-adapt-intel/snapshots/ \
    rf_worker@brian.local:/var/lib/rf-adapt-intel/incoming/
```

### 9.4 Verify end-to-end flow

1. On Ray, check that snapshots are being sent:

   ```bash
   sudo journalctl -u iq-transfer-watcher -f
   # Should show: "OK transferred: <filename>"
   ```

2. On Brian, check that files are processed:

   ```bash
   ls /var/lib/rf-adapt-intel/processed/
   sudo journalctl -u rf-incoming-processor -f
   # Should show: "[scan_incoming] OK: <filename>"
   ```

3. Audit JSON reports appear alongside the processed files:

   ```bash
   ls /var/lib/rf-adapt-intel/processed/*.json
   ```

---

## 10. Troubleshooting

### "SoapySDR not found" during cmake

```
FATAL_ERROR: SoapySDR not found via pkg-config.
```

Fix:

```bash
sudo apt install -y libsoapysdr-dev pkg-config
# On Noble you may also need the runtime library — check the available name:
apt-cache search libsoapysdr
# Install whichever version is listed, e.g.:
sudo apt install -y libsoapysdr0.8
```

Confirm pkg-config can see SoapySDR:

```bash
pkg-config --modversion SoapySDR
```

---

### "usb_claim_interface error" with RTL-SDR

The kernel `dvb_usb_rtl28xxu` module claims the device before SoapySDR can.
Blacklist it:

```bash
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/rtlsdr-blacklist.conf
sudo modprobe -r dvb_usb_rtl28xxu
```

Unplug and re-plug the dongle, then re-run `SoapySDRUtil --probe`.

---

### Service fails to start

Check the journal:

```bash
sudo journalctl -u process-worker -n 100 --no-pager
```

Common causes:

| Symptom | Fix |
|---|---|
| `rf_worker: no such user` | `sudo useradd -r -s /sbin/nologin rf_worker` |
| `rf_adapt_intel: not found` | Rebuild and run `sudo cmake --install build` |
| `Permission denied` on `/var/lib/rf-adapt-intel` | `sudo chown -R rf_worker:rf_worker /var/lib/rf-adapt-intel` |
| `SoapySDR: no devices found` | Check dongle is plugged in; verify with `SoapySDRUtil --probe` |
| `EnvironmentFile not found` | Copy config: `sudo cp config/thresholds.env.example /etc/rf_worker/thresholds.env` |

---

### liquid-dsp build fails on Raspberry Pi

If `./configure` errors with missing `fftw3`:

```bash
sudo apt install -y libfftw3-dev
```

If `make` runs out of memory on a Pi with < 4 GB RAM, reduce parallelism:

```bash
# In ops/setup.sh --install-liquid-dsp the build uses nproc; override manually:
cd /tmp/liquid-dsp-build.*/
make -j1
sudo make install
sudo ldconfig
```

---

### "pkg-config cannot find liquid" after install

`ops/setup.sh --install-liquid-dsp` handles this automatically on Ubuntu/Debian:

1. If `make install` did not create `/usr/local/lib/pkgconfig/liquid.pc`, the
   script synthesises one from the installed header/library.
2. `/etc/profile.d/liquid-dsp.sh` is written so every new login shell has the
   correct `PKG_CONFIG_PATH`.
3. `PKG_CONFIG_PATH` is also exported in the current session so the smoke tests
   run immediately without requiring a re-login.

If you installed liquid-dsp **manually** (outside `ops/setup.sh`) and still see
the warning, run the one-time fix by hand:

```bash
export PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:$PKG_CONFIG_PATH
pkg-config --modversion liquid
```

To make this permanent, create `/etc/profile.d/liquid-dsp.sh`:

```bash
echo 'export PKG_CONFIG_PATH="/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"' \
  | sudo tee /etc/profile.d/liquid-dsp.sh
sudo chmod 644 /etc/profile.d/liquid-dsp.sh
```

---

### Canary / rollback

If the service is misbehaving after an update:

```bash
# Roll back to the last backed-up config
sudo bash ops/canary.sh --rollback

# Or restore manually from /root/process-worker.service.bak.*
sudo cp /root/process-worker.service.bak.<timestamp> /etc/systemd/system/process-worker.service
sudo systemctl daemon-reload && sudo systemctl restart process-worker
```

---

## Quick-reference cheat sheet

```bash
# Full install from scratch (Bookworm or Noble)
git clone https://github.com/foxrouter/meek.git && cd meek
bash install.sh          # automated installer — detects OS, builds, deploys

# Manual steps
sudo apt install -y build-essential cmake pkg-config libsoapysdr-dev libsqlite3-dev python3 python3-numpy
mkdir build && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -- -j$(nproc)
sudo cmake --install build
sudo useradd -r -s /sbin/nologin rf_worker
sudo bash ops/deploy.sh
sudo bash ops/verify.sh

# Build iq_metrics only (no SDR hardware required)
cmake -S . -B build -DBUILD_HARDWARE_TARGETS=OFF && cmake --build build -t iq_metrics

# Container build (builds iq_metrics + runs all tests inside Docker)
docker build -t rf-adapt-intel:latest .
docker run --rm rf-adapt-intel:latest ctest --test-dir /build -V
```

See the main [README](../README.md) for full operational documentation.
