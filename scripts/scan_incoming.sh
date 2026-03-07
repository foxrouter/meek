#!/usr/bin/env bash
# scripts/scan_incoming.sh — Scan the incoming IQ directory and process each new file.
# Called by rf-incoming-processor.service (triggered by rf-incoming-processor.path).
#
# For every *.cf32 or *.raw file found in INCOMING_DIR:
#   1. Calls process_incoming.sh <file> for offline analysis.
#   2. Moves the file to PROCESSED_DIR on success, or logs failure and moves it anyway
#      so the path watcher does not re-trigger indefinitely.
#
# Environment overrides:
#   INCOMING_DIR    Directory to scan  (default /var/lib/rf-adapt-intel/incoming)
#   PROCESSED_DIR   Move target        (default /var/lib/rf-adapt-intel/processed)
#   PROCESS_SCRIPT  Path to process_incoming.sh
#                   (default /usr/local/share/rf-adapt-intel/scripts/process_incoming.sh)
set -euo pipefail

INCOMING_DIR="${INCOMING_DIR:-/var/lib/rf-adapt-intel/incoming}"
PROCESSED_DIR="${PROCESSED_DIR:-/var/lib/rf-adapt-intel/processed}"
PROCESS_SCRIPT="${PROCESS_SCRIPT:-/usr/local/share/rf-adapt-intel/scripts/process_incoming.sh}"

shopt -s nullglob

mkdir -p "${PROCESSED_DIR}"

processed=0
failed=0

for f in "${INCOMING_DIR}"/*.cf32 "${INCOMING_DIR}"/*.raw; do
  [[ -f "${f}" ]] || continue
  echo "[scan_incoming] processing: $(basename "${f}")"
  if bash "${PROCESS_SCRIPT}" "${f}"; then
    echo "[scan_incoming] OK: $(basename "${f}")"
    processed=$(( processed + 1 ))
  else
    echo "[scan_incoming] WARN: process_incoming.sh failed for $(basename "${f}")" >&2
    failed=$(( failed + 1 ))
  fi
  # Always move the file so the path watcher does not re-trigger.
  mv -f "${f}" "${PROCESSED_DIR}/" 2>/dev/null || \
    echo "[scan_incoming] WARN: could not move $(basename "${f}") to ${PROCESSED_DIR}" >&2
done

echo "[scan_incoming] complete: ${processed} processed, ${failed} failed"
