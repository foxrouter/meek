#!/usr/bin/env bash
# scripts/heartbeat_and_metrics.sh — Write heartbeat + Prometheus textfile
# on a fixed cadence.  Can be run as a background loop or a one-shot.
# Usage: bash scripts/heartbeat_and_metrics.sh [interval_seconds]
set -euo pipefail

INTERVAL="${1:-30}"
METRICS_FILE="${RF_METRICS_FILE:-/var/lib/rf-adapt-intel/metrics.prom}"
HEARTBEAT_FILE="${RF_HEARTBEAT_FILE:-/var/lib/rf-adapt-intel/heartbeat}"
WORKER_LOG="${RF_WORKER_LOG:-/var/lib/rf-adapt-intel/worker.log}"
SERVICE="process-worker"

mkdir -p "$(dirname "${METRICS_FILE}")"
mkdir -p "$(dirname "${HEARTBEAT_FILE}")"

write_once() {
  local ts
  ts=$(date +%s)

  # Heartbeat
  echo "ok ${ts}" > "${HEARTBEAT_FILE}"

  # Count recent log entries for metrics
  local total=0 rejected=0 candidates=0 conf_sum=0 n_conf=0
  if [[ -f "${WORKER_LOG}" ]]; then
    # Process only the last 10000 lines to avoid O(n) growth on large logs
    while IFS= read -r line; do
      total=$((total + 1))
      # count rejections (snr or bw gate)
      if echo "${line}" | grep -q '"snr_gate_pass":false\|"bw_gate_pass":false'; then
        rejected=$((rejected + 1))
      fi
      # count candidates (confidence > 0) — validate conf is a plain decimal
      local conf
      conf=$(echo "${line}" | grep -oP '"confidence":\K[0-9]+(\.[0-9]+)?' || true)
      if [[ "${conf}" =~ ^[0-9]+(\.[0-9]+)?$ ]] && \
         awk -v c="${conf}" 'BEGIN{exit !(c > 0)}'; then
        candidates=$((candidates + 1))
        conf_sum=$(awk -v s="${conf_sum}" -v c="${conf}" 'BEGIN{print s + c}')
        n_conf=$((n_conf + 1))
      fi
    done < <(tail -n 10000 "${WORKER_LOG}")
  fi

  local avg_conf=0
  if [[ ${n_conf} -gt 0 ]]; then
    avg_conf=$(awk -v s="${conf_sum}" -v n="${n_conf}" 'BEGIN{printf "%.6f", s / n}')
  fi

  # Service up/down
  local svc_up=0
  systemctl is-active --quiet "${SERVICE}" 2>/dev/null && svc_up=1 || true

  cat > "${METRICS_FILE}" <<EOF
# HELP rf_worker_up 1 if process-worker service is active
# TYPE rf_worker_up gauge
rf_worker_up ${svc_up}
# HELP rf_log_frames_total Total JSON log entries observed
# TYPE rf_log_frames_total counter
rf_log_frames_total ${total}
# HELP rf_log_frames_rejected Log entries with gate rejections
# TYPE rf_log_frames_rejected counter
rf_log_frames_rejected ${rejected}
# HELP rf_log_frames_candidate Log entries with confidence > 0
# TYPE rf_log_frames_candidate counter
rf_log_frames_candidate ${candidates}
# HELP rf_log_confidence_avg Average confidence of candidate frames
# TYPE rf_log_confidence_avg gauge
rf_log_confidence_avg ${avg_conf}
# HELP rf_heartbeat_timestamp_seconds Unix timestamp of last heartbeat
# TYPE rf_heartbeat_timestamp_seconds gauge
rf_heartbeat_timestamp_seconds ${ts}
EOF
  echo "[heartbeat] ts=${ts} total=${total} rejected=${rejected} candidates=${candidates} avg_conf=${avg_conf}"
}

if [[ "${INTERVAL}" == "once" ]]; then
  write_once
  exit 0
fi

echo "[heartbeat_and_metrics] running every ${INTERVAL}s (Ctrl-C to stop)"
while true; do
  write_once || true
  sleep "${INTERVAL}"
done
