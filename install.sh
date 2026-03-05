#!/usr/bin/env bash
# BEGIN HELP
# install.sh — One-shot installer for rf_adapt_intel (meek)
#
# Supports:
#   - Raspberry Pi OS Bookworm 64-bit (Debian 12 aarch64)
#   - Ubuntu Server Noble 24.04 LTS (x86-64 or ARM64)
#
# Usage:
#   bash install.sh [OPTIONS]
#
# Options:
#   --all-decoders      Also install multimon-ng, rtl_433, and liquid-dsp
#   --no-service        Build only; do not install or start the systemd service
#   --dry-run           Print actions without executing them
#   --help              Show this help message
#
# Example (full install with all optional decoders):
#   sudo bash install.sh --all-decoders
# END HELP
set -euo pipefail

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
ALL_DECODERS=false
NO_SERVICE=false
DRY_RUN=false

for _arg in "$@"; do
  case "$_arg" in
    --all-decoders) ALL_DECODERS=true ;;
    --no-service)   NO_SERVICE=true ;;
    --dry-run)      DRY_RUN=true ;;
    --help|-h)
      sed -n '/^# BEGIN HELP/,/^# END HELP/{ /^# BEGIN HELP/d; /^# END HELP/d; s/^# \{0,1\}//; p }' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: ${_arg}" >&2
      echo "Run 'bash install.sh --help' for usage." >&2
      exit 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

  # Common packages for both platforms
  local pkgs=(
    git
    build-essential
    cmake
    pkg-config
    libsoapysdr-dev
    soapysdr-tools
    libsqlite3-dev
    python3
    python3-numpy
    inotify-tools
    rsync
    rtl-sdr
    librtlsdr-dev
  )

  # SoapySDR RTL-SDR plugin name differs between releases
  if [[ "${PLATFORM}" == "bookworm" ]]; then
    pkgs+=(soapysdr-module-rtlsdr)
  else
    # Noble ships the same name
    pkgs+=(soapysdr-module-rtlsdr)
  fi

  run sudo apt-get update -qq
  run sudo apt-get install -y "${pkgs[@]}"
}

# ---------------------------------------------------------------------------
# udev rule for RTL-SDR (prevents dvb module from claiming the device)
# ---------------------------------------------------------------------------
install_udev_rule() {
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

  # Add rf_worker to the plugdev group so the systemd service can access the
  # RTL-SDR USB device (the udev rule in 99-rtlsdr.rules grants plugdev access).
  if id -nG rf_worker 2>/dev/null | grep -qw plugdev; then
    info "rf_worker is already in plugdev group."
  else
    log "Adding rf_worker to plugdev group (required for RTL-SDR USB access)..."
    run sudo usermod -aG plugdev rf_worker
    info "rf_worker added to plugdev."
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
# Deploy systemd service
# ---------------------------------------------------------------------------
deploy_service() {
  log "Deploying process-worker systemd service..."
  local deploy_args=()
  $DRY_RUN && deploy_args+=(--dry-run)
  run bash "${REPO_ROOT}/ops/deploy.sh" "${deploy_args[@]}"
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
    echo "  Service  : sudo systemctl status process-worker"
    echo "  Logs     : sudo journalctl -u process-worker -f"
    echo "  Config   : /etc/rf_worker/thresholds.env"
  fi
  echo ""
  echo "  Next steps:"
  echo "    1. Plug in your RTL-SDR dongle (if not already connected)."
  local real_user="${SUDO_USER:-$USER}"
  if ! id -nG "${real_user}" | grep -qw plugdev 2>/dev/null; then
    echo "    2. Run 'newgrp plugdev' or log out and back in for udev access."
  fi
  echo "    3. Check service logs: sudo journalctl -u process-worker -n 50 --no-pager"
  echo "    4. Auto-tune thresholds: sudo bash ops/autotune.sh"
  echo "       (or edit /etc/rf_worker/thresholds.env manually)"
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
  echo ""

  detect_os
  install_deps
  install_udev_rule
  build_worker
  install_binary

  if ! $NO_SERVICE; then
    create_service_user
    install_config
    if $ALL_DECODERS; then
      install_decoders
    fi
    deploy_service
    verify_install
  fi

  print_summary
}

main
