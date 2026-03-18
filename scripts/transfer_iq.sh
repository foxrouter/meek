#!/usr/bin/env bash
# scripts/transfer_iq.sh — Transfer IQ files from Ray (edge SDR) to Brian
# (central processing server) via rsync/scp with retries, bandwidth limiting,
# logging, and an optional inotifywait watcher for automatic triggering.
# After each transfer batch the SQLite classifications DB is also synced so
# that Brian's reporting node always has up-to-date classification data.
#
# Usage:
#   bash scripts/transfer_iq.sh [--source <dir>] [--dest <user@host:path>]
#                                [--db-source <file>] [--db-dest <user@host:file>]
#                                [--bwlimit <kbps>] [--retries <n>]
#                                [--watch] [--dry-run]
#
# Environment overrides:
#   IQ_SOURCE_DIR   Local directory containing .cf32 / .raw IQ files to send.
#   IQ_DEST         rsync destination, e.g. brian@192.168.1.10:/var/lib/rf-adapt-intel/incoming/
#   IQ_BW_KBPS      rsync --bwlimit value in kbps (default 2048 = 2 Mbit/s).
#   IQ_MAX_RETRIES  Maximum rsync retry attempts per file (default 3).
#   IQ_TRANSFER_LOG Path to append transfer log lines (default /var/log/iq_transfer.log).
#   IQ_WATCH        Set to 1 to enable inotifywait watcher mode.
#   DB_SOURCE       Local path to the SQLite DB to sync (default /var/lib/rf-adapt-intel/rf_adapt_intel.db).
#   DB_DEST         rsync destination for the DB, e.g. rf_worker@192.168.4.246:/var/lib/rf-adapt-intel/rf_adapt_intel.db
#                   If unset, DB sync is skipped.
#   DB_SYNC_INTERVAL  Minimum seconds between DB syncs in watch mode (default 60).
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SOURCE_DIR="${IQ_SOURCE_DIR:-/var/lib/rf-adapt-intel/snapshots}"
DEST="${IQ_DEST:-}"
BW_KBPS="${IQ_BW_KBPS:-2048}"
MAX_RETRIES="${IQ_MAX_RETRIES:-3}"
TRANSFER_LOG="${IQ_TRANSFER_LOG:-/var/log/iq_transfer.log}"
WATCH_MODE="${IQ_WATCH:-0}"
DRY_RUN=false
DB_SOURCE="${DB_SOURCE:-/var/lib/rf-adapt-intel/rf_adapt_intel.db}"
DB_DEST="${DB_DEST:-}"
DB_SYNC_INTERVAL="${DB_SYNC_INTERVAL:-60}"
# Validate DB_SYNC_INTERVAL is a non-negative integer; fall back to default if not.
if ! [[ "${DB_SYNC_INTERVAL}" =~ ^[0-9]+$ ]]; then
  echo "[WARN] DB_SYNC_INTERVAL='${DB_SYNC_INTERVAL}' is not a non-negative integer; using default 60." >&2
  DB_SYNC_INTERVAL=60
fi

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
require_arg() {
  if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == -* ]]; then
    echo "[ERROR] Option '$1' requires an argument." >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)     require_arg "$@"; SOURCE_DIR="$2"; shift ;;
    --dest)       require_arg "$@"; DEST="$2";       shift ;;
    --db-source)  require_arg "$@"; DB_SOURCE="$2";  shift ;;
    --db-dest)    require_arg "$@"; DB_DEST="$2";    shift ;;
    --bwlimit)    require_arg "$@"; BW_KBPS="$2";    shift ;;
    --retries)    require_arg "$@"; MAX_RETRIES="$2"; shift ;;
    --watch)      WATCH_MODE=1 ;;
    --dry-run)    DRY_RUN=true ;;
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

if [[ -z "${DEST}" ]]; then
  echo "[ERROR] Destination not set. Use --dest or IQ_DEST env var." >&2
  echo "  Example: IQ_DEST=brian@192.168.1.10:/var/lib/rf-adapt-intel/incoming/" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
  local msg="[$(date '+%Y-%m-%dT%H:%M:%S')] $*"
  echo "${msg}"
  if ! $DRY_RUN; then
    echo "${msg}" >> "${TRANSFER_LOG}" 2>/dev/null || true
  fi
}

# Run a command piping stdout+stderr to the transfer log.
# The exit code reflects only the command's outcome; tee failures are non-fatal.
run_logged() {
  local cmd_rc
  set +o pipefail
  "$@" 2>&1 | { tee -a "${TRANSFER_LOG}" 2>/dev/null || true; }
  cmd_rc="${PIPESTATUS[0]}"
  set -o pipefail
  return "${cmd_rc}"
}

run_rsync() {
  local file="$1"
  local attempt=1
  while [[ "${attempt}" -le "${MAX_RETRIES}" ]]; do
    if $DRY_RUN; then
      log "[dry-run] rsync --bwlimit=${BW_KBPS} '${file}' '${DEST}'"
      return 0
    fi
    # --partial: resume interrupted transfers; --timeout: abort stalled transfers.
    # Add --checksum if IQ integrity verification is critical (increases bandwidth
    # usage since rsync re-reads both sides to compare checksums).
    if run_logged rsync -az --bwlimit="${BW_KBPS}" --partial --timeout=30 \
        "${file}" "${DEST}"; then
      log "OK transferred: $(basename "${file}")"
      return 0
    fi
    log "WARN attempt ${attempt}/${MAX_RETRIES} failed for $(basename "${file}")"
    attempt=$(( attempt + 1 ))
    sleep $(( attempt * 5 ))
  done
  log "ERROR failed to transfer $(basename "${file}") after ${MAX_RETRIES} attempts"
  return 1
}

# ---------------------------------------------------------------------------
# Sync the SQLite classifications DB to Brian.
# Skipped silently when DB_DEST is empty.
# A consistent snapshot is created via "sqlite3 .backup" (online backup API)
# before rsyncing.  The online backup API holds only brief shared locks and
# does not rewrite the entire database, so it is less disruptive to concurrent
# worker writes than VACUUM INTO.  When sqlite3 is unavailable, or when the
# snapshot step fails, the function falls back to rsyncing the live DB and
# sidecar files, which is inherently racy.
# Rsync respects the same --bwlimit as the IQ file transfers.
# ---------------------------------------------------------------------------
sync_db() {
  if [[ -z "${DB_DEST}" ]]; then
    return 0
  fi
  # Dry-run: log intended operations without any filesystem or SQLite work.
  if $DRY_RUN; then
    log "[dry-run] rsync -az --bwlimit=${BW_KBPS} '${DB_SOURCE}' '${DB_DEST}'"
    [[ -f "${DB_SOURCE}-wal" ]] && \
      log "[dry-run] rsync -az --bwlimit=${BW_KBPS} '${DB_SOURCE}-wal' '${DB_DEST}-wal'"
    [[ -f "${DB_SOURCE}-shm" ]] && \
      log "[dry-run] rsync -az --bwlimit=${BW_KBPS} '${DB_SOURCE}-shm' '${DB_DEST}-shm'"
    return 0
  fi
  if [[ ! -f "${DB_SOURCE}" ]]; then
    log "WARN DB file not found, skipping DB sync: ${DB_SOURCE}"
    return 0
  fi

  # Build a consistent snapshot to rsync (avoids WAL-mode raciness).
  # Use the same directory as DB_SOURCE so the snapshot stays on the same
  # filesystem (sufficient space, no cross-device copy) and avoids TMPDIR.
  local snap_file=""
  if command -v sqlite3 &>/dev/null; then
    local snap_dir
    snap_dir="$(dirname "${DB_SOURCE}")"
    local snap_tmp
    # Use an if-guard so mktemp failure is non-fatal: fall back to live-file rsync.
    if snap_tmp="$(mktemp --tmpdir="${snap_dir}" --suffix=.db 2>/dev/null)"; then
      snap_file="${snap_tmp}"
      # Use the SQLite online backup API (.backup): avoids a full DB rewrite and
      # holds only brief shared locks, causing minimal disruption to concurrent
      # worker writes.  Unlike VACUUM INTO, .backup can write to an existing file.
      if ! run_logged sqlite3 "${DB_SOURCE}" ".backup '${snap_file}'"; then
        log "WARN sqlite3 .backup failed; falling back to live-file rsync"
        [[ -n "${snap_file}" ]] && rm -f "${snap_file}"
        snap_file=""
      fi
    else
      log "WARN mktemp failed in ${snap_dir}; falling back to live-file rsync"
    fi
  else
    log "WARN sqlite3 not found; rsyncing live DB files (may be racy in WAL mode)"
  fi

  local attempt=1
  while [[ "${attempt}" -le "${MAX_RETRIES}" ]]; do
    local ok=true
    if [[ -n "${snap_file}" ]]; then
      run_logged rsync -az --bwlimit="${BW_KBPS}" --partial --timeout=30 \
          "${snap_file}" "${DB_DEST}" || ok=false
    else
      # Fallback: sync live DB and sidecars (racy but better than nothing).
      run_logged rsync -az --bwlimit="${BW_KBPS}" --partial --timeout=30 \
          "${DB_SOURCE}" "${DB_DEST}" || ok=false
      if $ok && [[ -f "${DB_SOURCE}-wal" ]]; then
        run_logged rsync -az --bwlimit="${BW_KBPS}" --partial --timeout=30 \
            "${DB_SOURCE}-wal" "${DB_DEST}-wal" || ok=false
      fi
      if $ok && [[ -f "${DB_SOURCE}-shm" ]]; then
        run_logged rsync -az --bwlimit="${BW_KBPS}" --partial --timeout=30 \
            "${DB_SOURCE}-shm" "${DB_DEST}-shm" || ok=false
      fi
    fi
    if $ok; then
      log "OK DB synced: $(basename "${DB_SOURCE}") -> ${DB_DEST}"
      rm -f "${snap_file}"
      return 0
    fi
    log "WARN DB sync attempt ${attempt}/${MAX_RETRIES} failed"
    if [[ "${attempt}" -lt "${MAX_RETRIES}" ]]; then
      sleep $(( attempt * 5 ))
    fi
    attempt=$(( attempt + 1 ))
  done
  rm -f "${snap_file}"
  log "ERROR DB sync failed after ${MAX_RETRIES} attempts"
  return 1
}

# Sync the DB only when DB_SYNC_INTERVAL seconds have elapsed since the last
# sync.  _last_db_sync must live in the caller's shell (not a subshell).
_last_db_sync=0
sync_db_if_due() {
  local now
  now=$(date +%s)
  if (( now - _last_db_sync >= DB_SYNC_INTERVAL )); then
    # Record the start time before syncing so the interval counts from when
    # this sync began, not after it finishes (avoids immediate re-trigger
    # when sync takes longer than DB_SYNC_INTERVAL).
    _last_db_sync=${now}
    sync_db || true
  fi
}

# ---------------------------------------------------------------------------
# Transfer all existing IQ files in SOURCE_DIR
# ---------------------------------------------------------------------------
transfer_dir() {
  if [[ ! -d "${SOURCE_DIR}" ]]; then
    log "WARN source directory does not exist: ${SOURCE_DIR}"
    return 0
  fi
  local count=0
  local failed=0
  while IFS= read -r -d '' file; do
    if run_rsync "${file}"; then
      count=$(( count + 1 ))
    else
      failed=$(( failed + 1 ))
    fi
  done < <(find "${SOURCE_DIR}" -maxdepth 1 \( -name '*.cf32' -o -name '*.raw' \) -print0 2>/dev/null | sort -z)
  log "Transfer sweep complete: ${count} OK, ${failed} failed"
  # Record the sync timestamp before syncing (same convention as sync_db_if_due)
  # so watch mode doesn't immediately re-trigger a DB sync after the initial sweep.
  _last_db_sync=$(date +%s)
  sync_db || true
}

# ---------------------------------------------------------------------------
# inotifywait watcher: trigger transfer on new .cf32 / .raw files
# ---------------------------------------------------------------------------
watch_and_transfer() {
  if ! command -v inotifywait &>/dev/null; then
    log "ERROR inotifywait not found. Install inotify-tools: apt install inotify-tools" >&2
    exit 1
  fi
  log "Watching ${SOURCE_DIR} for new IQ files (Ctrl-C to stop)..."
  mkdir -p "${SOURCE_DIR}"
  # Use process substitution (not a pipe) so the while loop runs in this shell
  # and shares _last_db_sync state with sync_db_if_due.
  # --format '%w%f' gives full path; -e close_write triggers after file is done
  while IFS= read -r new_file; do
    case "${new_file}" in
      *.cf32|*.raw)
        log "New file detected: $(basename "${new_file}")"
        run_rsync "${new_file}" || true
        sync_db_if_due
        ;;
    esac
  done < <(inotifywait -m -r -e close_write --format '%w%f' "${SOURCE_DIR}")
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
log "rf_adapt_intel IQ transfer: source=${SOURCE_DIR} dest=${DEST} bwlimit=${BW_KBPS}kbps"
if [[ -n "${DB_DEST}" ]]; then
  log "DB sync enabled: ${DB_SOURCE} -> ${DB_DEST}"
fi

if [[ "${WATCH_MODE}" == "1" ]]; then
  # Watch mode: first do an initial sweep, then watch for new files
  transfer_dir
  watch_and_transfer
else
  transfer_dir
fi
