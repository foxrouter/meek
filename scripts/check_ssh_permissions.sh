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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_PASS=0
_FAIL=0
_FIXED=0

pass() { echo "  [PASS] $*"; _PASS=$(( _PASS + 1 )); }
fail() { echo "  [FAIL] $*" >&2; _FAIL=$(( _FAIL + 1 )); }
fixed() { echo "  [FIXED] $*"; _FIXED=$(( _FIXED + 1 )); }
info() { echo "  [INFO] $*"; }

# run_fix <description> <cmd...>
# If --dry-run: print what would happen.  If --fix: run and report.
run_fix() {
  local desc="$1"; shift
  if $DRY_RUN; then
    echo "  [dry-run] Would: ${desc}"
    _FIXED=$(( _FIXED + 1 ))
  else
    "$@"
    fixed "${desc}"
  fi
}

# ---------------------------------------------------------------------------
# Pre-flight: must run as root (so we can chown and switch user)
# _RF_TEST_NO_ROOT may be set in unit-test environments to skip this check.
# ---------------------------------------------------------------------------
if [[ -z "${_RF_TEST_NO_ROOT:-}" ]] && [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
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
      run_fix "chmod 0750 ${SSH_BASE} + chown ${SERVICE_USER}:${SERVICE_USER}" \
        bash -c "chown '${SERVICE_USER}:${SERVICE_USER}' '${SSH_BASE}' && \
                 chmod 0750 '${SSH_BASE}'"
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
      bash -c "rm -f '${SSH_DIR}' && \
               mkdir -p '${SSH_DIR}' && \
               chown '${SERVICE_USER}:${SERVICE_USER}' '${SSH_DIR}' && \
               chmod 700 '${SSH_DIR}'"
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
        bash -c "rm -f '${SSH_DIR}' && \
                 mkdir -p '${SSH_DIR}' && \
                 chown '${SERVICE_USER}:${SERVICE_USER}' '${SSH_DIR}' && \
                 chmod 700 '${SSH_DIR}'"
    fi
  else
    fail "${SSH_DIR} does not exist"
    if $FIX; then
      run_fix "mkdir -p ${SSH_DIR} (mode 700, owner ${SERVICE_USER})" \
        bash -c "mkdir -p '${SSH_DIR}' && chown '${SERVICE_USER}:${SERVICE_USER}' '${SSH_DIR}' && chmod 700 '${SSH_DIR}'"
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
    run_fix "remove symlink ${KNOWN_HOSTS} and create real file" \
      bash -c "rm -f '${KNOWN_HOSTS}' && touch '${KNOWN_HOSTS}' && \
               chown '${SERVICE_USER}:${SERVICE_USER}' '${KNOWN_HOSTS}' && \
               chmod 600 '${KNOWN_HOSTS}'"
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
    run_fix "remove ${KNOWN_HOSTS} and create regular file (mode 600, owner ${SERVICE_USER})" \
      bash -c "rm -rf '${KNOWN_HOSTS}' && touch '${KNOWN_HOSTS}' && \
               chown '${SERVICE_USER}:${SERVICE_USER}' '${KNOWN_HOSTS}' && \
               chmod 600 '${KNOWN_HOSTS}'"
  fi
else
  info "${KNOWN_HOSTS} does not exist yet (will be created by SSH on first connect)"
  if $FIX; then
    run_fix "create ${KNOWN_HOSTS} (mode 600, owner ${SERVICE_USER})" \
      bash -c "touch '${KNOWN_HOSTS}' && \
               chown '${SERVICE_USER}:${SERVICE_USER}' '${KNOWN_HOSTS}' && \
               chmod 600 '${KNOWN_HOSTS}'"
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
                   -C '${SERVICE_USER}@$(hostname -s 2>/dev/null || echo localhost)'"
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
      # Only include .pub in chown if it exists as a regular file (not symlink).
      run_fix "chown ${SERVICE_USER}:${SERVICE_USER} ${SSH_KEY} ${SSH_KEY}.pub" \
        bash -c "chown '${SERVICE_USER}:${SERVICE_USER}' '${SSH_KEY}' \
                 $( [[ ! -L "${SSH_KEY}.pub" ]] && [[ -f "${SSH_KEY}.pub" ]] && echo \"'${SSH_KEY}.pub'\" || true )"
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
  else
    info "${SSH_KEY}.pub missing — regenerate with: ssh-keygen -y -f ${SSH_KEY}"
  fi
else
  fail "${SSH_KEY} does not exist — key pair missing"
  if $FIX; then
    if [[ ! -d "${SSH_DIR}" ]]; then
      run_fix "mkdir -p ${SSH_DIR} before key generation" \
        bash -c "mkdir -p '${SSH_DIR}' && \
                 chown '${SERVICE_USER}:${SERVICE_USER}' '${SSH_DIR}' && \
                 chmod 700 '${SSH_DIR}'"
    fi
    if $DRY_RUN; then
      echo "  [dry-run] Would: generate ${KEY_TYPE} key pair as ${SERVICE_USER}"
      _FIXED=$(( _FIXED + 1 ))
    else
      sudo -u "${SERVICE_USER}" \
        HOME="${SSH_BASE}" \
        ssh-keygen -t "${KEY_TYPE}" -N "" \
          -f "${SSH_KEY}" \
          -C "${SERVICE_USER}@$(hostname -s 2>/dev/null || echo localhost)"
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
        _FIXED=$(( _FIXED + 1 ))
        continue
      fi
      # Ensure known_hosts exists and is owned correctly before appending.
      if [[ ! -f "${KNOWN_HOSTS}" ]]; then
        touch "${KNOWN_HOSTS}"
        chown "${SERVICE_USER}:${SERVICE_USER}" "${KNOWN_HOSTS}"
        chmod 600 "${KNOWN_HOSTS}"
      fi
      # Avoid duplicate entries: only add if host key not already present.
      if ssh-keygen -F "${_scan_host}" -f "${KNOWN_HOSTS}" &>/dev/null; then
        info "Host key for '${_scan_host}' already in ${KNOWN_HOSTS}"
        pass "${_scan_host} already trusted"
      else
        local_scan=""
        if local_scan="$(ssh-keyscan -H -T 10 -- "${_scan_host}" 2>/dev/null)" && \
           [[ -n "${local_scan}" ]]; then
          printf '%s\n' "${local_scan}" >> "${KNOWN_HOSTS}"
          chown "${SERVICE_USER}:${SERVICE_USER}" "${KNOWN_HOSTS}"
          chmod 600 "${KNOWN_HOSTS}"
          fixed "added host key for '${_scan_host}' to ${KNOWN_HOSTS}"
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
  info "rf_worker public key (copy to Brian's authorized_keys):"
  echo ""
  cat "${_pub_key_file}"
  echo ""
  echo "  On Brian (decode-only node), run:"
  echo "    sudo -u rf_worker bash -c \"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys\" << 'PUBKEY'"
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
  if $DRY_RUN && [[ ${_FIXED} -gt 0 ]]; then
    echo "Would fix ${_FIXED} issue(s) — re-run without --dry-run to apply."
  elif $FIX && [[ ${_FIXED} -gt 0 ]]; then
    echo "${_FIXED} issue(s) fixed."
  fi
  if $DRY_RUN; then
    echo "${_FAIL} issue(s) detected. Re-run without --dry-run (and with --fix) to repair." >&2
  elif $FIX; then
    echo "${_FAIL} issue(s) could not be fixed automatically — review output above." >&2
  else
    echo "${_FAIL} issue(s) detected. Re-run with --fix to repair automatically." >&2
  fi
  exit 1
fi
