#!/usr/bin/env bash
# ops/autotune.sh — Automatically tune /etc/rf_worker/thresholds.env.
#
# Analyses IQ snapshot files in the configured snapshot directory (or falls
# back to synthetic reference signals) and writes data-driven threshold
# recommendations to the configuration file.
#
# Usage:
#   sudo bash ops/autotune.sh [OPTIONS]
#
# Options:
#   --snapshot-dir DIR   IQ snapshot directory (default: /var/lib/rf-adapt-intel/snapshots)
#   --conf FILE          Config file to update (default: /etc/rf_worker/thresholds.env)
#   --out FILE           Also save key=value lines to FILE
#   --no-restart         Do not restart process-worker after writing thresholds
#   --dry-run            Print recommended values without modifying any file
#   --verbose            Show per-metric percentile statistics
#   --help               Show this help message
#
# Examples:
#   # Tune from live snapshots and restart service:
#   sudo bash ops/autotune.sh
#
#   # Preview recommendations without writing:
#   bash ops/autotune.sh --dry-run
#
#   # Tune without restarting the service:
#   sudo bash ops/autotune.sh --no-restart
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SNAPSHOT_DIR="${RF_SNAPSHOT_DIR:-/var/lib/rf-adapt-intel/snapshots}"
CONF_FILE="/etc/rf_worker/thresholds.env"
OUT_FILE=""
NO_RESTART=false
DRY_RUN=false
VERBOSE=false
SERVICE="process-worker"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot-dir)
      shift
      SNAPSHOT_DIR="${1:?--snapshot-dir requires an argument}"
      ;;
    --conf)
      shift
      CONF_FILE="${1:?--conf requires an argument}"
      ;;
    --out)
      shift
      OUT_FILE="${1:?--out requires an argument}"
      ;;
    --no-restart)  NO_RESTART=true ;;
    --dry-run)     DRY_RUN=true ;;
    --verbose|-v)  VERBOSE=true ;;
    -h|--help)
      sed -n '2,/^set -/{ /^set -/d; s/^# \{0,1\}//; p }' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      echo "Run 'bash ops/autotune.sh --help' for usage." >&2
      exit 1
      ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "==> $*"; }
warn() { echo "[WARN] $*" >&2; }

run() {
  if $DRY_RUN; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

require_python3() {
  if ! command -v python3 &>/dev/null; then
    echo "[ERROR] python3 is required but not installed." >&2
    exit 1
  fi
  if ! python3 -c "import numpy" 2>/dev/null; then
    echo "[ERROR] Python package 'numpy' is required." >&2
    echo "        Install with: sudo apt-get install -y python3-numpy" >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  echo "================================================================"
  echo "  rf_adapt_intel — threshold auto-tuner"
  echo "================================================================"
  if $DRY_RUN; then
    echo "  DRY-RUN mode: no files will be modified."
  fi
  echo ""

  require_python3

  # Build python3 argument list
  local py_args=(
    "${REPO_ROOT}/tools/autotune_thresholds.py"
    --snapshot-dir "${SNAPSHOT_DIR}"
    --conf         "${CONF_FILE}"
  )

  if [[ -n "${OUT_FILE}" ]]; then
    py_args+=(--out "${OUT_FILE}")
  fi

  if $DRY_RUN; then
    py_args+=(--dry-run)
  else
    py_args+=(--write)
  fi

  if $VERBOSE; then
    py_args+=(--verbose)
  fi

  log "Running autotune_thresholds.py..."
  run python3 "${py_args[@]}"

  # Restart service unless --no-restart or --dry-run
  if ! $DRY_RUN && ! $NO_RESTART; then
    if systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
      log "Restarting ${SERVICE} to apply new thresholds..."
      run systemctl restart "${SERVICE}"
      echo "    Service restarted. Check logs:"
      echo "    sudo journalctl -u ${SERVICE} -n 30 --no-pager"
    else
      warn "Service ${SERVICE} is not running — skipping restart."
      warn "Start with: sudo systemctl start ${SERVICE}"
    fi
  fi

  echo ""
  echo "================================================================"
  echo "  Autotune complete."
  if ! $DRY_RUN; then
    echo "  Config : ${CONF_FILE}"
    echo "  Logs   : sudo journalctl -u ${SERVICE} -f"
  fi
  echo "================================================================"
}

main
