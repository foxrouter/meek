#!/usr/bin/env bash
# ops/setup.sh — Interactive setup for rf_adapt_intel optional decoders.
#
# Usage:
#   sudo bash ops/setup.sh [OPTIONS]
#
# Options:
#   --install-multimon-ng   Install multimon-ng (POCSAG/FLEX/OOK decoder)
#   --install-rtl_433       Install rtl_433 (OOK/ASK ISM-433 device decoder)
#   --install-liquid-dsp    Build and install liquid-dsp (advanced GMSK demod)
#   --non-interactive       Skip all prompts; only install decoders named by flags
#   --dry-run               Print actions without executing them
#
# Platform requirements:
#   - Linux (Raspberry Pi OS Bookworm 64-bit recommended)
#   - Minimum 4 GB RAM (liquid-dsp build requires ~300 MB)
#
# After setup, decoder paths are written to:
#   /etc/rf_worker/thresholds.env
# so that the process-worker systemd service picks them up via EnvironmentFile.
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
INSTALL_MULTIMON=false
INSTALL_RTL433=false
INSTALL_LIQUID=false
NON_INTERACTIVE=false
DRY_RUN=false

CONF_DIR="/etc/rf_worker"
CONF_FILE="${CONF_DIR}/thresholds.env"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-multimon-ng)  INSTALL_MULTIMON=true ;;
    --install-rtl_433)      INSTALL_RTL433=true ;;
    --install-liquid-dsp)   INSTALL_LIQUID=true ;;
    --non-interactive)      NON_INTERACTIVE=true ;;
    --dry-run)              DRY_RUN=true ;;
    -h|--help)
      sed -n '2,/^set -/{ /^set -/d; s/^# \{0,1\}//; p }' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      exit 1
      ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
run() {
  if $DRY_RUN; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

log()  { echo "==> $*"; }
warn() { echo "[WARN] $*" >&2; }

# Detect whether stdin is a TTY (non-interactive when piped / no terminal)
is_tty() { [[ -t 0 ]]; }

ask_yes_no() {
  # ask_yes_no "Question?" default_yes|default_no
  # Requires a TTY on stdin; guard is handled by interactive_select().
  if ! is_tty; then
    return 1
  fi
  local question="$1"
  local default="${2:-default_no}"
  local prompt
  if [[ "$default" == "default_yes" ]]; then
    prompt="[Y/n]"
  else
    prompt="[y/N]"
  fi
  local reply
  read -r -p "${question} ${prompt} " reply
  reply="${reply:-}"
  case "${reply,,}" in
    y|yes) return 0 ;;
    n|no)  return 1 ;;
    "")
      [[ "$default" == "default_yes" ]]
      ;;
    *)
      echo "Please answer y or n." >&2
      ask_yes_no "$question" "$default"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Platform checks
# ---------------------------------------------------------------------------
check_platform() {
  log "Checking platform..."
  local os
  os="$(uname -s)"
  if [[ "$os" != "Linux" ]]; then
    echo "[ERROR] Unsupported OS: ${os}. This installer requires Linux." >&2
    exit 1
  fi

  # RAM check: liquid-dsp needs ~300 MB free; warn if total < 4 GB
  local mem_kb
  mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)
  local mem_gb
  mem_gb=$(( mem_kb / 1024 / 1024 ))
  if [[ "$mem_gb" -lt 4 ]]; then
    warn "Total RAM is ~${mem_gb} GB. Recommended minimum is 4 GB (especially for --install-liquid-dsp)."
  else
    echo "  RAM: ~${mem_gb} GB — OK"
  fi

  # Architecture
  local arch
  arch="$(uname -m)"
  echo "  Arch: ${arch}"

  # OS release
  if [[ -f /etc/os-release ]]; then
    local pretty
    pretty=$(. /etc/os-release && echo "${PRETTY_NAME:-unknown}")
    echo "  OS  : ${pretty}"
  fi
}

# ---------------------------------------------------------------------------
# Interactive decoder selection (only when stdin is a TTY and --non-interactive
# was not given)
# ---------------------------------------------------------------------------
interactive_select() {
  if $NON_INTERACTIVE || ! is_tty; then
    return 0
  fi

  echo ""
  echo "=== Optional decoder installation ==="
  echo "The following optional decoders extend rf_adapt_intel's capabilities:"
  echo ""
  echo "  1. multimon-ng  — POCSAG / FLEX / OOK decoding (apt package)"
  echo "     Requires: ~15 MB disk. Adds: POCSAG pager & OOK protocol decoding."
  echo ""
  echo "  2. rtl_433      — OOK/ASK ISM-433 device packet decoding (apt package)"
  echo "     Requires: ~10 MB disk. Adds: 433 MHz sensor & remote-control decoding."
  echo ""
  echo "  3. liquid-dsp   — Advanced GMSK/PSK demodulation library (build from source)"
  echo "     Requires: ~300 MB disk + build tools, 4 GB RAM recommended."
  echo "     Adds: high-quality GMSK / PSK matched-filter demodulation."
  echo ""

  if ask_yes_no "Install multimon-ng?" default_no; then
    INSTALL_MULTIMON=true
  fi
  if ask_yes_no "Install rtl_433?" default_no; then
    INSTALL_RTL433=true
  fi
  if ask_yes_no "Install liquid-dsp (builds from source, takes a few minutes)?" default_no; then
    INSTALL_LIQUID=true
  fi
}

# ---------------------------------------------------------------------------
# Installer functions
# ---------------------------------------------------------------------------
install_multimon_ng() {
  log "Installing multimon-ng..."
  run sudo apt-get install -y multimon-ng
  local bin
  bin="$(command -v multimon-ng 2>/dev/null || echo '')"
  if [[ -n "$bin" ]]; then
    echo "  multimon-ng installed at: ${bin}"
    write_env_var "MULTIMON_NG_PATH" "${bin}"
  else
    warn "multimon-ng binary not found on PATH after install."
  fi
}

install_rtl_433() {
  log "Installing rtl_433..."
  run sudo apt-get install -y rtl-433
  local bin
  bin="$(command -v rtl_433 2>/dev/null || echo '')"
  if [[ -n "$bin" ]]; then
    echo "  rtl_433 installed at: ${bin}"
    write_env_var "RTL_433_PATH" "${bin}"
  else
    warn "rtl_433 binary not found on PATH after install."
  fi
}

install_liquid_dsp() {
  log "Installing liquid-dsp build dependencies..."
  run sudo apt-get install -y build-essential pkg-config libfftw3-dev autoconf automake libtool git

  local src_dir
  src_dir="$(mktemp -d /tmp/liquid-dsp-build.XXXXXX)"
  # shellcheck disable=SC2064
  trap "rm -rf '${src_dir}'" EXIT

  log "Cloning liquid-dsp into ${src_dir}..."
  run git clone --depth 1 https://github.com/jgaeddert/liquid-dsp.git "${src_dir}"

  log "Building liquid-dsp (this may take several minutes)..."
  run bash -c "cd '${src_dir}' && ./bootstrap.sh && ./configure && make -j\$(nproc)"
  run bash -c "cd '${src_dir}' && sudo make install"
  run sudo ldconfig

  # Confirm pkg-config visibility
  if pkg-config --exists liquid 2>/dev/null; then
    local ver
    ver="$(pkg-config --modversion liquid)"
    echo "  liquid-dsp installed, version: ${ver}"
    write_env_var "LIQUID_DSP_VERSION" "${ver}"
  else
    warn "liquid-dsp installed but pkg-config cannot find 'liquid'. You may need to set PKG_CONFIG_PATH=/usr/local/lib/pkgconfig."
  fi
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
write_env_var() {
  local key="$1"
  local value="$2"
  # Validate key is safe for sed (alphanumeric and underscore only)
  if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    warn "write_env_var: invalid key '${key}' — skipping."
    return 1
  fi
  if $DRY_RUN; then
    echo "[dry-run] Would write ${key}=${value} to ${CONF_FILE}"
    return
  fi
  sudo mkdir -p "${CONF_DIR}"
  # Ensure thresholds.env exists
  if [[ ! -f "${CONF_FILE}" ]]; then
    sudo install -m 640 -o root -g root "${REPO_ROOT}/config/thresholds.env.example" "${CONF_FILE}"
    echo "  Created ${CONF_FILE} from example template."
  fi
  # Remove any existing line for this key, then append
  sudo sed -i "/^${key}=/d" "${CONF_FILE}"
  echo "${key}=${value}" | sudo tee -a "${CONF_FILE}" > /dev/null
  echo "  Written: ${key}=${value} -> ${CONF_FILE}"
}

ensure_conf_file() {
  if $DRY_RUN; then
    echo "[dry-run] Would ensure ${CONF_FILE} exists."
    return
  fi
  if [[ ! -f "${CONF_FILE}" ]]; then
    sudo mkdir -p "${CONF_DIR}"
    sudo install -m 640 -o root -g root "${REPO_ROOT}/config/thresholds.env.example" "${CONF_FILE}"
    echo "  Created ${CONF_FILE} from example template."
  else
    echo "  Config already exists: ${CONF_FILE}"
  fi
}

# ---------------------------------------------------------------------------
# Self-test: confirm installed tools respond
# ---------------------------------------------------------------------------
run_decoder_tests() {
  if $DRY_RUN; then
    echo "[dry-run] Would run decoder smoke tests."
    return 0
  fi
  log "Running decoder smoke tests..."
  local failed=0

  if $INSTALL_MULTIMON; then
    if command -v multimon-ng &>/dev/null; then
      echo "  [PASS] multimon-ng: $(multimon-ng --help 2>&1 | head -1 || true)"
    else
      echo "  [FAIL] multimon-ng not found on PATH" >&2
      failed=$(( failed + 1 ))
    fi
  fi

  if $INSTALL_RTL433; then
    if command -v rtl_433 &>/dev/null; then
      echo "  [PASS] rtl_433: $(rtl_433 -V 2>&1 | head -1 || true)"
    else
      echo "  [FAIL] rtl_433 not found on PATH" >&2
      failed=$(( failed + 1 ))
    fi
  fi

  if $INSTALL_LIQUID; then
    if pkg-config --exists liquid 2>/dev/null; then
      echo "  [PASS] liquid-dsp: $(pkg-config --modversion liquid)"
    else
      echo "  [FAIL] liquid-dsp pkg-config entry not found" >&2
      failed=$(( failed + 1 ))
    fi
  fi

  if [[ $failed -eq 0 ]]; then
    echo "  All smoke tests passed."
  else
    echo "[WARN] ${failed} smoke test(s) failed. Check output above." >&2
  fi
  return $failed
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  echo "=== rf_adapt_intel optional decoder setup ==="

  check_platform
  interactive_select

  if ! $INSTALL_MULTIMON && ! $INSTALL_RTL433 && ! $INSTALL_LIQUID; then
    echo ""
    echo "No decoders selected. Nothing to install."
    echo "Re-run with --install-multimon-ng, --install-rtl_433, --install-liquid-dsp,"
    echo "or interactively (without --non-interactive) to select decoders."
    exit 0
  fi

  echo ""
  log "Updating apt package lists..."
  run sudo apt-get update -qq

  if $INSTALL_MULTIMON; then install_multimon_ng; fi
  if $INSTALL_RTL433;   then install_rtl_433;    fi
  if $INSTALL_LIQUID;   then install_liquid_dsp; fi

  ensure_conf_file
  echo ""
  run_decoder_tests
  echo ""
  echo "=== Setup complete ==="
  echo "Config written to: ${CONF_FILE}"
  echo "Re-run 'sudo bash ops/deploy.sh' to restart the service with the new configuration."
}

main
