#!/usr/bin/env bash
# ops/canary.sh — Canary procedure for rf_adapt_intel (process-worker service).
#
# Usage:
#   sudo bash ops/canary.sh [--promote | --rollback | --status] [--dry-run]
#
# Phases:
#   (default)   Enable canary mode: set RF_SNR_MIN_DB=0 and deploy drop-in.
#   --status    Show current FP/FN counters, CPU/memory, and lock-fail metrics.
#   --promote   Promote canary config to production (remove canary drop-in).
#   --rollback  Restore production config from backup and reload service.
#
# Acceptance criteria checked automatically during --promote:
#   - False-positive/rejection rate < 3 % over the monitoring window.
#   - CPU usage < 80 % on target host.
#   Promotion is fully automated: no manual confirmation required.
#   Classify >= 95 % accuracy at >= 0 dB SNR is aspirational and tracked
#   via tests/test_snr_sweep.py; it is not polled from Prometheus directly.
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SERVICE="process-worker"
CONF_DIR="/etc/rf_worker"
CONF_FILE="${CONF_DIR}/thresholds.env"
DROP_IN_DIR="/etc/systemd/system/${SERVICE}.service.d"
CANARY_DROP_IN="${DROP_IN_DIR}/canary.conf"
METRICS_FILE="${RF_METRICS_FILE:-/var/lib/rf-adapt-intel/metrics.prom}"
WORKER_LOG="${RF_WORKER_LOG:-/var/lib/rf-adapt-intel/worker.log}"
BACKUP_DIR="/root/rf_adapt_intel_backup"
DRY_RUN=false
ACTION="canary"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --promote)   ACTION="promote" ;;
    --rollback)  ACTION="rollback" ;;
    --status)    ACTION="status" ;;
    --dry-run)   DRY_RUN=true ;;
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
log()  { echo "[canary] $*"; }
warn() { echo "[canary] WARN: $*" >&2; }

run() {
  if $DRY_RUN; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "[ERROR] This script must be run as root (sudo)." >&2
    exit 1
  fi
}

service_reload() {
  run systemctl daemon-reload
  run systemctl restart "${SERVICE}"
  log "Service ${SERVICE} reloaded."
}

# ---------------------------------------------------------------------------
# Read a metric value from the Prometheus textfile.
# Usage: get_metric rf_frames_total
# ---------------------------------------------------------------------------
get_metric() {
  local metric="$1"
  if [[ ! -f "${METRICS_FILE}" ]]; then
    echo "N/A"
    return
  fi
  grep -E "^${metric}[{ ]" "${METRICS_FILE}" 2>/dev/null \
    | tail -1 | awk '{print $NF}' || echo "N/A"
}

# ---------------------------------------------------------------------------
# ACTION: status — print current FP/FN rates and resource usage
# ---------------------------------------------------------------------------
do_status() {
  log "=== Canary Status ==="
  echo ""
  echo "--- Prometheus metrics (${METRICS_FILE}) ---"
  if [[ -f "${METRICS_FILE}" ]]; then
    local total rejected candidates
    total="$(get_metric rf_frames_total)"
    rejected="$(get_metric rf_frames_rejected)"
    candidates="$(get_metric rf_frames_candidate)"
    echo "  rf_frames_total:     ${total}"
    echo "  rf_frames_rejected:  ${rejected}"
    echo "  rf_frames_candidate: ${candidates}"
    # Compute FP rate proxy (rejected / total)
    if [[ "${total}" =~ ^[0-9]+$ ]] && [[ "${total}" -gt 0 ]]; then
      local fp_pct
      fp_pct=$(awk "BEGIN { printf \"%.1f\", ${rejected} / ${total} * 100 }")
      echo "  Rejection rate: ${fp_pct}% (target < 3% for canary)"
    fi
    echo ""
    echo "  Per-class frames:"
    grep 'rf_class_frames' "${METRICS_FILE}" | sed 's/^/    /'
  else
    warn "Metrics file not found: ${METRICS_FILE}"
  fi

  echo ""
  echo "--- CPU / Memory ---"
  if systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
    local pid
    pid="$(systemctl show -p MainPID --value "${SERVICE}" 2>/dev/null || echo 0)"
    if [[ "${pid}" -gt 0 ]]; then
      echo "  PID: ${pid}"
      ps -p "${pid}" -o %cpu,%mem,vsz,rss --no-headers 2>/dev/null \
        | awk '{printf "  CPU: %s%%  MEM: %s%%  VSZ: %s kB  RSS: %s kB\n",$1,$2,$3,$4}' \
        || echo "  (ps failed)"
    fi
  else
    warn "Service ${SERVICE} is not active."
  fi

  echo ""
  echo "--- Last 5 worker.log entries ---"
  tail -n 5 "${WORKER_LOG}" 2>/dev/null | sed 's/^/  /' || true

  echo ""
  echo "--- Promotion criteria (checked automatically by --promote) ---"
  check_promotion_criteria || true
  echo ""
  echo "  Note: 'Classifier accuracy >= 95% at >= 0 dB SNR' is tracked via"
  echo "  tests/test_snr_sweep.py and is not polled from Prometheus."
  echo "  Run: python3 tests/test_snr_sweep.py -v"
}

# ---------------------------------------------------------------------------
# ACTION: canary — enable canary mode (RF_SNR_MIN_DB=0)
# ---------------------------------------------------------------------------
do_canary() {
  require_root
  log "Enabling canary mode on service ${SERVICE}..."

  # Backup current config before modifying
  if [[ -f "${CONF_FILE}" ]] && ! $DRY_RUN; then
    run mkdir -p "${BACKUP_DIR}"
    local stamp
    stamp="$(date '+%Y%m%d_%H%M%S')"
    run cp "${CONF_FILE}" "${BACKUP_DIR}/thresholds.env.bak.${stamp}"
    log "Config backed up to ${BACKUP_DIR}/thresholds.env.bak.${stamp}"
  fi

  # Write canary drop-in: override RF_SNR_MIN_DB to 0 (passive capture).
  # Use atomic write (tmp file + mv) to prevent partial writes on interrupt.
  run mkdir -p "${DROP_IN_DIR}"
  if $DRY_RUN; then
    echo "[dry-run] Would write ${CANARY_DROP_IN}:"
    echo "  [Service]"
    echo "  Environment=RF_SNR_MIN_DB=0"
  else
    local tmp_dropin
    tmp_dropin="$(mktemp "${DROP_IN_DIR}/.canary_XXXXXX.conf")"
    cat > "${tmp_dropin}" << 'EOF'
# Canary drop-in — generated by ops/canary.sh
# Remove this file (ops/canary.sh --promote) to return to production thresholds.
[Service]
Environment=RF_SNR_MIN_DB=0
EOF
    mv "${tmp_dropin}" "${CANARY_DROP_IN}"
    log "Canary drop-in written: ${CANARY_DROP_IN}"
  fi

  service_reload

  log "Canary mode active."
  log "Monitor with: sudo bash ops/canary.sh --status"
  log "Promote when criteria met: sudo bash ops/canary.sh --promote"
  log "Rollback at any time: sudo bash ops/canary.sh --rollback"
}

# ---------------------------------------------------------------------------
# check_promotion_criteria — automated Prometheus metric gate.
# Returns 0 if all acceptance criteria pass; non-zero otherwise.
# Prints a per-criterion status line for each check.
# ---------------------------------------------------------------------------
check_promotion_criteria() {
  local failed=0   # count of failed criteria; 0 = all OK

  # --- Criterion 1: FP/rejection rate < 3 % ---------------------------------
  local total rejected fp_pct
  total="$(get_metric rf_frames_total)"
  rejected="$(get_metric rf_frames_rejected)"
  if [[ "${total}" =~ ^[0-9]+(\.[0-9]+)?$ ]] && [[ "${total%.*}" -gt 0 ]]; then
    fp_pct=$(awk "BEGIN { printf \"%.2f\", ${rejected} / ${total} * 100 }")
    if awk "BEGIN { exit !(${fp_pct} < 3.0) }"; then
      echo "  [PASS] FP/rejection rate: ${fp_pct}% < 3%"
    else
      echo "  [FAIL] FP/rejection rate: ${fp_pct}% >= 3%"
      failed=$(( failed + 1 ))
    fi
  else
    echo "  [SKIP] FP/rejection rate: insufficient data (total=${total})"
  fi

  # --- Criterion 2: CPU usage < 80 % ----------------------------------------
  local cpu_pct=0
  if systemctl is-active --quiet "${SERVICE}" 2>/dev/null; then
    local pid
    pid="$(systemctl show -p MainPID --value "${SERVICE}" 2>/dev/null || echo 0)"
    if [[ "${pid:-0}" -gt 0 ]]; then
      cpu_pct=$(ps -p "${pid}" -o %cpu --no-headers 2>/dev/null | tr -d ' ' || echo 0)
      cpu_pct="${cpu_pct:-0}"
      if awk "BEGIN { exit !(${cpu_pct} < 80.0) }"; then
        echo "  [PASS] CPU usage: ${cpu_pct}% < 80%"
      else
        echo "  [FAIL] CPU usage: ${cpu_pct}% >= 80%"
        failed=$(( failed + 1 ))
      fi
    else
      echo "  [SKIP] CPU usage: service MainPID not found"
    fi
  else
    echo "  [SKIP] CPU usage: service ${SERVICE} is not active"
  fi

  # --- Criterion 3: at least one candidate frame recorded -------------------
  # Hard failure: if no candidate frames exist the canary has not processed
  # enough signal data to be safely promoted to production.
  local candidates
  candidates="$(get_metric rf_frames_candidate)"
  if [[ "${candidates}" =~ ^[0-9]+(\.[0-9]+)?$ ]] && [[ "${candidates%.*}" -gt 0 ]]; then
    echo "  [PASS] Candidate frames recorded: ${candidates}"
  else
    echo "  [FAIL] No candidate frames yet (candidates=${candidates})."
    echo "         Allow the canary to run longer before promoting."
    failed=$(( failed + 1 ))
  fi

  return $failed
}

# ---------------------------------------------------------------------------
# ACTION: promote — promote canary to production (automated gate)
# ---------------------------------------------------------------------------
do_promote() {
  require_root
  log "=== Promoting canary to production ==="
  echo ""
  echo "Checking acceptance criteria against live Prometheus metrics..."
  echo ""

  if ! check_promotion_criteria; then
    echo ""
    log "One or more acceptance criteria FAILED. Promotion blocked."
    log "Resolve the issues above, then re-run: sudo bash ops/canary.sh --promote"
    exit 1
  fi

  echo ""
  log "All criteria passed. Proceeding with automatic promotion."

  if [[ -f "${CANARY_DROP_IN}" ]]; then
    run rm -f "${CANARY_DROP_IN}"
    log "Canary drop-in removed: ${CANARY_DROP_IN}"
  else
    warn "Canary drop-in not found (may already be removed)."
  fi

  service_reload
  log "Production config active. Canary promotion complete."
}

# ---------------------------------------------------------------------------
# ACTION: rollback — restore production config from backup
# ---------------------------------------------------------------------------
do_rollback() {
  require_root
  log "=== Rolling back to production config ==="

  # Remove canary drop-in
  if [[ -f "${CANARY_DROP_IN}" ]]; then
    run rm -f "${CANARY_DROP_IN}"
    log "Canary drop-in removed."
  fi

  # Restore latest config backup if available
  local latest_bak
  latest_bak="$(ls -t "${BACKUP_DIR}"/thresholds.env.bak.* 2>/dev/null | head -1 || true)"
  if [[ -n "${latest_bak}" ]]; then
    run cp "${latest_bak}" "${CONF_FILE}"
    log "Config restored from: ${latest_bak}"
  else
    warn "No config backup found in ${BACKUP_DIR}. Using current ${CONF_FILE}."
  fi

  service_reload
  log "Rollback complete. Service restored to production config."
}

# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------
case "${ACTION}" in
  canary)   do_canary ;;
  status)   do_status ;;
  promote)  do_promote ;;
  rollback) do_rollback ;;
  *)
    echo "[ERROR] Unknown action: ${ACTION}" >&2
    exit 1
    ;;
esac
