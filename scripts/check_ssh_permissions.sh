#!/usr/bin/env bash
# scripts/check_ssh_permissions.sh — Check and fix SSH permissions for the
# rf_worker service account so the iq-transfer-watcher (rsync watcher) can
# authenticate to the remote Brian host.
#
# Why this script exists
# ----------------------
# iq-transfer-watcher.service runs as rf_worker with ProtectHome=yes, which
# makes home-directory paths inaccessible.  The service sets
# HOME=/var/lib/rf-adapt-intel so that SSH uses
# /var/lib/rf-adapt-intel/.ssh/ for keys and known_hosts — a path that is
# already writable under ReadWritePaths.  This script creates/repairs that
# directory and its contents.
#
# Usage:
#   sudo bash scripts/check_ssh_permissions.sh [OPTIONS]
#
# Options:
#   --fix          Automatically create missing items and repair permissions.
#                  Without this flag the script only reports (check mode).
#   --dry-run      Report what would change without making any modifications.
#   --host HOST    Scan HOST with ssh-keyscan and append its host key to
#                  known_hosts. Implies --fix (triggers full auto-repair so
#                  the SSH directory and key pair are ready before scanning).
#                  Can be repeated: --host h1 --host h2
#   --key-type TYPE  Key type to generate: ed25519 (default) or rsa.
#   --help         Show this help message.
#
# Examples:
#   sudo bash scripts/check_ssh_permissions.sh
#   sudo bash scripts/check_ssh_permissions.sh --fix
#   sudo bash scripts/check_ssh_permissions.sh --fix --host brian.local
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults — each may be overridden by environment variables to allow
# testing without root and without touching real system paths.
# ---------------------------------------------------------------------------
SERVICE_USER="${SERVICE_USER:-rf_worker}"
SSH_BASE="${SSH_BASE:-/var/lib/rf-adapt-intel}"
SSH_DIR="${SSH_BASE}/.ssh"
KNOWN_HOSTS="${SSH_DIR}/known_hosts"
SSH_KEY="${SSH_DIR}/id_ed25519"
KEY_TYPE="ed25519"
FIX=false
DRY_RUN=false
SCAN_HOSTS=()

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --fix)        FIX=true ;;
    --dry-run)    DRY_RUN=true ;;
    --host)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "[ERROR] --host requires a hostname argument." >&2
        exit 1
      fi
      SCAN_HOSTS+=("$2"); shift ;;
    --key-type)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "[ERROR] --key-type requires a type argument (ed25519 or rsa)." >&2
        exit 1
      fi
      KEY_TYPE="$2"
      shift ;;
    -h|--help)
      sed -n '2,/^set -/{ /^set -/d; s/^# \{0,1\}//; p }' "${BASH_SOURCE[0]}"
      exit 0 ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      exit 1 ;;
  esac
  shift
done

# Validate key type
if [[ "${KEY_TYPE}" != "ed25519" && "${KEY_TYPE}" != "rsa" ]]; then
  echo "[ERROR] --key-type must be 'ed25519' or 'rsa'." >&2
  exit 1
fi

SSH_KEY="${SSH_DIR}/id_${KEY_TYPE}"

# --host implies full --fix so that the .ssh directory, key pair, and
# known_hosts are all provisioned before the keyscan runs.
# --dry-run also enables FIX so that fix code paths are entered and print
# "[dry-run] Would: ..." instead of performing actual changes.
if [[ ${#SCAN_HOSTS[@]} -gt 0 ]] || $DRY_RUN; then
  FIX=true
fi

# Precompute hostname for SSH key comment so it is expanded once in the outer
# shell and can be safely embedded in bash -c strings without relying on
# command substitution inside single-quoted arguments.
_KEY_COMMENT_HOST="$(hostname -s 2>/dev/null || echo localhost)"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_PASS=0
_FAIL=0
_FIXED=0
# _UNFIXED tracks failures that have not yet been repaired.  It is incremented
# by fail() and decremented by fixed() / run_fix().  Unlike the derived
# expression (_FAIL - _FIXED), it is never affected by optional actions (e.g.
# host-key additions) that do not correspond to a previously recorded [FAIL].
_UNFIXED=0

pass()  { echo "  [PASS] $*"; _PASS=$(( _PASS + 1 )); }
fail()  { echo "  [FAIL] $*" >&2; _FAIL=$(( _FAIL + 1 )); _UNFIXED=$(( _UNFIXED + 1 )); }
fixed() { echo "  [FIXED] $*"; _FIXED=$(( _FIXED + 1 )); _UNFIXED=$(( _UNFIXED - 1 )); }
info()  { echo "  [INFO] $*"; }

# run_fix <description> <cmd...>
# If --dry-run: print what would happen.  If --fix: run and report.
run_fix() {
  local desc="$1"; shift
  if $DRY_RUN; then
    echo "  [dry-run] Would: ${desc}"
    # Only increment _FIXED (tracks 'would-fix' count shown in dry-run summary).
    # Do NOT decrement _UNFIXED: no actual repair occurred, and mutating _UNFIXED
    # in dry-run makes counter semantics inconsistent with the non-dry-run path
    # (where _UNFIXED is only decremented after a repair actually succeeds).
    _FIXED=$(( _FIXED + 1 ))
  else
    "$@"
    fixed "${desc}"
  fi
}

# ---------------------------------------------------------------------------
# Pre-flight: must run as root (so we can chown and switch user)
# Test-only overrides (never set in production):
#   _RF_TEST_NO_ROOT=1      – skip the root check entirely
#   _RF_TEST_EUID=<uid>     – treat <uid> as the effective UID instead of
#                             $EUID, so the check works correctly when the
#                             test runner itself is already root (e.g. sudo).
# ---------------------------------------------------------------------------
if [[ -z "${_RF_TEST_NO_ROOT:-}" ]] && [[ "${_RF_TEST_EUID:-${EUID:-$(id -u)}}" -ne 0 ]]; then
  echo "[ERROR] This script must be run as root (sudo bash $0)." >&2
  exit 1
fi

echo "=== SSH permissions check for ${SERVICE_USER} ==="
echo "    SSH directory : ${SSH_DIR}"
echo "    Key           : ${SSH_KEY}"
echo "    Known hosts   : ${KNOWN_HOSTS}"
echo ""

# ---------------------------------------------------------------------------
# Check 1: service user exists
# ---------------------------------------------------------------------------
echo "--- User ---"
if id "${SERVICE_USER}" &>/dev/null; then
  pass "user '${SERVICE_USER}' exists"
else
  fail "user '${SERVICE_USER}' does not exist — run 'sudo bash ops/deploy.sh' first"
  echo ""
  echo "Cannot continue: user ${SERVICE_USER} is required." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Check 2: SSH base directory (/var/lib/rf-adapt-intel) is writable by user
# ---------------------------------------------------------------------------
echo ""
echo "--- Base directory ---"
if [[ -d "${SSH_BASE}" ]]; then
  pass "${SSH_BASE} exists"
  local_owner="$(stat -c '%U' "${SSH_BASE}" 2>/dev/null || echo unknown)"
  if [[ "${local_owner}" == "${SERVICE_USER}" ]]; then
    pass "${SSH_BASE} owned by ${SERVICE_USER}"
  else
    fail "${SSH_BASE} owned by '${local_owner}', expected '${SERVICE_USER}'"
    if $FIX; then
      run_fix "chown ${SERVICE_USER}:${SERVICE_USER} ${SSH_BASE}" \
        chown "${SERVICE_USER}:${SERVICE_USER}" "${SSH_BASE}"
    fi
  fi
  # Verify actual write access (ownership alone is insufficient if mode bits
  # or ACLs restrict access). This test has no side effects so it runs even
  # in --dry-run mode; only the repair step is gated behind DRY_RUN.
  if sudo -u "${SERVICE_USER}" test -w "${SSH_BASE}" 2>/dev/null; then
    pass "${SSH_BASE} is writable by ${SERVICE_USER}"
  else
    fail "${SSH_BASE} is not writable by ${SERVICE_USER} — check mode bits"
    if $FIX; then
      run_fix "chown ${SERVICE_USER}:${SERVICE_USER} and chmod 0750 on ${SSH_BASE}" \
        bash -c 'chown "$1:$1" "$2" && chmod 0750 "$2"' _ "${SERVICE_USER}" "${SSH_BASE}"
    fi
  fi
else
  fail "${SSH_BASE} does not exist — run 'sudo bash ops/deploy.sh' first"
  echo ""
  echo "Cannot continue: ${SSH_BASE} is required." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Check 3: .ssh directory
# ---------------------------------------------------------------------------
echo ""
echo "--- .ssh directory (${SSH_DIR}) ---"
# Reject symlinks: running privileged chown/chmod on a symlink would follow
# it and potentially affect a path outside the intended tree.
if [[ -L "${SSH_DIR}" ]]; then
  fail "${SSH_DIR} is a symlink — this is a security risk; refusing to operate on it"
  if $FIX; then
    run_fix "remove symlink ${SSH_DIR} and create real directory (mode 700, owner ${SERVICE_USER})" \
      bash -c 'rm -f "$1" && \
               mkdir -p "$1" && \
               chown "$2:$2" "$1" && \
               chmod 700 "$1"' _ "${SSH_DIR}" "${SERVICE_USER}"
  fi
elif [[ -d "${SSH_DIR}" ]]; then
  pass "${SSH_DIR} exists (regular directory)"

  ssh_dir_mode="$(stat -c '%a' "${SSH_DIR}" 2>/dev/null || echo unknown)"
  ssh_dir_owner="$(stat -c '%U' "${SSH_DIR}" 2>/dev/null || echo unknown)"

  if [[ "${ssh_dir_owner}" == "${SERVICE_USER}" ]]; then
    pass "${SSH_DIR} owned by ${SERVICE_USER}"
  else
    fail "${SSH_DIR} owned by '${ssh_dir_owner}', expected '${SERVICE_USER}'"
    if $FIX; then
      run_fix "chown ${SERVICE_USER}:${SERVICE_USER} ${SSH_DIR}" \
        chown "${SERVICE_USER}:${SERVICE_USER}" "${SSH_DIR}"
    fi
  fi

  if [[ "${ssh_dir_mode}" == "700" ]]; then
    pass "${SSH_DIR} mode is 700"
  else
    fail "${SSH_DIR} mode is ${ssh_dir_mode}, expected 700"
    if $FIX; then
      run_fix "chmod 700 ${SSH_DIR}" chmod 700 "${SSH_DIR}"
    fi
  fi
else
  # SSH_DIR doesn't exist as a symlink or a directory.  If something else is
  # squatting on the path (regular file, device node, etc.) mkdir -p would
  # fail with an unhelpful error.  Detect and handle it explicitly.
  if [[ -e "${SSH_DIR}" ]]; then
    fail "${SSH_DIR} exists but is not a directory (unexpected file type)"
    if $FIX; then
      # rm -f is safe here: SSH_DIR is verified not to be a symlink (-L) or
      # directory (-d), so it is a regular file or special node that rm -f
      # can remove without recursion.
      run_fix "remove ${SSH_DIR} and create real directory (mode 700, owner ${SERVICE_USER})" \
        bash -c 'rm -f "$1" && \
                 mkdir -p "$1" && \
                 chown "$2:$2" "$1" && \
                 chmod 700 "$1"' _ "${SSH_DIR}" "${SERVICE_USER}"
    fi
  else
    fail "${SSH_DIR} does not exist"
    if $FIX; then
      run_fix "mkdir -p ${SSH_DIR} (mode 700, owner ${SERVICE_USER})" \
        bash -c 'mkdir -p "$1" && chown "$2:$2" "$1" && chmod 700 "$1"' _ "${SSH_DIR}" "${SERVICE_USER}"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Check 4: known_hosts file (may not exist yet — that is acceptable)
# ---------------------------------------------------------------------------
echo ""
echo "--- known_hosts ---"
# Check -L before -f: bash's -f follows symlinks, so a symlink to a regular
# file passes -f.  Guard the symlink case first.
if [[ -L "${KNOWN_HOSTS}" ]]; then
  fail "${KNOWN_HOSTS} is a symlink — this is a security risk"
  if $FIX; then
    # Atomic replace: write to a root-owned temp in the same dir (same
    # filesystem) so mv(1) uses rename(2).  rename(2) replaces the symlink
    # entry atomically, never following it to the target.
    run_fix "remove symlink ${KNOWN_HOSTS} and create real file" \
      bash -c "_tmp=\$(mktemp '${SSH_DIR}/known_hosts.XXXXXX') && \
               trap 'rm -f \"\${_tmp}\"' EXIT && \
               chmod 600 \"\${_tmp}\" && \
               chown '${SERVICE_USER}:${SERVICE_USER}' \"\${_tmp}\" && \
               mv -f \"\${_tmp}\" '${KNOWN_HOSTS}'"
  fi
elif [[ -f "${KNOWN_HOSTS}" ]]; then
  pass "${KNOWN_HOSTS} exists (regular file)"

  kh_owner="$(stat -c '%U' "${KNOWN_HOSTS}" 2>/dev/null || echo unknown)"
  kh_mode="$(stat -c '%a' "${KNOWN_HOSTS}" 2>/dev/null || echo unknown)"

  if [[ "${kh_owner}" == "${SERVICE_USER}" ]]; then
    pass "${KNOWN_HOSTS} owned by ${SERVICE_USER}"
  else
    fail "${KNOWN_HOSTS} owned by '${kh_owner}', expected '${SERVICE_USER}'"
    if $FIX; then
      run_fix "chown ${SERVICE_USER}:${SERVICE_USER} ${KNOWN_HOSTS}" \
        chown "${SERVICE_USER}:${SERVICE_USER}" "${KNOWN_HOSTS}"
    fi
  fi

  if [[ "${kh_mode}" == "600" ]]; then
    pass "${KNOWN_HOSTS} mode is 600"
  else
    fail "${KNOWN_HOSTS} mode is ${kh_mode}, expected 600"
    if $FIX; then
      run_fix "chmod 600 ${KNOWN_HOSTS}" chmod 600 "${KNOWN_HOSTS}"
    fi
  fi
elif [[ -e "${KNOWN_HOSTS}" ]]; then
  # Exists but is not a regular file (could be a directory, device node, etc.).
  # chmod and append operations would behave unexpectedly on such a path.
  fail "${KNOWN_HOSTS} exists but is not a regular file (directory or special file)"
  if $FIX; then
    # rm -rf is intentional: KNOWN_HOSTS was verified to not be a symlink
    # or regular file, so it is a directory or special node that requires -r.
    # After removal, use the same atomic temp-file pattern to avoid a TOCTOU
    # window where the service user could plant a symlink before the new file
    # is written.
    run_fix "remove ${KNOWN_HOSTS} and create regular file (mode 600, owner ${SERVICE_USER})" \
      bash -c "rm -rf '${KNOWN_HOSTS}' && \
               _tmp=\$(mktemp '${SSH_DIR}/known_hosts.XXXXXX') && \
               trap 'rm -f \"\${_tmp}\"' EXIT && \
               chmod 600 \"\${_tmp}\" && \
               chown '${SERVICE_USER}:${SERVICE_USER}' \"\${_tmp}\" && \
               mv -f \"\${_tmp}\" '${KNOWN_HOSTS}'"
  fi
else
  info "${KNOWN_HOSTS} does not exist yet (will be created by SSH on first connect)"
  if $FIX && ! $DRY_RUN; then
    # This is an optional hardening step, not a fix for a recorded failure, so
    # intentionally avoid run_fix here to keep the failure/fix counters accurate.
    # Atomic replace via temp file (same dir → same filesystem → rename(2) is
    # atomic).  This closes the TOCTOU window where the service user could
    # plant a symlink between the earlier -L check and the write.
    # DRY_RUN is excluded: .ssh dir isn't created in dry-run mode so mktemp
    # would fail on the non-existent parent directory.
    bash -c "_tmp=\$(mktemp '${SSH_DIR}/known_hosts.XXXXXX') && \
             trap 'rm -f \"\${_tmp}\"' EXIT && \
             chmod 600 \"\${_tmp}\" && \
             chown '${SERVICE_USER}:${SERVICE_USER}' \"\${_tmp}\" && \
             mv -f \"\${_tmp}\" '${KNOWN_HOSTS}'"
  fi
fi

# ---------------------------------------------------------------------------
# Check 5: SSH key pair
# ---------------------------------------------------------------------------
echo ""
echo "--- SSH key pair (${SSH_KEY}) ---"
# Check -L before -f: bash's -f follows symlinks, so a symlink to a regular
# file passes -f.  A symlink at the key path could redirect privileged
# chown/chmod operations to an unintended file.  Guard the symlink case first.
if [[ -L "${SSH_KEY}" ]]; then
  fail "${SSH_KEY} is a symlink — this is a security risk"
  if $FIX; then
    run_fix "remove symlink ${SSH_KEY} and regenerate ${KEY_TYPE} key pair" \
      bash -c "rm -f '${SSH_KEY}' '${SSH_KEY}.pub' && \
               sudo -u '${SERVICE_USER}' HOME='${SSH_BASE}' \
                 ssh-keygen -t '${KEY_TYPE}' -N '' \
                   -f '${SSH_KEY}' \
                   -C '${SERVICE_USER}@${_KEY_COMMENT_HOST}'"
  fi
elif [[ -f "${SSH_KEY}" ]]; then
  key_owner="$(stat -c '%U' "${SSH_KEY}" 2>/dev/null || echo unknown)"
  key_mode="$(stat -c '%a' "${SSH_KEY}" 2>/dev/null || echo unknown)"

  pass "${SSH_KEY} exists (regular file)"

  if [[ "${key_owner}" == "${SERVICE_USER}" ]]; then
    pass "${SSH_KEY} owned by ${SERVICE_USER}"
  else
    fail "${SSH_KEY} owned by '${key_owner}', expected '${SERVICE_USER}'"
    if $FIX; then
      # Only include .pub in chown if it is a real regular file (not symlink).
      # The symlink re-check happens inside bash -c so chown -h sees a
      # consistent view; -h / --no-dereference ensures we never follow a
      # symlink that was raced in after the outer [[ -L ]] guard.
      run_fix "chown -h ${SERVICE_USER}:${SERVICE_USER} ${SSH_KEY} [${SSH_KEY}.pub]" \
        bash -c "set -euo pipefail
                 paths=('${SSH_KEY}')
                 if [[ ! -L '${SSH_KEY}.pub' ]] && [[ -f '${SSH_KEY}.pub' ]]; then
                   paths+=('${SSH_KEY}.pub')
                 fi
                 chown -h '${SERVICE_USER}:${SERVICE_USER}' \"\${paths[@]}\""
    fi
  fi

  if [[ "${key_mode}" == "600" ]]; then
    pass "${SSH_KEY} mode is 600"
  else
    fail "${SSH_KEY} mode is ${key_mode}, expected 600"
    if $FIX; then
      run_fix "chmod 600 ${SSH_KEY}" chmod 600 "${SSH_KEY}"
    fi
  fi

  # Check .pub with symlink guard (same reasoning as for the private key).
  if [[ -L "${SSH_KEY}.pub" ]]; then
    fail "${SSH_KEY}.pub is a symlink — this is a security risk"
    if $FIX; then
      run_fix "remove symlink ${SSH_KEY}.pub and re-extract public key" \
        bash -c "rm -f '${SSH_KEY}.pub' && \
                 tmp_pub=\$(mktemp '${SSH_KEY}.pub.XXXXXX') && \
                 sudo -u '${SERVICE_USER}' HOME='${SSH_BASE}' \
                   ssh-keygen -y -f '${SSH_KEY}' > \"\${tmp_pub}\" && \
                 chown '${SERVICE_USER}:${SERVICE_USER}' \"\${tmp_pub}\" && \
                 chmod 644 \"\${tmp_pub}\" && \
                 mv -f \"\${tmp_pub}\" '${SSH_KEY}.pub'"
    fi
  elif [[ -f "${SSH_KEY}.pub" ]]; then
    pass "${SSH_KEY}.pub exists (regular file)"
  elif [[ -e "${SSH_KEY}.pub" ]]; then
    fail "${SSH_KEY}.pub exists but is not a regular file — unexpected file type"
    if $FIX; then
      run_fix "remove non-regular ${SSH_KEY}.pub and re-extract public key" \
        bash -c "rm -rf -- '${SSH_KEY}.pub' && \
                 tmp_pub=\$(mktemp '${SSH_KEY}.pub.XXXXXX') && \
                 sudo -u '${SERVICE_USER}' HOME='${SSH_BASE}' \
                   ssh-keygen -y -f '${SSH_KEY}' > \"\${tmp_pub}\" && \
                 chown '${SERVICE_USER}:${SERVICE_USER}' \"\${tmp_pub}\" && \
                 chmod 644 \"\${tmp_pub}\" && \
                 mv -f \"\${tmp_pub}\" '${SSH_KEY}.pub'"
    fi
  else
    fail "${SSH_KEY}.pub is missing"
    if $FIX; then
      run_fix "regenerate missing ${SSH_KEY}.pub from private key" \
        bash -c "tmp_pub=\$(mktemp '${SSH_KEY}.pub.XXXXXX') && \
                 sudo -u '${SERVICE_USER}' HOME='${SSH_BASE}' \
                   ssh-keygen -y -f '${SSH_KEY}' > \"\${tmp_pub}\" && \
                 chown '${SERVICE_USER}:${SERVICE_USER}' \"\${tmp_pub}\" && \
                 chmod 644 \"\${tmp_pub}\" && \
                 mv -f \"\${tmp_pub}\" '${SSH_KEY}.pub'"
    else
      info "Regenerate with: ssh-keygen -y -f ${SSH_KEY}"
    fi
  fi
elif [[ -e "${SSH_KEY}" ]]; then
  fail "${SSH_KEY} exists but is not a regular file — unexpected file type"
  if $FIX; then
    run_fix "remove non-regular ${SSH_KEY} and regenerate ${KEY_TYPE} key pair" \
      bash -c "rm -rf -- '${SSH_KEY}' '${SSH_KEY}.pub' && \
               sudo -u '${SERVICE_USER}' HOME='${SSH_BASE}' \
                 ssh-keygen -t '${KEY_TYPE}' -N '' \
                   -f '${SSH_KEY}' \
                   -C '${SERVICE_USER}@${_KEY_COMMENT_HOST}'"
  fi
else
  fail "${SSH_KEY} does not exist — key pair missing"
  if $FIX; then
    # Ensure SSH_DIR exists before running ssh-keygen.  This is a prerequisite
    # step, not a repair of the recorded [FAIL] above, so it must NOT go through
    # run_fix() (which would decrement _UNFIXED and underflow the counter).
    # Check 3 already handles SSH_DIR creation; this is a safety net only.
    if [[ ! -d "${SSH_DIR}" ]] && ! $DRY_RUN; then
      bash -c "mkdir -p '${SSH_DIR}' && \
               chown '${SERVICE_USER}:${SERVICE_USER}' '${SSH_DIR}' && \
               chmod 700 '${SSH_DIR}'"
    fi
    if $DRY_RUN; then
      echo "  [dry-run] Would: generate ${KEY_TYPE} key pair as ${SERVICE_USER}"
      # Only increment _FIXED (dry-run 'would-fix' count); do NOT decrement
      # _UNFIXED because no actual repair occurred.
      _FIXED=$(( _FIXED + 1 ))
    else
      sudo -u "${SERVICE_USER}" \
        HOME="${SSH_BASE}" \
        ssh-keygen -t "${KEY_TYPE}" -N "" \
          -f "${SSH_KEY}" \
          -C "${SERVICE_USER}@${_KEY_COMMENT_HOST}"
      fixed "generated ${KEY_TYPE} key pair at ${SSH_KEY}"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Optional: pre-populate known_hosts via ssh-keyscan
# ---------------------------------------------------------------------------
if [[ ${#SCAN_HOSTS[@]} -gt 0 ]]; then
  echo ""
  echo "--- Pre-populating known_hosts ---"
  echo "  [WARN] Host keys are accepted on first contact (TOFU — trust on first use)."
  echo "         Verify the fingerprint below matches the expected host key before"
  echo "         relying on this entry for production use."
  if ! command -v ssh-keyscan &>/dev/null; then
    fail "ssh-keyscan not found; cannot pre-populate known_hosts for requested hosts: ${SCAN_HOSTS[*]}"
  else
    for _scan_host in "${SCAN_HOSTS[@]}"; do
      if [[ -z "${_scan_host}" ]]; then
        continue
      fi
      if $DRY_RUN; then
        echo "  [dry-run] Would: ssh-keyscan -H ${_scan_host} >> ${KNOWN_HOSTS}"
        # Do not increment _FIXED here: host-key additions are requested actions,
        # not repairs of recorded [FAIL] items; incrementing _FIXED would also
        # decrement _UNFIXED and could mask genuine unresolved failures.
        continue
      fi
      # Ensure known_hosts exists atomically before scanning.  Use the same
      # mktemp + rename pattern as the main check above to close the TOCTOU
      # window where SERVICE_USER could plant a symlink between the earlier -L
      # guard and the creation step.
      if [[ ! -e "${KNOWN_HOSTS}" ]]; then
        bash -c "_tmp=\$(mktemp '${SSH_DIR}/known_hosts.XXXXXX') && \
                 trap 'rm -f \"\${_tmp}\"' EXIT && \
                 chmod 600 \"\${_tmp}\" && \
                 chown '${SERVICE_USER}:${SERVICE_USER}' \"\${_tmp}\" && \
                 mv -f \"\${_tmp}\" '${KNOWN_HOSTS}'"
      fi
      # Re-check after creation: guard against TOCTOU where SERVICE_USER could
      # have planted a symlink between the check above and now.
      if [[ -L "${KNOWN_HOSTS}" ]]; then
        fail "TOCTOU: ${KNOWN_HOSTS} is a symlink; aborting keyscan for ${_scan_host}"
        continue
      fi
      # Avoid duplicate entries: only add if host key not already present.
      if ssh-keygen -F "${_scan_host}" -f "${KNOWN_HOSTS}" &>/dev/null; then
        info "Host key for '${_scan_host}' already in ${KNOWN_HOSTS}"
        pass "${_scan_host} already trusted"
      else
        local_scan=""
        if local_scan="$(ssh-keyscan -H -T 10 -- "${_scan_host}" 2>/dev/null)" && \
           [[ -n "${local_scan}" ]]; then
          # Atomic append via temp file + rename(2).  This avoids a TOCTOU
          # window where SERVICE_USER could replace ${KNOWN_HOSTS} with a
          # symlink between the -L guard above and the append.  mv -f uses
          # rename(2), which atomically replaces the directory entry without
          # following symlinks, preventing privilege escalation even if a
          # symlink is raced in.
          _tmp_kh="$(mktemp "${SSH_DIR}/known_hosts.XXXXXX")"
          # Cleanup trap: remove the temp file if any step below fails
          # before mv completes (set -e exits the script on error).
          trap 'rm -f "${_tmp_kh}"' EXIT
          # Read any existing known_hosts content without elevated privileges:
          # drop to SERVICE_USER for the read so a raced symlink to a
          # sensitive file (e.g. /etc/shadow) cannot be read as root.
          if [[ "$(id -u)" -eq 0 ]]; then
            sudo -u "${SERVICE_USER}" bash -c 'cat "$1" 2>/dev/null' \
              _ "${KNOWN_HOSTS}" > "${_tmp_kh}" || true
          else
            cat "${KNOWN_HOSTS}" 2>/dev/null > "${_tmp_kh}" || true
          fi
          printf '%s\n' "${local_scan}" >> "${_tmp_kh}"
          chmod 600 "${_tmp_kh}"
          chown "${SERVICE_USER}:${SERVICE_USER}" "${_tmp_kh}"
          mv -f "${_tmp_kh}" "${KNOWN_HOSTS}"
          trap - EXIT  # temp file renamed; no longer needs cleanup
          # Use info() not fixed(): adding a host key is a requested action,
          # not a repair of a recorded [FAIL]; calling fixed() would decrement
          # _UNFIXED and could mask genuine unresolved failures.
          info "added host key for '${_scan_host}' to ${KNOWN_HOSTS}"
          # Display the fingerprint of the just-added key so the operator can
          # verify it out-of-band.  Feed only the newly-scanned lines (not the
          # entire known_hosts file) to ssh-keygen -l to avoid showing
          # fingerprints for previously-trusted hosts.
          echo ""
          echo "  Host key fingerprint for '${_scan_host}' (verify before trusting):"
          ssh-keygen -l -f <(printf '%s\n' "${local_scan}") 2>/dev/null || \
            echo "  (fingerprint unavailable — verify manually with: ssh-keygen -l -f ${KNOWN_HOSTS})"
          echo ""
        else
          fail "ssh-keyscan could not reach '${_scan_host}'; add manually or ensure the host is reachable"
        fi
      fi
    done
  fi
fi

# ---------------------------------------------------------------------------
# Show public key (so the operator can copy it to the remote host)
# ---------------------------------------------------------------------------
echo ""
echo "--- Public key ---"
_pub_key_file="${SSH_KEY}.pub"
# Guard against symlinks before cat: -f follows symlinks so a symlink at the
# pubkey path would cause the script (running as root) to print an arbitrary
# file's contents.  Always check -L first.
if [[ -L "${_pub_key_file}" ]]; then
  info "Public key path ${_pub_key_file} is a symlink — skipping display."
  info "Run with --fix to remove the symlink and regenerate the key pair."
elif [[ -f "${_pub_key_file}" ]]; then
  info "${SERVICE_USER} public key (copy to Brian's authorized_keys):"
  echo ""
  cat "${_pub_key_file}"
  echo ""
  echo "  On Brian (decode-only node), run:"
  echo "    sudo -u ${SERVICE_USER} bash -c \"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys\" << 'PUBKEY'"
  cat "${_pub_key_file}"
  echo "PUBKEY"
else
  info "No public key file found at ${_pub_key_file}"
  info "Run with --fix to generate a key pair."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Summary ==="
echo "  Passed : ${_PASS}"
echo "  Failed : ${_FAIL}"
if $DRY_RUN; then
  echo "  Would fix: ${_FIXED}"
elif $FIX; then
  echo "  Fixed  : ${_FIXED}"
fi
echo ""

if [[ ${_FAIL} -eq 0 ]]; then
  echo "All checks passed."
else
  # Use _UNFIXED directly: it is decremented only when a previously-recorded
  # [FAIL] is successfully repaired, so it can never be affected by optional
  # actions (e.g. host-key additions) that do not correspond to a [FAIL].
  if $DRY_RUN; then
    if [[ ${_FIXED} -gt 0 ]]; then
      echo "Would fix ${_FIXED} issue(s) — re-run without --dry-run to apply."
    fi
    echo "${_FAIL} issue(s) detected. Re-run without --dry-run (and with --fix) to repair." >&2
    exit 1
  elif $FIX; then
    if [[ ${_FIXED} -gt 0 ]]; then
      echo "${_FIXED} issue(s) fixed."
    fi
    if [[ ${_UNFIXED} -gt 0 ]]; then
      echo "${_UNFIXED} issue(s) could not be fixed automatically — review output above." >&2
      exit 1
    fi
    # All detected issues were successfully repaired.
  else
    echo "${_FAIL} issue(s) detected. Re-run with --fix to repair automatically." >&2
    exit 1
  fi
fi
