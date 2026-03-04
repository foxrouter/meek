#!/usr/bin/env bash
# scripts/process_incoming.sh — Process an incoming raw IQ file through rf_adapt_intel
# Usage: bash scripts/process_incoming.sh <path/to/file.raw>
#
# Band detection is done by filename convention: the filename may contain a
# known band tag (433, 868, 915, 315, 137, etc.).  All classifier env vars
# can be further overridden in /etc/rf_worker/thresholds.env.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <iq_file.raw>" >&2
  exit 1
fi

IQ_FILE="$1"
BASENAME="$(basename "${IQ_FILE}")"
WORKER_BIN="${WORKER_BIN:-/usr/local/bin/rf_adapt_intel}"
OUTPUT_DIR="${OUTPUT_DIR:-/var/lib/rf-adapt-intel/processed}"
WORKER_LOG="${WORKER_LOG:-/var/lib/rf-adapt-intel/worker.log}"

# Load site-level defaults: only accept KEY=VALUE assignment lines (no commands)
if [[ -f /etc/rf_worker/thresholds.env ]]; then
  while IFS='=' read -r key val; do
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && export "${key}=${val}" || true
  done < /etc/rf_worker/thresholds.env
fi

# --- Band detection from filename ---
BAND="unknown"
CENTER_FREQ="433920000"
SAMPLE_RATE="2048000"
RSYM="128000"
FDEV="50000"
MOD_HINT=""
SNR_MIN="${RF_SNR_MIN_DB:-0.0}"
BW_HZ="0"

case "${BASENAME}" in
  *433*) BAND=433;  CENTER_FREQ=433920000;  SAMPLE_RATE=2048000; RSYM=128000; FDEV=50000;  MOD_HINT=fsk; BW_HZ=100000 ;;
  *868*) BAND=868;  CENTER_FREQ=868000000;  SAMPLE_RATE=2048000; RSYM=250000; FDEV=62500;  MOD_HINT=fsk; BW_HZ=125000 ;;
  *915*) BAND=915;  CENTER_FREQ=915000000;  SAMPLE_RATE=2048000; RSYM=250000; FDEV=62500;  MOD_HINT=fsk; BW_HZ=125000 ;;
  *315*) BAND=315;  CENTER_FREQ=315000000;  SAMPLE_RATE=2048000; RSYM=128000; FDEV=50000;  MOD_HINT=ook; BW_HZ=100000 ;;
  *137*) BAND=137;  CENTER_FREQ=137500000;  SAMPLE_RATE=2048000; RSYM=4160;   FDEV=17000;  MOD_HINT=fsk; BW_HZ=34000  ;;
  *) echo "[process_incoming] WARNING: unknown band in filename '${BASENAME}', using 433 defaults" ;;
esac

echo "[process_incoming] file=${IQ_FILE} band=${BAND} center=${CENTER_FREQ} sps=${SAMPLE_RATE} mod_hint=${MOD_HINT}"

mkdir -p "${OUTPUT_DIR}"
OUT_FILE="${OUTPUT_DIR}/${BASENAME%.raw}.processed.raw"

# Run the worker with band-specific env vars
export RF_SNR_MIN_DB="${SNR_MIN}"
export RF_EXPECTED_BW_HZ="${BW_HZ}"
export RF_WORKER_LOG="${WORKER_LOG}"
export BAND="${BAND}"
export RSYM="${RSYM}"
export FDEV="${FDEV}"
[[ -n "${MOD_HINT}" ]] && export MOD_HINT="${MOD_HINT}"

# rf_adapt_intel reads from the SDR device; for offline file processing feed
# stdin via a named pipe / replace with a replay tool when available.
echo "[process_incoming] output will be at ${OUT_FILE}"
echo "[process_incoming] last JSON log entries:"
tail -n 5 "${WORKER_LOG}" 2>/dev/null || true
