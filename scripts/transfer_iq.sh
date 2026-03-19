#!/usr/bin/env bash
# scripts/transfer_iq.sh — Transfer IQ files from Ray (edge SDR) to Brian
# (central processing server) via rsync/scp with retries, bandwidth limiting,
# logging, and an optional inotifywait watcher for automatic triggering.
# When DB_DEST is set, the SQLite classifications DB is also synced after each
# transfer batch so that Brian's reporting node has up-to-date classification
# data.  DB sync is skipped silently when DB_DEST is unset.
#
# Usage:
#   bash scripts/transfer_iq.sh [--source <dir>] [--dest <user@host:path>]
#                                [--db-source <file>] [--db-dest <user@host:file>]
#                                [--bwlimit <kbps>] [--retries <n>]
#                                [--watch] [--dry-run]
#
# Environment overrides:
#   IQ_SOURCE_DIR   Local directory containing .cf32 / .raw IQ files to send.
#   IQ_DEST         rsync destination, e.g. rf_worker@<brian_host>:/var/lib/rf-adapt-intel/incoming/
#   IQ_BW_KBPS      rsync --bwlimit value in kbps (default 2048 = 2 Mbit/s).
#   IQ_MAX_RETRIES  Maximum rsync retry attempts per file (default 3).
#   IQ_TRANSFER_LOG Path to append transfer log lines (default /var/log/iq_transfer.log).
#   IQ_WATCH        Set to 1 to enable inotifywait watcher mode.
#   DB_SOURCE       Local path to the SQLite DB to sync (default /var/lib/rf-adapt-intel/rf_adapt_intel.db).
#   DB_DEST         rsync destination for the DB, e.g. rf_worker@<brian_host>:/var/lib/rf-adapt-intel/rf_adapt_intel.db
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
# Force base-10 interpretation so values with leading zeros (e.g. "010") are
# treated as decimal rather than octal in arithmetic expressions like
# (( now - _last_db_sync >= DB_SYNC_INTERVAL )).
DB_SYNC_INTERVAL=$(( 10#${DB_SYNC_INTERVAL} ))

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
  echo "  Example: IQ_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/incoming/" >&2
  exit 1
fi
if [[ -n "${DB_DEST}" ]]; then
  if [[ "${DB_DEST}" == */ ]]; then
    echo "[ERROR] DB_DEST must be a file path, not a directory (trailing '/' not allowed)." >&2
    echo "  Example: DB_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/rf_adapt_intel.db" >&2
    exit 1
  fi
  # Reject rsync-daemon syntax (host::module/path): the double-colon is parsed
  # as host="host", path=":module/path" by our remote-detection logic, causing
  # a bogus ssh cleanup target and a false sync failure.  Daemon destinations
  # are not supported; require the standard [user@]host:/path form.
  # IPv6 bracket addresses (user@[::1]:/path) contain :: inside [...] and are
  # allowed; :: in the path portion of a remote destination (e.g.
  # user@host:/var/lib/db::backup.sqlite) is also valid and must not be
  # rejected.  Strategy: only check the host segment (everything before the
  # first '/') for ::, after stripping any IPv6 bracket content.
  if [[ "${DB_DEST}" == *::* ]]; then
    _before_slash="${DB_DEST%%/*}"
    if [[ "${_before_slash}" == *\[*\]* ]]; then
      _check="${_before_slash%%\[*}${_before_slash##*\]}"
    else
      _check="${_before_slash}"
    fi
    if [[ "${_check}" == *::* ]]; then
      echo "[ERROR] DB_DEST '${DB_DEST}' uses rsync-daemon syntax (::) which is not supported." >&2
      echo "  Use the standard [user@]host:/path form instead." >&2
      exit 1
    fi
    unset _before_slash _check
  fi
  # Reject remote destinations with an empty path component (e.g. user@host:).
  # rsync would write the snapshot into the remote home directory (not a named
  # file), and the WAL/SHM cleanup would target just '-wal'/'-shm' with no
  # preceding path, which is clearly wrong.
  if [[ "${DB_DEST}" =~ ^([^/@]+@)?\[([^]]*)\]:(.*)$ ]]; then
    # IPv6 bracket form: host is the second capture group, path is the third.
    if [[ -z "${BASH_REMATCH[2]}" ]]; then
      echo "[ERROR] DB_DEST '${DB_DEST}' has an empty bracket host; specify a host address." >&2
      echo "  Example: DB_DEST=rf_worker@[::1]:/var/lib/rf-adapt-intel/rf_adapt_intel.db" >&2
      exit 1
    fi
    if [[ -z "${BASH_REMATCH[3]}" ]]; then
      echo "[ERROR] DB_DEST '${DB_DEST}' has an empty remote path; specify a full file path." >&2
      echo "  Example: DB_DEST=rf_worker@[::1]:/var/lib/rf-adapt-intel/rf_adapt_intel.db" >&2
      exit 1
    fi
  elif [[ "${DB_DEST}" == *:* && "${DB_DEST%%:*}" != */* ]]; then
    # Standard remote form: [user@]host:path — host is before the colon, path after.
    _remote_prefix="${DB_DEST%%:*}"
    # Reject cases with an '@' but no username before it (e.g. @host:/path).
    if [[ "${_remote_prefix}" == @* ]]; then
      echo "[ERROR] DB_DEST '${DB_DEST}' has an empty username before '@'; specify a non-empty user or omit '@' entirely." >&2
      echo "  Example: DB_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/rf_adapt_intel.db" >&2
      unset _remote_prefix; exit 1
    fi
    _std_host="${_remote_prefix##*@}"
    if [[ -z "${_std_host}" ]]; then
      echo "[ERROR] DB_DEST '${DB_DEST}' has an empty remote host; specify a hostname." >&2
      echo "  Example: DB_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/rf_adapt_intel.db" >&2
      unset _std_host _remote_prefix; exit 1
    fi
    unset _std_host _remote_prefix
    if [[ -z "${DB_DEST#*:}" ]]; then
      echo "[ERROR] DB_DEST '${DB_DEST}' has an empty remote path; specify a full file path." >&2
      echo "  Example: DB_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/rf_adapt_intel.db" >&2
      exit 1
    fi
  fi
  # For local paths (no remote colon), reject an existing directory even without
  # a trailing slash: rsync would place the snapshot in the directory using the
  # source file's basename, breaking the WAL/SHM cleanup logic that assumes
  # DB_DEST is a complete file path (e.g. cleanup targets "${DB_DEST}-wal").
  # A path is local when it has no colon OR when the part before the colon
  # contains a slash (absolute path like /some/dir:/bad has a slash before ':').
  if { [[ "${DB_DEST}" != *:* ]] || [[ "${DB_DEST%%:*}" == */* ]]; } && [[ -d "${DB_DEST}" ]]; then
    echo "[ERROR] DB_DEST '${DB_DEST}' is an existing directory; a file path is required." >&2
    echo "  Example: DB_DEST=/var/lib/rf-adapt-intel/rf_adapt_intel.db" >&2
    exit 1
  fi
fi

# Validate integer parameters (may have been set via env or CLI).
# Regex check is done first (safe, no arithmetic), then normalize to base-10
# with $(( 10#... )) before doing numeric comparisons so octal-looking values
# like '08' or '010' don't cause 'value too great for base' errors.
if ! [[ "${BW_KBPS}" =~ ^[0-9]+$ ]]; then
  echo "[WARN] IQ_BW_KBPS/--bwlimit='${BW_KBPS}' is not a positive integer; using default 2048." >&2
  BW_KBPS=2048
else
  BW_KBPS=$(( 10#${BW_KBPS} ))
  if [[ "${BW_KBPS}" -lt 1 ]]; then
    echo "[WARN] IQ_BW_KBPS/--bwlimit='${BW_KBPS}' is not a positive integer; using default 2048." >&2
    BW_KBPS=2048
  fi
fi
if ! [[ "${MAX_RETRIES}" =~ ^[0-9]+$ ]]; then
  echo "[WARN] IQ_MAX_RETRIES/--retries='${MAX_RETRIES}' is not a positive integer; using default 3." >&2
  MAX_RETRIES=3
else
  MAX_RETRIES=$(( 10#${MAX_RETRIES} ))
  if [[ "${MAX_RETRIES}" -lt 1 ]]; then
    echo "[WARN] IQ_MAX_RETRIES/--retries='${MAX_RETRIES}' is not a positive integer; using default 3." >&2
    MAX_RETRIES=3
  fi
fi

# Open log FD (3) once after a symlink check.  All writes go via this FD so
# they always reach the originally-opened inode even if the filesystem path is
# later replaced with a symlink, significantly reducing TOCTOU exposure.
# Both log() and run_logged() write to FD 3.
exec 3>/dev/null
if [[ -L "${TRANSFER_LOG}" ]]; then
  echo "[WARN] TRANSFER_LOG '${TRANSFER_LOG}' is a symlink; logging disabled (writes go to /dev/null)." >&2
elif ! { exec 3>>"${TRANSFER_LOG}"; } 2>/dev/null; then
  echo "[WARN] Could not open TRANSFER_LOG '${TRANSFER_LOG}' for writing; logging disabled (writes go to /dev/null)." >&2
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() {
  local msg="[$(date '+%Y-%m-%dT%H:%M:%S')] $*"
  echo "${msg}"
  if ! $DRY_RUN; then
    echo "${msg}" >&3 || true
  fi
}

# Run a command, streaming stdout+stderr to both the terminal and the transfer
# log (FD 3). A read loop is used rather than tee so that write errors to
# FD 3 (e.g. disk full after the log was opened) never affect the command's
# exit code; FD 3 write failures are silently ignored and the returned status
# always reflects only the command's outcome.
run_logged() {
  local cmd_rc _rl_line
  set +o pipefail
  "$@" 2>&1 | while IFS= read -r _rl_line || [[ -n "${_rl_line}" ]]; do
    printf '%s\n' "${_rl_line}"
    printf '%s\n' "${_rl_line}" >&3 2>/dev/null || true
  done
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
        -- "${file}" "${DEST}"; then
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

# Shell-quote a string for use inside a POSIX /bin/sh command string.
# This function runs locally in bash; the *output* it produces — a single-quoted
# string with embedded single quotes escaped as '\'' — is what must be safe
# for a remote /bin/sh to interpret.  This avoids printf '%q', which can emit
# bash-specific $'...' quoting that /bin/sh on the remote side may not support.
_posix_sq() {
  # Wrap a string in single quotes, escaping any embedded single quotes as '\''.
  # Use a variable for the quote character to avoid backslash/glob-pattern
  # ambiguity that arises when writing \' inside a bash ${...} expansion.
  local _sq="'"
  local s="${1//${_sq}/${_sq}\\${_sq}${_sq}}"
  printf "'%s'" "${s}"
}

# Global handle for the current snapshot temp file so that the EXIT trap can
# remove it even when the script is interrupted mid-sync (INT/TERM both trigger
# exit, which then runs the EXIT trap).
_current_snap_file=""
_cleanup_snap() {
  if [[ -n "${_current_snap_file}" ]]; then
    rm -f -- "${_current_snap_file}" 2>/dev/null || true
  fi
}
trap '_cleanup_snap' EXIT
trap 'exit 130' INT   # cleanup via EXIT trap; 128+SIGINT(2)
trap 'exit 143' TERM  # cleanup via EXIT trap; 128+SIGTERM(15)

sync_db() {
  if [[ -z "${DB_DEST}" ]]; then
    return 0
  fi
  # Dry-run: log the intended operations without any filesystem or SQLite work.
  # The primary path creates a consistent sqlite3 .backup snapshot, rsyncs it
  # with --partial-dir=.rsync-tmp --delay-updates --timeout=30 (atomic at-dest),
  # and then deletes (not rsyncs) stale -wal/-shm sidecars at the destination.
  # The fallback (sqlite3 unavailable or mktemp failed) rsyncs the live DB and
  # its sidecar files directly with the same rsync flags.
  if $DRY_RUN; then
    log "[dry-run] sqlite3 -- '${DB_SOURCE}' '.backup <snapshot>'"
    log "[dry-run] rsync -az --bwlimit=${BW_KBPS} --partial-dir=.rsync-tmp --delay-updates --timeout=30 -- <snapshot> '${DB_DEST}'"
    # Log the correct WAL/SHM cleanup form (ssh for remote, local rm otherwise).
    if [[ "${DB_DEST}" =~ ^([^/@]*@)?\[([^]]*)\]:(.*)$ ]] || \
       { [[ "${DB_DEST}" == *:* ]] && [[ "${DB_DEST%%:*}" != */* ]]; }; then
      log "[dry-run] ssh <host> 'rm -f -- <dest_path>-wal <dest_path>-shm'  # remove stale WAL/SHM at destination"
    else
      log "[dry-run] rm -f -- '${DB_DEST}-wal' '${DB_DEST}-shm'  # remove stale WAL/SHM at destination"
    fi
    log "[dry-run] # fallback (sqlite3 unavailable): rsync -az --bwlimit=${BW_KBPS} --partial-dir=.rsync-tmp --delay-updates --timeout=30 -- '${DB_SOURCE}' '${DB_DEST}'"
    return 0
  fi
  if [[ ! -f "${DB_SOURCE}" ]]; then
    log "WARN DB file not found, skipping DB sync: ${DB_SOURCE}"
    return 1
  fi

  # For remote DB_DEST, verify that the remote path is not an existing directory.
  # rsync (without a trailing /) would copy the snapshot into the directory under
  # the snapshot temp file's basename rather than to the intended file path, and
  # the WAL/SHM cleanup logic would then target wrong paths like
  # "${DB_DEST}-wal".  This mirrors the local -d guard in the startup validation
  # block.  SSH errors (host unreachable etc.) are ignored so a transient
  # connectivity problem does not prevent other transfers from running.
  local _chk_is_remote=false _chk_host="" _chk_path=""
  if [[ "${DB_DEST}" =~ ^([^/@]*@)?\[([^]]*)\]:(.*)$ ]]; then
    _chk_host="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
    _chk_path="${BASH_REMATCH[3]}"
    _chk_is_remote=true
  elif [[ "${DB_DEST}" == *:* && "${DB_DEST%%:*}" != */* ]]; then
    _chk_host="${DB_DEST%%:*}"
    _chk_path="${DB_DEST#*:}"
    _chk_is_remote=true
  fi
  if $_chk_is_remote; then
    if ssh -o BatchMode=yes -o ConnectTimeout=10 \
           -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
           -- "${_chk_host}" \
           "test -d $(_posix_sq "${_chk_path}")" 2>/dev/null; then
      log "ERROR DB_DEST '${DB_DEST}' is a remote directory; a full file path is required"
      log "      Example: DB_DEST=rf_worker@<brian_host>:/var/lib/rf-adapt-intel/rf_adapt_intel.db"
      return 1
    fi
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
      _current_snap_file="${snap_file}"  # register for EXIT/signal cleanup
      # Use the SQLite online backup API (.backup): avoids a full DB rewrite and
      # holds only brief shared locks, causing minimal disruption to concurrent
      # worker writes.  Unlike VACUUM INTO, .backup can write to an existing file.
      # Use SQL-style single-quote escaping (double each embedded ') so sqlite3's
      # shell tokenizer correctly handles paths with single quotes or spaces.
      # Do NOT use _posix_sq here: that function produces shell quoting for remote
      # /bin/sh commands and its \'...'\'' escaping is not understood by sqlite3.
      local _snap_sql_esc="${snap_file//"'"/"''"}"
      if ! run_logged sqlite3 -- "${DB_SOURCE}" ".backup '${_snap_sql_esc}'"; then
        log "WARN sqlite3 .backup failed; falling back to live-file rsync"
        [[ -n "${snap_file}" ]] && rm -f -- "${snap_file}"
        _current_snap_file=""
        snap_file=""
      else
        # mktemp creates files with mode 0600; copy source DB permissions so
        # rsync's -a doesn't propagate restrictive permissions to the destination
        # and break reads by other users on Brian.  Fall back to stat-derived
        # source mode if --reference is unavailable (non-GNU chmod).
        if ! chmod --reference="${DB_SOURCE}" "${snap_file}" 2>/dev/null; then
          local _src_mode
          _src_mode="$(stat -c '%a' "${DB_SOURCE}" 2>/dev/null)"
          if [[ -n "${_src_mode}" ]]; then
            log "WARN chmod --reference failed; applying source mode ${_src_mode} from stat"
            if ! chmod "${_src_mode}" "${snap_file}" 2>/dev/null; then
              log "WARN chmod ${_src_mode} also failed; snapshot may reach destination at mode 0600"
            fi
          else
            log "WARN chmod --reference failed and stat could not read source mode; snapshot may reach destination at mode 0600"
          fi
        fi
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
      run_logged rsync -az --bwlimit="${BW_KBPS}" --partial-dir=.rsync-tmp \
          --delay-updates --timeout=30 -- "${snap_file}" "${DB_DEST}" || ok=false
      # After a successful snapshot transfer, remove any stale WAL/SHM at the
      # destination.  If those sidecars are left from a previous live-file sync,
      # SQLite on the receiver would apply the old WAL data to the new consistent
      # snapshot, producing corrupt or inconsistent reads.
      # Cleanup failure marks the sync as failed so the retry loop can attempt
      # the full transfer again (including cleanup) rather than reporting success
      # with stale sidecars still in place.
      if $ok; then
        # Determine whether DB_DEST is remote.  Support both the standard
        # [user@]host:/path form and the IPv6 bracket form [user@][addr]:/path.
        # A colon that belongs to an IPv6 address is inside [...], so we check
        # for the bracket form first to avoid splitting on the wrong colon.
        local _db_dest_host _db_dest_path _is_remote=false
        if [[ "${DB_DEST}" =~ ^([^/@]*@)?\[([^]]*)\]:(.*)$ ]]; then
          # IPv6 bracket form: [user@][addr]:/path.  ssh expects the bare
          # address (no brackets), so strip them here for the ssh invocation.
          _db_dest_host="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
          _db_dest_path="${BASH_REMATCH[3]}"
          _is_remote=true
        elif [[ "${DB_DEST}" == *:* && "${DB_DEST%%:*}" != */* ]]; then
          # Standard form: [user@]hostname:/path (colon present, no slash before it)
          _db_dest_host="${DB_DEST%%:*}"
          _db_dest_path="${DB_DEST#*:}"
          _is_remote=true
        fi
        if $_is_remote; then
          # Use POSIX-portable single-quote escaping (not printf %q, which can
          # emit bash-specific $'...' syntax that fails on remote /bin/sh).
          local _wal_q _shm_q _ssh_err
          _wal_q="$(_posix_sq "${_db_dest_path}-wal")"
          _shm_q="$(_posix_sq "${_db_dest_path}-shm")"
          _ssh_err="$(ssh -o BatchMode=yes \
              -o ConnectTimeout=10 \
              -o ServerAliveInterval=5 \
              -o ServerAliveCountMax=2 \
              -- "${_db_dest_host}" \
              "rm -f -- ${_wal_q} ${_shm_q}" 2>&1)" || {
            log "WARN could not remove stale WAL/SHM at destination; marking sync as failed${_ssh_err:+: ${_ssh_err}}"
            ok=false
          }
        else
          rm -f -- "${DB_DEST}-wal" "${DB_DEST}-shm" 2>/dev/null || {
            log "WARN could not remove stale WAL/SHM at destination; marking sync as failed"
            ok=false
          }
        fi
      fi
    else
      # Fallback: sync live DB and sidecars (racy but better than nothing).
      run_logged rsync -az --bwlimit="${BW_KBPS}" --partial-dir=.rsync-tmp \
          --delay-updates --timeout=30 -- "${DB_SOURCE}" "${DB_DEST}" || ok=false
      if $ok && [[ -f "${DB_SOURCE}-wal" ]]; then
        run_logged rsync -az --bwlimit="${BW_KBPS}" --partial-dir=.rsync-tmp \
            --delay-updates --timeout=30 -- "${DB_SOURCE}-wal" "${DB_DEST}-wal" || ok=false
      fi
      if $ok && [[ -f "${DB_SOURCE}-shm" ]]; then
        run_logged rsync -az --bwlimit="${BW_KBPS}" --partial-dir=.rsync-tmp \
            --delay-updates --timeout=30 -- "${DB_SOURCE}-shm" "${DB_DEST}-shm" || ok=false
      fi
      # Remove any stale destination sidecars whose source counterpart no longer
      # exists (e.g. WAL checkpointed and deleted by the writer).  Without this,
      # SQLite on the receiver could apply old WAL data to the freshly-copied DB
      # and produce inconsistent reads.  Failures here are treated as non-fatal
      # warnings so a transient cleanup error doesn't abort the transfer.
      if $ok; then
        local _fb_is_remote=false _fb_host="" _fb_path=""
        if [[ "${DB_DEST}" =~ ^([^/@]*@)?\[([^]]*)\]:(.*)$ ]]; then
          _fb_host="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
          _fb_path="${BASH_REMATCH[3]}"
          _fb_is_remote=true
        elif [[ "${DB_DEST}" == *:* && "${DB_DEST%%:*}" != */* ]]; then
          _fb_host="${DB_DEST%%:*}"
          _fb_path="${DB_DEST#*:}"
          _fb_is_remote=true
        else
          _fb_path="${DB_DEST}"
        fi
        local -a _fb_stale=()
        [[ ! -f "${DB_SOURCE}-wal" ]] && _fb_stale+=("${_fb_path}-wal")
        [[ ! -f "${DB_SOURCE}-shm" ]] && _fb_stale+=("${_fb_path}-shm")
        if [[ ${#_fb_stale[@]} -gt 0 ]]; then
          if $_fb_is_remote; then
            local _fb_rm_q="" _fb_p
            for _fb_p in "${_fb_stale[@]}"; do
              _fb_rm_q="${_fb_rm_q} $(_posix_sq "${_fb_p}")"
            done
            ssh -o BatchMode=yes -o ConnectTimeout=10 \
                -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
                -- "${_fb_host}" "rm -f --${_fb_rm_q}" 2>/dev/null || \
              log "WARN could not remove stale WAL/SHM at fallback destination (non-fatal)"
          else
            rm -f -- "${_fb_stale[@]}" 2>/dev/null || \
              log "WARN could not remove stale WAL/SHM at fallback destination (non-fatal)"
          fi
        fi
      fi
    fi
    if $ok; then
      log "OK DB synced: $(basename "${DB_SOURCE}") -> ${DB_DEST}"
      [[ -n "${snap_file}" ]] && rm -f -- "${snap_file}"
      _current_snap_file=""
      return 0
    fi
    log "WARN DB sync attempt ${attempt}/${MAX_RETRIES} failed"
    if [[ "${attempt}" -lt "${MAX_RETRIES}" ]]; then
      sleep $(( attempt * 5 ))
    fi
    attempt=$(( attempt + 1 ))
  done
  [[ -n "${snap_file}" ]] && rm -f -- "${snap_file}"
  _current_snap_file=""
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
    # Only update the timestamp on success so a transient failure (network
    # outage, disk-full) allows a retry on the next IQ file event rather than
    # suppressing further attempts for a full DB_SYNC_INTERVAL.
    if sync_db; then
      _last_db_sync=${now}
    fi
  fi
  return 0
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
  # Only record the sync timestamp on success, matching sync_db_if_due behaviour:
  # a failed initial sync lets watch mode retry on the next file event rather
  # than suppressing attempts for a full DB_SYNC_INTERVAL.
  if sync_db; then
    _last_db_sync=$(date +%s)
  fi
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
  # Run inotifywait as a coprocess so:
  #  1. the while loop runs in this shell and shares _last_db_sync state
  #     with sync_db_if_due (unlike the former pipe-into-while form which ran
  #     in a subshell and could not update the caller's variables), and
  #  2. we can detect unexpected termination (inotify queue overflow, missing
  #     directory, permission error) via the coprocess exit status after the loop.
  # --format '%w%f' gives full path; -e close_write triggers after file is done.
  coproc _IQ_WATCH (inotifywait -m -r -e close_write --format '%w%f' "${SOURCE_DIR}" 2>&3)
  local _inotify_pid="${_IQ_WATCH_PID}"
  while IFS= read -r new_file <&"${_IQ_WATCH[0]}"; do
    case "${new_file}" in
      *.cf32|*.raw)
        log "New file detected: $(basename "${new_file}")"
        run_rsync "${new_file}" || true
        sync_db_if_due
        ;;
    esac
  done
  # Reap the coprocess and check its exit status.  SIGINT (exit 130) and SIGTERM
  # (exit 143) are expected; any other non-zero exit means inotifywait terminated
  # due to an error (e.g., inotify queue overflow, source directory removed).
  local _inotify_rc=0
  wait "${_inotify_pid}" 2>/dev/null || _inotify_rc=$?
  if [[ ${_inotify_rc} -ne 0 && ${_inotify_rc} -ne 130 && ${_inotify_rc} -ne 143 ]]; then
    log "ERROR inotifywait terminated unexpectedly (exit code ${_inotify_rc}); stopping watcher" >&2
    exit 1
  fi
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
