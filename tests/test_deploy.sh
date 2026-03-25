#!/usr/bin/env bash
# tests/test_deploy.sh — Shell tests for the TOCTOU-hardened directory setup
# section of ops/deploy.sh.
#
# Run with:
#   bash tests/test_deploy.sh [-v]
#
# Tests are run entirely in /tmp and require no root, SDR hardware, or network
# access.  They exercise the symlink-rejection guard and the lock/create/unlock
# ordering of ops/deploy.sh by overriding _RF_DATA_DIR to a temporary
# directory and running the script in --dry-run mode (so the filesystem checks
# execute but privileged installs are only printed).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="${REPO_ROOT}/ops/deploy.sh"
VERBOSE=false
[[ "${1:-}" == "-v" ]] && VERBOSE=true

# ---------------------------------------------------------------------------
# Minimal test harness (mirrors tests/test_setup.sh)
# ---------------------------------------------------------------------------
_PASS=0
_FAIL=0

ok() {
  local desc="$1"
  _PASS=$(( _PASS + 1 ))
  $VERBOSE && echo "  [PASS] ${desc}" || true
}

fail() {
  local desc="$1"
  local detail="${2:-}"
  _FAIL=$(( _FAIL + 1 ))
  echo "  [FAIL] ${desc}" >&2
  [[ -n "$detail" ]] && echo "         ${detail}" >&2 || true
}

assert_contains() {
  local desc="$1"
  local needle="$2"
  local haystack="$3"
  if printf '%s\n' "${haystack}" | grep -qF -- "${needle}"; then
    ok "${desc}"
  else
    fail "${desc}" "Expected to find: '${needle}'"
    $VERBOSE && echo "--- actual output ---" && printf '%s\n' "${haystack}" && echo "---" || true
  fi
}

assert_exit() {
  local desc="$1"
  local expected="$2"
  local actual="$3"
  if [[ "$actual" -eq "$expected" ]]; then
    ok "${desc}"
  else
    fail "${desc}" "Expected exit ${expected}, got ${actual}"
  fi
}

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
_TMP=""

setup_env() {
  _TMP="$(mktemp -d /tmp/rf-deploy-test.XXXXXX)"
}

teardown_env() {
  rm -rf "${_TMP:-}"
}

# Run deploy.sh in --dry-run mode with _RF_DATA_DIR pointing to the test
# directory.  Combined stdout+stderr is returned; the caller captures it.
run_deploy_dry() {
  _RF_DATA_DIR="${_TMP}" bash "${DEPLOY}" --dry-run < /dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# deploy.sh must refuse to proceed when one of the data subdirectories
# (snapshots / incoming / processed) is a symlink, even in --dry-run mode.
# This validates the TOCTOU guard that no longer skips the check in dry-run.
test_symlink_at_subdir_rejected() {
  setup_env

  # Plant a symlink where 'snapshots' should be.
  ln -s /dev/null "${_TMP}/snapshots"

  local out rc=0
  out="$(run_deploy_dry)" || rc=$?

  teardown_env

  assert_exit "symlink at snapshots: exits non-zero" 1 "${rc}"
  assert_contains "symlink at snapshots: error message" "is a symlink" "${out}"
  assert_contains "symlink at snapshots: names the path" "snapshots" "${out}"
}

# deploy.sh must refuse when a non-directory, non-symlink file occupies a
# subdir slot (e.g. a regular file left behind by a failed prior run).
test_regular_file_at_subdir_rejected() {
  setup_env

  touch "${_TMP}/incoming"

  local out rc=0
  out="$(run_deploy_dry)" || rc=$?

  teardown_env

  assert_exit "regular file at incoming: exits non-zero" 1 "${rc}"
  assert_contains "regular file at incoming: error message" \
    "exists but is not a directory" "${out}"
}

# In the dry-run output, the chmod g+w line for the parent must appear AFTER
# the install -d lines for all three subdirectories (snapshots, incoming,
# processed).  This confirms the lock + create/verify + unlock ordering.
test_chmod_gw_follows_all_subdir_installs() {
  setup_env

  local out rc=0
  out="$(run_deploy_dry)" || rc=$?

  teardown_env

  assert_exit "clean dry-run exits 0" 0 "${rc}"

  # Verify each expected line is present in the output.
  assert_contains "dry-run: installs snapshots" "${_TMP}/snapshots" "${out}"
  assert_contains "dry-run: installs incoming"  "${_TMP}/incoming"  "${out}"
  assert_contains "dry-run: installs processed" "${_TMP}/processed" "${out}"
  assert_contains "dry-run: chmod g+w present"  "chmod g+w"         "${out}"

  # Verify ordering: line number of 'chmod g+w' > last 'install -d' line for all subdirs.
  local chmod_line snapshots_line incoming_line processed_line
  chmod_line="$(printf '%s\n' "${out}" | grep -n "chmod g+w" | tail -1 | cut -d: -f1 || true)"
  snapshots_line="$(printf '%s\n' "${out}" | grep -n "install -d.*snapshots" | tail -1 | cut -d: -f1 || true)"
  incoming_line="$(printf '%s\n' "${out}" | grep -n "install -d.*incoming" | tail -1 | cut -d: -f1 || true)"
  processed_line="$(printf '%s\n' "${out}" | grep -n "install -d.*processed" | tail -1 | cut -d: -f1 || true)"

  if [[ -n "${chmod_line}" && -n "${snapshots_line}" && -n "${incoming_line}" && -n "${processed_line}" \
        && "${chmod_line}" -gt "${snapshots_line}" \
        && "${chmod_line}" -gt "${incoming_line}" \
        && "${chmod_line}" -gt "${processed_line}" ]]; then
    ok "chmod g+w follows the last install -d for all subdirs"
  else
    fail "chmod g+w follows the last install -d for all subdirs" \
      "chmod g+w at line ${chmod_line:-?}, snapshots at line ${snapshots_line:-?}, incoming at line ${incoming_line:-?}, processed at line ${processed_line:-?}"
  fi
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
echo "=== tests/test_deploy.sh ==="
test_symlink_at_subdir_rejected
test_regular_file_at_subdir_rejected
test_chmod_gw_follows_all_subdir_installs

echo ""
echo "Results: ${_PASS} passed, ${_FAIL} failed."
if [[ $_FAIL -gt 0 ]]; then
  exit 1
fi
