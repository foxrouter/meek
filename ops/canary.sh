#!/usr/bin/env bash
# ops/canary.sh — Canary procedure for rf_adapt_intel (process-worker service).
#
# Usage:
#   sudo bash ops/canary.sh [--promote | --rollback | --status | --heal] [--dry-run]
#
# Phases:
#   (default)   Enable canary mode: set RF_SNR_MIN_DB=0 and deploy drop-in.
#   --status    Show current FP/FN counters, CPU/memory, heartbeat and DB
#               staleness, and lock-fail metrics.
#   --heal      Print status then auto-recover if service is stopped/failed:
#               calls 'systemctl reset-failed' + 'systemctl start' as needed.
#               Requires root.  Run by rf-adapt-intel-monitor.timer every 30 min.
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
HEARTBEAT_FILE="${RF_HEARTBEAT_FILE:-/var/lib/rf-adapt-intel/heartbeat}"
DB_PATH="${RF_DB_PATH:-/var/lib/rf-adapt-intel/rf_adapt_intel.db}"
BACKUP_DIR="/root/rf_adapt_intel_backup"
# Heartbeat is written every 30 s by output_loop; warn after 5 minutes of silence.
STALE_HEARTBEAT_S="${RF_STALE_HEARTBEAT_S:-300}"
# Warn when no signals have been written to the DB for more than 24 hours.
DB_STALE_S="${RF_DB_STALE_S:-86400}"
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
    --heal)      ACTION="heal" ;;
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
  # RF_CANARY_SKIP_ROOT_CHECK=1 bypasses the root check in automated tests.
  if [[ "${RF_CANARY_SKIP_ROOT_CHECK:-}" == "1" ]]; then
    return 0
  fi
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
# Staleness checks — used by do_status and do_heal
# ---------------------------------------------------------------------------

# check_heartbeat_staleness — warn if the heartbeat file is older than
# STALE_HEARTBEAT_S seconds or missing.
# Returns: 0 OK, 1 stale/missing, 2 stat unavailable (cannot determine mtime).
check_heartbeat_staleness() {
  echo ""
  echo "--- Heartbeat (${HEARTBEAT_FILE}) ---"
  if [[ ! -f "${HEARTBEAT_FILE}" ]]; then
    echo "  [WARN] Heartbeat file not found: ${HEARTBEAT_FILE}"
    echo "         The output thread may never have run (service not started?)."
    return 1
  fi
  local mtime now age_s
  # GNU stat uses -c '%Y'; BSD/macOS stat uses -f '%m'. Try GNU first, then
  # BSD; if neither works emit a distinct [SKIP] rather than treating mtime as
  # epoch 0 (which would produce a spurious "STALE" warning on macOS/BSD).
  mtime=$(stat -c '%Y' "${HEARTBEAT_FILE}" 2>/dev/null \
    || stat -f '%m' "${HEARTBEAT_FILE}" 2>/dev/null \
    || echo "")
  if [[ -z "${mtime}" ]]; then
    echo "  [SKIP] Cannot read heartbeat file mtime — stat unavailable on this platform."
    return 2
  fi
  now=$(date +%s)
  age_s=$(( now - mtime ))
  # Guard against clock skew: mtime in the future gives a negative age.
  # Clamp to 0 and emit a distinct warning so the operator is alerted.
  if [[ ${age_s} -lt 0 ]]; then
    echo "  [WARN] Heartbeat mtime is in the future (clock skew? mtime=${mtime}, now=${now})."
    age_s=0
  fi
  echo "  Last heartbeat: ${age_s}s ago  (threshold: ${STALE_HEARTBEAT_S}s)"
  if [[ ${age_s} -gt ${STALE_HEARTBEAT_S} ]]; then
    echo "  [WARN] Heartbeat STALE — output thread may be blocked or service stopped."
    return 1
  fi
  echo "  [OK] Heartbeat is fresh."
  return 0
}

# check_db_staleness — warn if no signals have been written to the DB within
# DB_STALE_S seconds.  Requires sqlite3 to be installed.
# Returns: 0 if recent data found, 1 if stale/no data/query error/missing DB,
#          2 if sqlite3 is unavailable.
check_db_staleness() {
  echo ""
  echo "--- DB write staleness (${DB_PATH}) ---"
  if ! command -v sqlite3 &>/dev/null; then
    echo "  [SKIP] sqlite3 not found — cannot check DB staleness."
    return 2
  fi
  if [[ ! -f "${DB_PATH}" ]]; then
    echo "  [WARN] DB file not found: ${DB_PATH}"
    return 1
  fi
  local last_epoch sqlite_rc=0
  # Query epoch seconds directly from SQLite using strftime('%s',...) so the
  # result is a UTC-based integer and is not affected by the host timezone.
  # -batch -noheader -init /dev/null prevent ~/.sqliterc from enabling headers
  # or column mode, which would produce non-numeric output and abort bash
  # arithmetic under set -e.  stderr is merged into stdout so any error message
  # is captured.  The '|| sqlite_rc=$?' idiom captures the exit code under set -e.
  last_epoch=$(sqlite3 -batch -noheader -init /dev/null "${DB_PATH}" \
    "SELECT strftime('%s', MAX(timestamp)) FROM signals;" 2>&1) || sqlite_rc=$?
  if [[ ${sqlite_rc} -ne 0 ]]; then
    echo "  [WARN] DB query failed (rc=${sqlite_rc}): ${last_epoch}"
    echo "         This may indicate a corrupt DB, missing table, or permission issue."
    return 1
  fi
  # Validate that the result is a plain integer before bash arithmetic.
  # A non-numeric value (e.g. from a residual ~/.sqliterc header) would cause
  # age_s=$(( now_epoch - last_epoch )) to abort the script under set -e.
  # This check also covers the NULL/empty case (strftime returns NULL when the
  # table is empty, which sqlite3 prints as an empty string).
  if ! [[ "${last_epoch}" =~ ^[0-9]+$ ]]; then
    if [[ -z "${last_epoch}" ]]; then
      echo "  [WARN] No signals found in DB — service may never have written any detections."
    else
      echo "  [WARN] DB query returned non-numeric epoch: '${last_epoch}'"
      echo "         Check for headers or extra output from sqlite3 init files."
    fi
    return 1
  fi
  echo "  Latest detection epoch: ${last_epoch}"
  local now_epoch age_s
  now_epoch=$(date +%s)
  age_s=$(( now_epoch - last_epoch ))
  # Guard against clock skew or future timestamps in the DB giving a negative
  # age. Clamp to 0 and emit a distinct warning so the operator is alerted.
  if [[ ${age_s} -lt 0 ]]; then
    echo "  [WARN] DB timestamp is in the future (clock skew? last_epoch=${last_epoch}, now=${now_epoch})."
    age_s=0
  fi
  local age_h=$(( age_s / 3600 ))
  echo "  Age: ${age_s}s (${age_h}h)  (threshold: ${DB_STALE_S}s / $(( DB_STALE_S / 3600 ))h)"
  if [[ ${age_s} -gt ${DB_STALE_S} ]]; then
    echo "  [WARN] DB writes STALE — no detections in ${age_h}h."
    echo "         Check: sudo systemctl status ${SERVICE}"
    echo "         Heal:  sudo bash ${BASH_SOURCE[0]} --heal"
    return 1
  fi
  echo "  [OK] DB writes are recent."
  return 0
}

# ---------------------------------------------------------------------------
# ACTION: status — print current FP/FN rates, CPU/memory, staleness checks
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

  check_heartbeat_staleness || true
  check_db_staleness || true

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
# ACTION: heal — print status then recover a stopped/failed service
# ---------------------------------------------------------------------------
# Intended to be called periodically by rf-adapt-intel-monitor.timer (every
# 30 min).  Requires root because systemctl reset-failed/start are privileged
# operations.
#
# Recovery logic:
#   failed   → systemctl reset-failed + start   (hit StartLimitBurst)
#   inactive → systemctl start                  (e.g. manually stopped)
#   active   → no action needed
# ---------------------------------------------------------------------------
do_heal() {
  require_root
  log "=== Heal check: ${SERVICE} ==="

  # Print full status for the log (monitor.log captures stdout/stderr).
  do_status

  echo ""
  echo "--- Service state ---"
  local active_state
  active_state=$(systemctl show -p ActiveState --value "${SERVICE}" 2>/dev/null \
    || echo "unknown")
  echo "  ActiveState: ${active_state}"

  case "${active_state}" in
    active)
      log "Service is running — no recovery needed."
      ;;
    failed)
      # StartLimitBurst was exhausted or the process exited with an error and
      # systemd gave up retrying.  Reset the failure counter and restart.
      log "Service is in FAILED state. Resetting failure counter and restarting..."
      local rc=0
      # Capture rc explicitly so a systemctl error doesn't abort the monitor
      # under set -e and put the monitor itself into a start-limited state.
      run systemctl reset-failed "${SERVICE}" || rc=$?
      if [[ ${rc} -ne 0 ]]; then
        warn "systemctl reset-failed exited ${rc} — continuing anyway."
        rc=0
      fi
      run systemctl start "${SERVICE}" || rc=$?
      if [[ ${rc} -ne 0 ]]; then
        warn "systemctl start exited ${rc} — service may still be failing."
      fi
      log "Recovery attempted. New state:"
      if ! $DRY_RUN; then
        systemctl show -p ActiveState --value "${SERVICE}" 2>/dev/null \
          | awk '{print "  ActiveState: " $0}'
        log "Monitor with: sudo journalctl -u ${SERVICE} -n 20 --no-pager"
      fi
      ;;
    inactive)
      # Service is stopped (not failed, just not running).  Start it.
      log "Service is INACTIVE. Starting..."
      local rc=0
      run systemctl start "${SERVICE}" || rc=$?
      if [[ ${rc} -ne 0 ]]; then
        warn "systemctl start exited ${rc} — service may still be failing."
      fi
      log "Start attempted. New state:"
      if ! $DRY_RUN; then
        systemctl show -p ActiveState --value "${SERVICE}" 2>/dev/null \
          | awk '{print "  ActiveState: " $0}'
      fi
      ;;
    activating|deactivating|reloading)
      log "Service is transitioning (${active_state}) — skipping intervention."
      ;;
    *)
      warn "Unknown ActiveState '${active_state}' — no action taken."
      ;;
  esac
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
  heal)     do_heal ;;
  promote)  do_promote ;;
  rollback) do_rollback ;;
  *)
    echo "[ERROR] Unknown action: ${ACTION}" >&2
    exit 1
    ;;
esac
