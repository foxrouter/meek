#!/usr/bin/env bash
# BEGIN HELP
# install.sh — One-shot installer for rf_adapt_intel (meek)
#
# Supports:
#   - Raspberry Pi OS Bookworm 64-bit (Debian 12 aarch64)  — Ray (edge SDR node)
#   - Ubuntu Server Noble 24.04 LTS (x86-64 or ARM64)      — Brian (decode-only node)
#
# Usage:
#   bash install.sh [OPTIONS]
#
# Options:
#   --all-decoders      Also install multimon-ng, rtl_433, and liquid-dsp
#   --no-sdr            Decode-only mode (Brian): skip RTL-SDR packages and udev
#                       rules; install the incoming-file processor service instead
#                       of process-worker. Use on nodes without attached SDR hardware.
#   --iq-dest <dest>    Enable automatic IQ transfer to Brian (Ray only). Sets
#                       IQ_DEST in /etc/rf_worker/iq-transfer.env and installs +
#                       enables iq-transfer-watcher.service.
#                       Example: --iq-dest rf_worker@brian.local:/var/lib/rf-adapt-intel/incoming/
#   --no-service        Build only; do not install or start any systemd service
#   --dry-run           Print actions without executing them
#   --help              Show this help message
#
# Examples:
#   sudo bash install.sh --all-decoders          # Ray (full install with decoders)
#   sudo bash install.sh --no-sdr                # Brian (decode-only)
#   sudo bash install.sh --no-sdr --all-decoders # Brian with optional decoders
#   sudo bash install.sh --iq-dest rf_worker@brian.local:/var/lib/rf-adapt-intel/incoming/
#                                                # Ray with automatic IQ transfer
# END HELP
set -euo pipefail

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
ALL_DECODERS=false
NO_SERVICE=false
NO_SDR=false
DRY_RUN=false
IQ_DEST=""
IQ_WATCHER_INSTALLED=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all-decoders) ALL_DECODERS=true ;;
    --no-service)   NO_SERVICE=true ;;
    --no-sdr)       NO_SDR=true ;;
    --dry-run)      DRY_RUN=true ;;
    --iq-dest)
      if [[ $# -lt 2 ]]; then
        echo "[ERROR] --iq-dest requires a destination argument." >&2
        echo "Run 'bash install.sh --help' for usage." >&2
        exit 1
      fi
      IQ_DEST="$2"
      # Reject newlines/CRs (would corrupt the EnvironmentFile) and leading '-'
      # (would be misinterpreted as an rsync option).
      if [[ "${IQ_DEST}" == *$'\n'* ]] || [[ "${IQ_DEST}" == *$'\r'* ]]; then
        echo "[ERROR] --iq-dest value must not contain newlines or carriage returns." >&2
        exit 1
      fi
      if [[ "${IQ_DEST}" == -* ]]; then
        echo "[ERROR] --iq-dest value must not start with '-'." >&2
        exit 1
      fi
      shift
      ;;
    --help|-h)
      sed -n '/^# BEGIN HELP/,/^# END HELP/{ /^# BEGIN HELP/d; /^# END HELP/d; s/^# \{0,1\}//; p }' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      echo "Run 'bash install.sh --help' for usage." >&2
      exit 1
      ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pinned liquid-dsp release tarball SHA256 (passed to ops/setup.sh via env).
# Recompute with: curl -fsSL <tarball-url> | sha256sum
# where <tarball-url> = https://github.com/jgaeddert/liquid-dsp/archive/refs/tags/<LIQUID_DSP_GIT_TAG>.tar.gz
# (see LIQUID_DSP_GIT_TAG in ops/setup.sh for the pinned version)
export LIQUID_DSP_SHA256="33c42ebc2e6088570421e282c6332e899705d42b4f73ebd1212e6a11da714dd4"

run() {
  if $DRY_RUN; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

log()  { echo ""; echo "==> $*"; }
info() { echo "    $*"; }
warn() { echo "[WARN] $*" >&2; }
fail() { echo "[ERROR] $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
detect_os() {
  if [[ ! -f /etc/os-release ]]; then
    fail "Cannot detect OS: /etc/os-release not found."
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  OS_ID="${ID:-unknown}"
  OS_VERSION_ID="${VERSION_ID:-unknown}"
  OS_CODENAME="${VERSION_CODENAME:-unknown}"
  OS_PRETTY="${PRETTY_NAME:-${ID} ${VERSION_ID}}"
  ARCH="$(uname -m)"

  echo "Detected OS : ${OS_PRETTY}"
  echo "Arch        : ${ARCH}"

  case "${OS_ID}" in
    debian|raspbian)
      if [[ "${OS_CODENAME}" == "bookworm" ]]; then
        PLATFORM="bookworm"
      else
        warn "Unsupported Debian/Raspbian version '${OS_CODENAME}'. Bookworm (12) is recommended."
        warn "Proceeding with generic Debian/Ubuntu package names — adjust if install fails."
        PLATFORM="bookworm"
      fi
      ;;
    ubuntu)
      if [[ "${OS_CODENAME}" == "noble" ]]; then
        PLATFORM="noble"
      elif [[ "${OS_CODENAME}" == "jammy" ]]; then
        warn "Ubuntu Jammy (22.04) detected. Noble (24.04) is preferred; proceeding anyway."
        PLATFORM="noble"
      else
        warn "Unsupported Ubuntu version '${OS_CODENAME}'. Noble (24.04) is preferred."
        warn "Proceeding with generic Ubuntu package names — adjust if install fails."
        PLATFORM="noble"
      fi
      ;;
    *)
      fail "Unsupported OS '${OS_ID}'. This installer supports Debian Bookworm and Ubuntu Noble."
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Dependency installation
# ---------------------------------------------------------------------------
install_deps() {
  log "Installing system dependencies for ${PLATFORM}..."

  # Runtime and tooling packages needed on both SDR and decode-only nodes
  local pkgs=(
    git
    python3
    python3-numpy
    inotify-tools
    rsync
  )

  if $NO_SDR; then
    info "Decode-only mode (--no-sdr): skipping build tools and RTL-SDR hardware packages."
  else
    # Build tools — only needed on nodes that compile rf_adapt_intel
    pkgs+=(build-essential cmake pkg-config libsqlite3-dev)
    # SoapySDR — only needed on SDR nodes (compile-time headers + runtime + utilities)
    pkgs+=(libsoapysdr-dev soapysdr-tools)
    # SDR hardware packages — only needed on nodes with an attached RTL-SDR dongle
    pkgs+=(rtl-sdr librtlsdr-dev)
    # SoapySDR RTL-SDR plugin name is the same on Bookworm and Noble
    pkgs+=(soapysdr-module-rtlsdr)
  fi

  run sudo apt-get update -qq
  run sudo apt-get install -y "${pkgs[@]}"
}

# ---------------------------------------------------------------------------
# udev rule for RTL-SDR (prevents dvb module from claiming the device)
# Skipped when --no-sdr is passed (Brian / decode-only nodes).
# ---------------------------------------------------------------------------
install_udev_rule() {
  if $NO_SDR; then
    info "Decode-only mode (--no-sdr): skipping udev rule and DVB blacklist setup."
    return 0
  fi

  local rule_file="/etc/udev/rules.d/99-rtlsdr.rules"
  local blacklist_file="/etc/modprobe.d/rtlsdr-blacklist.conf"

  if [[ -f "${rule_file}" ]] && ! $DRY_RUN; then
    info "udev rule already exists: ${rule_file}"
  else
    log "Installing RTL-SDR udev rule..."
    run sudo bash -c "cat > ${rule_file} <<'EOF'
# RTL-SDR USB dongle — grant plugdev group access
SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"0bda\", ATTRS{idProduct}==\"2838\", GROUP=\"plugdev\", MODE=\"0664\"
SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"0bda\", ATTRS{idProduct}==\"2832\", GROUP=\"plugdev\", MODE=\"0664\"
EOF"
    run sudo udevadm control --reload-rules
    run sudo udevadm trigger
    info "udev rule written to ${rule_file}"
  fi

  if [[ -f "${blacklist_file}" ]] && ! $DRY_RUN; then
    info "DVB blacklist already exists: ${blacklist_file}"
  else
    log "Blacklisting dvb_usb_rtl28xxu kernel module..."
    run sudo bash -c "echo 'blacklist dvb_usb_rtl28xxu' > ${blacklist_file}"
    run sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true
    info "Module blacklisted. Re-plug the RTL-SDR dongle after install completes."
  fi

  # Add the invoking user to plugdev group (no-op in dry-run)
  local real_user="${SUDO_USER:-$USER}"
  if id -nG "${real_user}" | grep -qw plugdev; then
    info "User ${real_user} is already in plugdev group."
  else
    log "Adding ${real_user} to plugdev group..."
    run sudo usermod -aG plugdev "${real_user}"
    warn "Group membership change requires a new login session to take effect."
    warn "After install, run:  newgrp plugdev  or log out and back in."
  fi
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
build_worker() {
  log "Building rf_adapt_intel..."
  local build_dir="${REPO_ROOT}/build"

  run mkdir -p "${build_dir}"
  run cmake -S "${REPO_ROOT}" -B "${build_dir}" -DCMAKE_BUILD_TYPE=Release
  run cmake --build "${build_dir}" -- -j"$(nproc)"

  info "Build complete."
}

install_binary() {
  log "Installing rf_adapt_intel to /usr/local/bin/..."
  run sudo cmake --install "${REPO_ROOT}/build"
  if ! $DRY_RUN; then
    info "Installed: $(which rf_adapt_intel 2>/dev/null || echo '/usr/local/bin/rf_adapt_intel')"
  fi
}

# ---------------------------------------------------------------------------
# Service account
# ---------------------------------------------------------------------------
create_service_user() {
  if id rf_worker &>/dev/null; then
    info "Service user rf_worker already exists."
  else
    log "Creating rf_worker service account..."
    run sudo useradd -r -s /sbin/nologin rf_worker
    info "User rf_worker created."
  fi
  if $NO_SDR; then
    # On decode-only nodes (Brian) the RTL-SDR dongle is not attached,
    # so plugdev membership is not needed.
    info "Decode-only mode: skipping plugdev group assignment for rf_worker."
  else
    # Ensure rf_worker is in the plugdev group so that the udev rule
    # (GROUP="plugdev", MODE="0664") grants it access to the RTL-SDR USB device.
    if ! id -nG rf_worker 2>/dev/null | grep -qw plugdev; then
      info "Adding rf_worker to plugdev group (required for RTL-SDR USB access)..."
      run sudo usermod -aG plugdev rf_worker
      info "rf_worker added to plugdev."
    else
      info "rf_worker is already in plugdev group."
    fi
  fi
}

# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------
install_config() {
  local conf_dir="/etc/rf_worker"
  local conf_file="${conf_dir}/thresholds.env"
  if [[ -f "${conf_file}" ]]; then
    info "Config already exists: ${conf_file}"
  else
    log "Installing default configuration..."
    run sudo mkdir -p "${conf_dir}"
    run sudo install -m 640 -o root -g root \
        "${REPO_ROOT}/config/thresholds.env.example" "${conf_file}"
    info "Config written to ${conf_file} — edit to customise thresholds."
  fi
}

# ---------------------------------------------------------------------------
# Install logrotate configuration for worker.log
# ---------------------------------------------------------------------------
install_logrotate() {
  local src="${REPO_ROOT}/config/logrotate.d/rf-adapt-intel"
  local dest="/etc/logrotate.d/rf-adapt-intel"
  if [[ -f "${src}" ]]; then
    log "Installing logrotate configuration..."
    run sudo install -m 644 -o root -g root "${src}" "${dest}"
    info "logrotate config written to ${dest}."
  else
    log "WARNING: logrotate config source not found at ${src}; skipping logrotate installation."
  fi
}

# ---------------------------------------------------------------------------
# Install shared scripts to /usr/local/share/rf-adapt-intel/scripts/
# (referenced by iq-transfer-watcher.service and rf-incoming-processor.service)
# ---------------------------------------------------------------------------
install_scripts() {
  local share_dir="/usr/local/share/rf-adapt-intel/scripts"
  log "Installing scripts to ${share_dir}..."
  run sudo mkdir -p "${share_dir}"
  for script in transfer_iq.sh process_incoming.sh scan_incoming.sh; do
    if [[ -f "${REPO_ROOT}/scripts/${script}" ]]; then
      run sudo install -m 755 -o root -g root \
          "${REPO_ROOT}/scripts/${script}" "${share_dir}/${script}"
    fi
  done
  info "Scripts installed."
}

# ---------------------------------------------------------------------------
# Install and enable the iq-transfer-watcher service (Ray only)
# Called when --iq-dest is supplied.
# ---------------------------------------------------------------------------
install_iq_transfer_watcher() {
  local iq_dest="$1"
  local conf_dir="/etc/rf_worker"
  local conf_file="${conf_dir}/iq-transfer.env"
  local unit_dir="/etc/systemd/system"
  local dropin_dir="${unit_dir}/iq-transfer-watcher.service.d"

  log "Installing iq-transfer-watcher service (IQ_DEST=${iq_dest})..."

  # Write /etc/rf_worker/iq-transfer.env with the provided destination.
  # Preserve an existing file; only overwrite when IQ_DEST differs.
  run sudo mkdir -p "${conf_dir}"
  if [[ -f "${conf_file}" ]] && ! $DRY_RUN; then
    local existing_dest
    existing_dest=$(grep -E '^IQ_DEST=' "${conf_file}" | head -1 | sed 's/^IQ_DEST=//' || true)
    if [[ "${existing_dest}" == "${iq_dest}" ]]; then
      info "Config already correct: ${conf_file}"
    else
      info "Updating IQ_DEST in ${conf_file} (was: ${existing_dest:-<unset>})"
      run sudo cp "${conf_file}" "${conf_file}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
      # Build updated config in a temp file: preserve all lines except IQ_DEST,
      # then append the new IQ_DEST. Trap ensures the temp file is removed on
      # early exit.
      local tmp_conf
      tmp_conf="$(mktemp /tmp/iq-transfer.env.XXXXXX)"
      trap 'rm -f "${tmp_conf}"' EXIT
      { grep -v '^IQ_DEST=' "${conf_file}" || true; printf 'IQ_DEST=%s\n' "${iq_dest}"; } \
        > "${tmp_conf}"
      run sudo install -m 640 -o root -g root "${tmp_conf}" "${conf_file}"
      rm -f "${tmp_conf}"
      trap - EXIT
    fi
  else
    # Create the config in a temp file so ownership and permissions are set
    # correctly via install(1). Trap ensures the temp file is removed on early exit.
    local tmp_conf
    tmp_conf="$(mktemp /tmp/iq-transfer.env.XXXXXX)"
    trap 'rm -f "${tmp_conf}"' EXIT
    cat > "${tmp_conf}" <<EOF
# iq-transfer.env — generated by install.sh
# Edit this file to override IQ transfer settings.
IQ_DEST=${iq_dest}
# IQ_BW_KBPS=2048
# IQ_MAX_RETRIES=3
# IQ_SOURCE_DIR=/var/lib/rf-adapt-intel/snapshots
EOF
    run sudo install -m 640 -o root -g root "${tmp_conf}" "${conf_file}"
    rm -f "${tmp_conf}"
    trap - EXIT
    info "Config written to ${conf_file}"
  fi

  # Install the service unit
  run sudo install -m 644 -o root -g root \
      "${REPO_ROOT}/systemd/iq-transfer-watcher.service" \
      "${unit_dir}/iq-transfer-watcher.service"

  # Install the hardening drop-in
  run sudo mkdir -p "${dropin_dir}"
  run sudo install -m 644 -o root -g root \
      "${REPO_ROOT}/systemd/iq-transfer-watcher.service.d/hardening.conf" \
      "${dropin_dir}/hardening.conf"

  run sudo systemctl daemon-reload
  run sudo systemctl enable --now iq-transfer-watcher

  IQ_WATCHER_INSTALLED=true
  info "iq-transfer-watcher enabled and started."
  info "IQ files in /var/lib/rf-adapt-intel/snapshots/ will be rsync'd to ${iq_dest}"
}

# ---------------------------------------------------------------------------
# Deploy systemd service (Ray — full SDR node)
# ---------------------------------------------------------------------------
deploy_service() {
  log "Deploying process-worker systemd service..."
  local deploy_args=()
  $DRY_RUN && deploy_args+=(--dry-run)
  run bash "${REPO_ROOT}/ops/deploy.sh" "${deploy_args[@]}"
}

# ---------------------------------------------------------------------------
# Deploy incoming-file processor service (Brian — decode-only node)
# ---------------------------------------------------------------------------
deploy_incoming_processor() {
  log "Deploying rf-incoming-processor (path + service) for decode-only node..."
  local unit_dir="/etc/systemd/system"
  local share_dir="/usr/local/share/rf-adapt-intel"

  # If process-worker was previously deployed (e.g. migrating from a full SDR
  # node to a decode-only node), stop and disable it so it does not continue
  # capturing on a node that has no attached SDR hardware.
  if command -v systemctl &>/dev/null; then
    if $DRY_RUN; then
      # Skip live probes in dry-run; just show the intended action.
      info "Stopping and disabling process-worker (not needed on decode-only nodes)..."
      run sudo systemctl disable --now process-worker
    elif systemctl is-enabled process-worker &>/dev/null || \
         systemctl is-active process-worker &>/dev/null; then
      info "Stopping and disabling process-worker (not needed on decode-only nodes)..."
      if ! sudo systemctl disable --now process-worker; then
        warn "Failed to disable/stop process-worker; it may still be running."
        warn "Please run 'systemctl status process-worker' and stop/disable it manually."
      fi
    fi
  fi

  # Install the path unit and service pair
  run sudo install -m 644 -o root -g root \
      "${REPO_ROOT}/systemd/rf-incoming-processor.path" \
      "${unit_dir}/rf-incoming-processor.path"
  run sudo install -m 644 -o root -g root \
      "${REPO_ROOT}/systemd/rf-incoming-processor.service" \
      "${unit_dir}/rf-incoming-processor.service"
  run sudo install -m 644 -o root -g root \
      "${REPO_ROOT}/systemd/rf-incoming-processor@.service" \
      "${unit_dir}/rf-incoming-processor@.service"

  # Create runtime data directories owned by rf_worker
  run sudo mkdir -p /var/lib/rf-adapt-intel/{incoming,processed,snapshots}
  run sudo chown -R rf_worker:rf_worker /var/lib/rf-adapt-intel
  run sudo chmod 0750 /var/lib/rf-adapt-intel
  run sudo chmod 0750 /var/lib/rf-adapt-intel/snapshots
  run sudo chmod 0750 /var/lib/rf-adapt-intel/incoming
  run sudo chmod 0750 /var/lib/rf-adapt-intel/processed

  # Install Python tools so decode_candidates.py is accessible
  run sudo mkdir -p "${share_dir}/tools"
  run sudo install -m 755 -o root -g root \
      "${REPO_ROOT}/tools/decode_candidates.py" \
      "${share_dir}/tools/decode_candidates.py"

  # numpy is already installed via the python3-numpy apt package above;
  # avoid pip3 here because Ubuntu Noble 24.04 rejects system-wide pip installs
  # (PEP 668 / externally-managed-environment).

  run sudo systemctl daemon-reload
  run sudo systemctl enable --now rf-incoming-processor.path

  info "rf-incoming-processor.path enabled."
  info "New *.cf32 / *.raw files in /var/lib/rf-adapt-intel/incoming/ will be processed automatically."
  info "Processed files are moved to /var/lib/rf-adapt-intel/processed/."
}

# ---------------------------------------------------------------------------
# Optional decoders
# ---------------------------------------------------------------------------
install_decoders() {
  log "Installing optional decoders (multimon-ng, rtl_433, liquid-dsp)..."
  local setup_args=(--non-interactive --install-multimon-ng --install-rtl_433 --install-liquid-dsp)
  $DRY_RUN && setup_args+=(--dry-run)
  run bash "${REPO_ROOT}/ops/setup.sh" "${setup_args[@]}"
}

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
verify_install() {
  log "Verifying installation..."
  if $DRY_RUN; then
    echo "[dry-run] Would run: bash ${REPO_ROOT}/ops/verify.sh"
    return
  fi
  bash "${REPO_ROOT}/ops/verify.sh" || true
}

# ---------------------------------------------------------------------------
# Print summary
# ---------------------------------------------------------------------------
print_summary() {
  echo ""
  echo "================================================================"
  echo "  rf_adapt_intel installation complete"
  echo "================================================================"
  echo ""
  if ! $NO_SERVICE; then
    if $NO_SDR; then
      echo "  Mode     : decode-only (Brian)"
      echo "  Path     : sudo systemctl status rf-incoming-processor.path"
      echo "  Logs     : sudo journalctl -u rf-incoming-processor -f"
      echo "  Incoming : /var/lib/rf-adapt-intel/incoming/"
      echo "  Processed: /var/lib/rf-adapt-intel/processed/"
    else
      echo "  Mode     : full SDR capture+classify (Ray)"
      echo "  Service  : sudo systemctl status process-worker"
      echo "  Logs     : sudo journalctl -u process-worker -f"
      echo "  Config   : /etc/rf_worker/thresholds.env"
      if $IQ_WATCHER_INSTALLED; then
        echo "  IQ Xfer  : sudo systemctl status iq-transfer-watcher"
      fi
    fi
  fi
  echo ""
  echo "  Next steps:"
  if $NO_SDR; then
    echo "    1. Edit /etc/rf_worker/thresholds.env to set classification thresholds."
    echo "    2. Configure ssh key trust from Ray so rsync can reach this node:"
    echo "       ssh-copy-id rf_worker@<this-host>"
    echo "    3. Check path unit: sudo systemctl status rf-incoming-processor.path"
  else
    echo "    1. Plug in your RTL-SDR dongle (if not already connected)."
    local real_user="${SUDO_USER:-$USER}"
    if ! id -nG "${real_user}" | grep -qw plugdev 2>/dev/null; then
      echo "    2. Run 'newgrp plugdev' or log out and back in for udev access."
    fi
    echo "    3. Check service logs: sudo journalctl -u process-worker -n 50 --no-pager"
    echo "    4. Auto-tune thresholds: sudo bash ops/autotune.sh"
    echo "       (or edit /etc/rf_worker/thresholds.env manually)"
    if $IQ_WATCHER_INSTALLED; then
      echo "    5. IQ transfer to Brian is enabled (iq-transfer-watcher.service)."
      echo "       Check status: sudo systemctl status iq-transfer-watcher"
      echo "       Config      : /etc/rf_worker/iq-transfer.env"
    elif [[ -z "${IQ_DEST}" ]]; then
      echo "    5. To enable automatic IQ transfer to Brian, re-run:"
      echo "       sudo bash install.sh --iq-dest rf_worker@brian.local:/var/lib/rf-adapt-intel/incoming/"
    fi
  fi
  echo ""
  echo "  See docs/INSTALL.md for full documentation and troubleshooting."
  echo "================================================================"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  echo "================================================================"
  echo "  rf_adapt_intel (meek) — automated installer"
  echo "================================================================"
  if $DRY_RUN; then
    echo "  DRY-RUN mode: no changes will be made."
  fi
  if $NO_SDR; then
    echo "  Mode: decode-only (--no-sdr) — Brian / no attached SDR hardware"
  fi
  if [[ -n "${IQ_DEST}" ]]; then
    echo "  IQ transfer: enabled (IQ_DEST=${IQ_DEST})"
  fi
  echo ""

  detect_os
  install_deps
  install_udev_rule

  if ! $NO_SDR; then
    build_worker
    install_binary
  fi

  if ! $NO_SERVICE; then
    create_service_user
    install_config
    install_logrotate
    install_scripts
    if $ALL_DECODERS; then
      install_decoders
    fi
    if $NO_SDR; then
      # Brian: install incoming-file processor service; no SDR daemon
      deploy_incoming_processor
    else
      # Ray: install full process-worker service
      deploy_service
      verify_install
    fi
    if [[ -n "${IQ_DEST}" ]]; then
      if $NO_SDR; then
        warn "--iq-dest is only meaningful on Ray (SDR) nodes; skipping iq-transfer-watcher setup."
      else
        install_iq_transfer_watcher "${IQ_DEST}"
      fi
    fi
  fi

  print_summary
}

main
