#!/usr/bin/env bash
# scripts/process_incoming.sh — Process an incoming raw IQ file through rf_adapt_intel
# Usage: bash scripts/process_incoming.sh <path/to/file.raw>
#
# Band detection is done by filename convention: the filename may contain a
# known band tag (433, 868, 915, 315, 137, etc.).  All classifier env vars
# can be further overridden in /etc/rf_worker/thresholds.env.
#
# File-replay mode (offline analysis):
#   When the worker binary reads from SoapySDR, offline IQ analysis is done via
#   tools/decode_candidates.py.  Set REPLAY_DB to the SQLite database path if
#   you want to analyse previously stored candidates; otherwise the script runs
#   decode_candidates.py directly on the IQ snapshot directory.
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
SNAPSHOT_DIR="${SNAPSHOT_DIR:-/var/lib/rf-adapt-intel/snapshots}"
REPLAY_DB="${REPLAY_DB:-}"

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

# First, try to extract the structured _b<band> tag embedded by rf_adapt_intel v3+.
# This avoids false positives from numeric substrings in timestamps or directories.
MATCHED_BAND=""
if [[ "${BASENAME}" =~ _b([A-Za-z0-9_-]+)\.(cf32|raw)$ ]]; then
  MATCHED_BAND="${BASH_REMATCH[1]}"
fi

case "${MATCHED_BAND}" in
  ISM-433|TPMS-433)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=433920000;  SAMPLE_RATE=2048000; RSYM=128000; FDEV=50000;  MOD_HINT=fsk; BW_HZ=100000 ;;
  LORA-868|SMETS2|ZWAVE-868|SIGFOX-868|ZIGBEE-868)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=868000000;  SAMPLE_RATE=2048000; RSYM=250000; FDEV=62500;  MOD_HINT=fsk; BW_HZ=125000 ;;
  NOAA-APT)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=137500000;  SAMPLE_RATE=2048000; RSYM=4160;   FDEV=17000;  MOD_HINT=fsk; BW_HZ=34000  ;;
  VDL2)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=136900000;  SAMPLE_RATE=2048000; RSYM=10500;  FDEV=25000;  MOD_HINT=psk; BW_HZ=25000  ;;
  ACARS-VHF)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=136900000;  SAMPLE_RATE=2048000; RSYM=2400;   FDEV=8000;   MOD_HINT=fsk; BW_HZ=8000   ;;
  METEOR-LRPT)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=137100000;  SAMPLE_RATE=2048000; RSYM=72000;  FDEV=120000; MOD_HINT=psk; BW_HZ=120000 ;;
  ACARS)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=131725000;  SAMPLE_RATE=2048000; RSYM=2400;   FDEV=8000;   MOD_HINT=fsk; BW_HZ=8000   ;;
  AIS-A)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=161975000;  SAMPLE_RATE=2048000; RSYM=9600;   FDEV=16000;  MOD_HINT=fsk; BW_HZ=16000  ;;
  AIS-B)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=162025000;  SAMPLE_RATE=2048000; RSYM=9600;   FDEV=16000;  MOD_HINT=fsk; BW_HZ=16000  ;;
  POCSAG-153)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=153350000;  SAMPLE_RATE=2048000; RSYM=2400;   FDEV=12500;  MOD_HINT=fsk; BW_HZ=12500  ;;
  FLEX-931)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=931937500;  SAMPLE_RATE=2048000; RSYM=6400;   FDEV=15000;  MOD_HINT=fsk; BW_HZ=15000  ;;
  RADIOSONDE)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=402500000;  SAMPLE_RATE=2048000; RSYM=2400;   FDEV=100000; MOD_HINT=fsk; BW_HZ=100000 ;;
  TETRA)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=392000000;  SAMPLE_RATE=2048000; RSYM=18000;  FDEV=25000;  MOD_HINT=psk; BW_HZ=25000  ;;
  DMR|PMR446)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=446006000;  SAMPLE_RATE=2048000; RSYM=9600;   FDEV=12500;  MOD_HINT=fsk; BW_HZ=12500  ;;
  APRS)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=144800000;  SAMPLE_RATE=2048000; RSYM=1200;   FDEV=16000;  MOD_HINT=fsk; BW_HZ=16000  ;;
  WMBUS-169|ISM-169)
    BAND="${MATCHED_BAND}"; CENTER_FREQ=169406000;  SAMPLE_RATE=2048000; RSYM=2400;   FDEV=12500;  MOD_HINT=fsk; BW_HZ=12500  ;;
  "")
    # No structured band tag; fall back to numeric substring patterns for legacy filenames.
    case "${BASENAME}" in
      *433*) BAND=433; CENTER_FREQ=433920000;  SAMPLE_RATE=2048000; RSYM=128000; FDEV=50000;  MOD_HINT=fsk; BW_HZ=100000 ;;
      *868*) BAND=868; CENTER_FREQ=868000000;  SAMPLE_RATE=2048000; RSYM=250000; FDEV=62500;  MOD_HINT=fsk; BW_HZ=125000 ;;
      *915*) BAND=915; CENTER_FREQ=915000000;  SAMPLE_RATE=2048000; RSYM=250000; FDEV=62500;  MOD_HINT=fsk; BW_HZ=125000 ;;
      *315*) BAND=315; CENTER_FREQ=315000000;  SAMPLE_RATE=2048000; RSYM=128000; FDEV=50000;  MOD_HINT=ook; BW_HZ=100000 ;;
      *137*) BAND=137; CENTER_FREQ=137500000;  SAMPLE_RATE=2048000; RSYM=4160;   FDEV=17000;  MOD_HINT=fsk; BW_HZ=34000  ;;
      *) echo "[process_incoming] WARNING: unknown band in filename '${BASENAME}', using 433 defaults" ;;
    esac
    ;;
  *)
    # Structured tag present but not in the table above; set BAND and use 433 defaults.
    BAND="${MATCHED_BAND}"
    echo "[process_incoming] INFO: band '${MATCHED_BAND}' not in decoder table, using 433 defaults" ;;
esac

echo "[process_incoming] file=${IQ_FILE} band=${BAND} center=${CENTER_FREQ} sps=${SAMPLE_RATE} mod_hint=${MOD_HINT}"

mkdir -p "${OUTPUT_DIR}"
OUT_FILE="${OUTPUT_DIR}/${BASENAME%.raw}.processed.raw"

# Run the worker with band-specific env vars
export RF_SNR_MIN_DB="${SNR_MIN}"
export RF_EXPECTED_BW_HZ="${BW_HZ}"
export RF_WORKER_LOG="${WORKER_LOG}"
export BAND="${BAND}"
export RF_RSYM="${RSYM}"
export RF_FDEV="${FDEV}"
[[ -n "${MOD_HINT}" ]] && export MOD_HINT="${MOD_HINT}"

# ---------------------------------------------------------------------------
# File-replay mode: rf_adapt_intel reads from SoapySDR live, so offline IQ
# file analysis is performed via tools/decode_candidates.py.
# The IQ file is first copied into the snapshot directory so decode_candidates
# can locate it, then the tool is invoked to produce a JSON audit report.
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DECODE_TOOL="${REPO_ROOT}/tools/decode_candidates.py"

if [[ -f "${DECODE_TOOL}" ]] && command -v python3 &>/dev/null; then
  # Copy IQ snapshot into the snapshot directory so decode_candidates sees it
  mkdir -p "${SNAPSHOT_DIR}"
  SNAP_COPY="${SNAPSHOT_DIR}/${BASENAME}"
  if [[ "${IQ_FILE}" != "${SNAP_COPY}" ]]; then
    cp "${IQ_FILE}" "${SNAP_COPY}"
  fi

  # Use REPLAY_DB if provided, otherwise let decode_candidates use its default
  DB_ARG=""
  if [[ -n "${REPLAY_DB}" ]]; then
    DB_ARG="--db ${REPLAY_DB}"
  fi

  REPORT_FILE="${OUTPUT_DIR}/${BASENAME%.raw}.audit.json"
  echo "[process_incoming] running offline analysis via decode_candidates.py"
  if python3 "${DECODE_TOOL}" \
    ${DB_ARG} \
    --snapshot-dir "${SNAPSHOT_DIR}" \
    --sample-rate "${SAMPLE_RATE}" \
    --out "${REPORT_FILE}" \
    2>&1; then
    echo "[process_incoming] offline report: ${REPORT_FILE}"
  else
    rc=$?
    echo "[process_incoming] WARN: decode_candidates.py exited with status ${rc}" >&2
    echo "[process_incoming] Check python3 dependencies (numpy) and file permissions."
  fi
  echo "[process_incoming] output file (replay): ${OUT_FILE}"
  # Copy/symlink the original IQ file to the expected output path
  cp "${IQ_FILE}" "${OUT_FILE}" 2>/dev/null || true
else
  # rf_adapt_intel reads from the SDR device; for offline file processing feed
  # stdin via a named pipe / replace with a replay tool when available.
  echo "[process_incoming] output will be at ${OUT_FILE}"
fi

echo "[process_incoming] last JSON log entries:"
tail -n 5 "${WORKER_LOG}" 2>/dev/null || true

