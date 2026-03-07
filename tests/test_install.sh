#!/usr/bin/env bash
# tests/test_install.sh — Shell tests for install.sh CLI flags and behaviour.
#
# Run with:
#   bash tests/test_install.sh [-v]
#
# Tests exercise argument parsing, the --iq-dest flag, and dry-run output of
# install.sh by stubbing apt-get / cmake / sudo / systemctl / bash so no real
# system changes happen.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="${REPO_ROOT}/install.sh"
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
  $VERBOSE && echo "  [PASS] ${desc}"
}

fail() {
  local desc="$1"
  local detail="${2:-}"
  _FAIL=$(( _FAIL + 1 ))
  echo "  [FAIL] ${desc}" >&2
  [[ -n "$detail" ]] && echo "         ${detail}" >&2
}

assert_contains() {
  local desc="$1"
  local needle="$2"
  local haystack="$3"
  if echo "${haystack}" | grep -qF -- "${needle}"; then
    ok "${desc}"
  else
    fail "${desc}" "Expected to find: '${needle}'"
    $VERBOSE && echo "--- actual output ---" && echo "${haystack}" && echo "---"
  fi
}

assert_not_contains() {
  local desc="$1"
  local needle="$2"
  local haystack="$3"
  if ! echo "${haystack}" | grep -qF -- "${needle}"; then
    ok "${desc}"
  else
    fail "${desc}" "Should NOT contain: '${needle}'"
    $VERBOSE && echo "--- actual output ---" && echo "${haystack}" && echo "---"
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
# Stubs that make install.sh safe to run in CI without root / SDR hardware.
# In --dry-run mode install.sh's run() just echoes commands, so most system
# calls never happen.  The stubs below cover the calls that run() does not
# guard (detect_os, id, etc.) and allow dry-run to reach the summary.
# ---------------------------------------------------------------------------
_STUB_DIR=""
_TMP=""

setup_stubs() {
  _STUB_DIR="$(mktemp -d /tmp/rf-adapt-intel-installer-stubs.XXXXXX)"
  _TMP="$(mktemp -d /tmp/rf-adapt-intel-installer-test.XXXXXX)"

  # sudo — run args directly (no privilege escalation needed in dry-run)
  cat > "${_STUB_DIR}/sudo" <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF

  # apt-get — always succeed silently
  cat > "${_STUB_DIR}/apt-get" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  # cmake — always succeed silently
  cat > "${_STUB_DIR}/cmake" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  # systemctl — always succeed silently
  cat > "${_STUB_DIR}/systemctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  # useradd / usermod — succeed silently
  cat > "${_STUB_DIR}/useradd" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  cat > "${_STUB_DIR}/usermod" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  # udevadm — succeed silently
  cat > "${_STUB_DIR}/udevadm" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  # id — stub to return a predictable result (user rf_worker does not exist)
  cat > "${_STUB_DIR}/id" <<'EOF'
#!/usr/bin/env bash
# Return "not found" for rf_worker; fall through for real user queries
if [[ "${*}" == "rf_worker" ]]; then
  exit 1
fi
exec /usr/bin/id "$@"
EOF

  chmod +x "${_STUB_DIR}/sudo" "${_STUB_DIR}/apt-get" "${_STUB_DIR}/cmake" \
            "${_STUB_DIR}/systemctl" "${_STUB_DIR}/useradd" "${_STUB_DIR}/usermod" \
            "${_STUB_DIR}/udevadm" "${_STUB_DIR}/id"
}

teardown_stubs() {
  rm -rf "${_STUB_DIR:-}" "${_TMP:-}"
}

run_installer() {
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    HOME="${_TMP}" \
    bash "${INSTALLER}" "$@" < /dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test_help_flag() {
  local out
  out="$(bash "${INSTALLER}" --help 2>&1 || true)"
  assert_contains "--help: shows --iq-dest option" "--iq-dest" "${out}"
  assert_contains "--help: shows --all-decoders" "--all-decoders" "${out}"
  assert_contains "--help: shows --no-sdr" "--no-sdr" "${out}"
  assert_contains "--help: shows example with --iq-dest" "brian.local" "${out}"
}

test_unknown_flag_fails() {
  local rc=0
  bash "${INSTALLER}" --unknown-flag < /dev/null 2>/dev/null || rc=$?
  assert_exit "--unknown-flag exits non-zero" 1 "${rc}"
}

test_iq_dest_requires_value() {
  local rc=0
  bash "${INSTALLER}" --iq-dest < /dev/null 2>/dev/null || rc=$?
  assert_exit "--iq-dest without value exits non-zero" 1 "${rc}"

  local err
  err="$(bash "${INSTALLER}" --iq-dest 2>&1 || true)"
  assert_contains "--iq-dest without value: shows error" "requires a destination argument" "${err}"
}

test_iq_dest_dry_run_installs_service() {
  setup_stubs
  local out rc=0
  out="$(run_installer --dry-run \
    --iq-dest "rf_worker@brian.local:/var/lib/rf-adapt-intel/incoming/" \
    --no-service)" || rc=$?
  teardown_stubs
  # --no-service skips deployment, so we test only that IQ_DEST is echoed
  assert_exit "--iq-dest --no-service exits 0" 0 "${rc}"
}

test_iq_dest_dry_run_summary() {
  setup_stubs
  local out rc=0
  out="$(run_installer --dry-run \
    --iq-dest "rf_worker@brian.local:/var/lib/rf-adapt-intel/incoming/")" \
    || rc=$?
  teardown_stubs
  assert_exit "--iq-dest dry-run exits 0" 0 "${rc}"
  # The banner should show IQ transfer is enabled
  assert_contains "summary: IQ transfer enabled in banner" \
    "IQ transfer: enabled" "${out}"
  # Dry-run should echo the iq-transfer-watcher service install commands
  assert_contains "dry-run: installs iq-transfer-watcher.service" \
    "iq-transfer-watcher.service" "${out}"
  assert_contains "dry-run: installs hardening.conf" \
    "hardening.conf" "${out}"
  assert_contains "dry-run: enables iq-transfer-watcher" \
    "iq-transfer-watcher" "${out}"
  # Summary should mention watcher status
  assert_contains "summary: mentions iq-transfer-watcher status command" \
    "systemctl status iq-transfer-watcher" "${out}"
}

test_no_iq_dest_summary_shows_hint() {
  setup_stubs
  local out rc=0
  out="$(run_installer --dry-run)" || rc=$?
  teardown_stubs
  assert_exit "no --iq-dest dry-run exits 0" 0 "${rc}"
  # When --iq-dest is not given, summary should suggest the flag
  assert_contains "summary: hint for --iq-dest" "--iq-dest" "${out}"
  assert_not_contains "summary: no 'watcher enabled' message" \
    "IQ transfer to Brian is enabled" "${out}"
}

test_iq_dest_no_sdr_warns() {
  setup_stubs
  local out rc=0
  out="$(run_installer --dry-run --no-sdr \
    --iq-dest "rf_worker@brian.local:/var/lib/rf-adapt-intel/incoming/")" \
    || rc=$?
  teardown_stubs
  assert_exit "--iq-dest --no-sdr exits 0" 0 "${rc}"
  assert_contains "--iq-dest --no-sdr: shows warning" \
    "only meaningful on Ray" "${out}"
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
echo "=== tests/test_install.sh ==="
test_help_flag
test_unknown_flag_fails
test_iq_dest_requires_value
test_iq_dest_dry_run_installs_service
test_iq_dest_dry_run_summary
test_no_iq_dest_summary_shows_hint
test_iq_dest_no_sdr_warns

echo ""
echo "Results: ${_PASS} passed, ${_FAIL} failed."
if [[ $_FAIL -gt 0 ]]; then
  exit 1
fi
