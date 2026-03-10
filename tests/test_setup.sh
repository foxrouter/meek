#!/usr/bin/env bash
# tests/test_setup.sh — Shell tests for ops/setup.sh CLI flags and behaviour.
#
# Run with:
#   bash tests/test_setup.sh [-v]
#
# Tests are run entirely in /tmp and require no root, SDR hardware, or network
# access. They exercise the argument parsing, platform checks, and dry-run
# output of ops/setup.sh by overriding apt-get / git / sudo with stubs.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETUP="${REPO_ROOT}/ops/setup.sh"
VERBOSE=false
[[ "${1:-}" == "-v" ]] && VERBOSE=true

# ---------------------------------------------------------------------------
# Minimal test harness
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
# Run setup.sh in a temporary environment where sudo / apt-get / git are
# stubbed so no real system changes happen, even without --dry-run.
# ---------------------------------------------------------------------------
run_setup() {
  # All extra args forwarded to setup.sh.
  # We inject a PATH that prepends a stub directory so fake sudo/apt/git
  # are used.  stdin is /dev/null to simulate non-TTY.
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    HOME="${_TMP}" \
    bash "${SETUP}" "$@" < /dev/null 2>&1
}

setup_stubs() {
  _STUB_DIR="$(mktemp -d /tmp/rf-adapt-intel-setup-stubs.XXXXXX)"
  _TMP="$(mktemp -d /tmp/rf-adapt-intel-setup-test.XXXXXX)"

  # stub: sudo — just run the rest of the args (drops privilege requirement)
  cat > "${_STUB_DIR}/sudo" <<'EOF'
#!/usr/bin/env bash
# Stub: run command without real sudo
exec "$@"
EOF

  # stub: apt-get — pretend success silently
  cat > "${_STUB_DIR}/apt-get" <<'EOF'
#!/usr/bin/env bash
# Stub: apt-get always succeeds
exit 0
EOF

  # Extract the expected commit hash from setup.sh so the stub can echo it back.
  local _expected_commit
  _expected_commit=$(grep '^LIQUID_DSP_EXPECTED_COMMIT=' "${SETUP}" | cut -d'"' -f2)

  # stub: git — pretend clone succeeded by creating a minimal dir structure;
  # echo the expected commit on "rev-parse HEAD" so verification passes.
  cat > "${_STUB_DIR}/git" <<EOF
#!/usr/bin/env bash
# Stub: git clone creates empty target dir; rev-parse returns expected commit
if [[ "\${1:-}" == "clone" ]]; then
  # \${*: -1} extracts the last argument (the clone destination directory)
  dest="\${*: -1}"
  mkdir -p "\${dest}"
  # Minimal autoconf stubs so setup.sh bootstrap steps succeed
  touch "\${dest}/bootstrap.sh"
  chmod +x "\${dest}/bootstrap.sh"
elif [[ "\${1:-}" == "-C" && "\${3:-}" == "rev-parse" && "\${4:-}" == "HEAD" ]]; then
  echo "${_expected_commit}"
fi
exit 0
EOF

  chmod +x "${_STUB_DIR}/sudo" "${_STUB_DIR}/apt-get" "${_STUB_DIR}/git"
}

teardown_stubs() {
  rm -rf "${_STUB_DIR:-}" "${_TMP:-}"
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test_help_flag() {
  local out
  out="$(bash "${SETUP}" --help 2>&1 || true)"
  assert_contains "--help: shows usage" "--install-multimon-ng" "${out}"
  assert_contains "--help: mentions --non-interactive" "--non-interactive" "${out}"
}

test_unknown_flag_fails() {
  local rc=0
  bash "${SETUP}" --unknown-flag < /dev/null 2>/dev/null || rc=$?
  assert_exit "--unknown-flag exits non-zero" 1 "${rc}"
}

test_no_decoders_exits_zero() {
  setup_stubs
  local out rc=0
  out="$(run_setup --non-interactive)" || rc=$?
  teardown_stubs
  assert_exit "no decoders selected exits 0" 0 "${rc}"
  assert_contains "no decoders: reports nothing to install" "Nothing to install" "${out}"
}

test_dry_run_multimon() {
  setup_stubs
  local out rc=0
  out="$(run_setup --non-interactive --install-multimon-ng --dry-run)" || rc=$?
  teardown_stubs
  assert_exit "--dry-run exits 0" 0 "${rc}"
  assert_contains "--dry-run: mentions apt-get install" "apt-get" "${out}"
  assert_contains "--dry-run: multimon-ng appears in output" "multimon-ng" "${out}"
  assert_not_contains "--dry-run: does not actually install" "FAIL" "${out}"
}

test_dry_run_rtl_433() {
  setup_stubs
  local out rc=0
  out="$(run_setup --non-interactive --install-rtl_433 --dry-run)" || rc=$?
  teardown_stubs
  assert_exit "--install-rtl_433 --dry-run exits 0" 0 "${rc}"
  assert_contains "--dry-run: rtl_433 in output" "rtl-433" "${out}"
}

test_dry_run_liquid_dsp() {
  setup_stubs
  local out rc=0
  out="$(run_setup --non-interactive --install-liquid-dsp --dry-run)" || rc=$?
  teardown_stubs
  assert_exit "--install-liquid-dsp --dry-run exits 0" 0 "${rc}"
  assert_contains "--dry-run: liquid-dsp in output" "liquid-dsp" "${out}"
  # Verify the clone is pinned to the declared version tag (not HEAD).
  local liq_tag
  liq_tag=$(grep '^LIQUID_DSP_GIT_TAG=' "${SETUP}" | cut -d'"' -f2)
  assert_contains "--dry-run: clones specific version tag" "${liq_tag}" "${out}"
}

test_liquid_dsp_commit_mismatch_fails() {
  setup_stubs
  # Override the git stub so rev-parse returns an all-zero (wrong) commit hash.
  cat > "${_STUB_DIR}/git" <<'EOF'
#!/usr/bin/env bash
# Stub: clone creates dir; rev-parse returns a deliberately wrong hash.
if [[ "${1:-}" == "clone" ]]; then
  dest="${*: -1}"
  mkdir -p "${dest}"
  touch "${dest}/bootstrap.sh"
  chmod +x "${dest}/bootstrap.sh"
elif [[ "${1:-}" == "-C" && "${3:-}" == "rev-parse" && "${4:-}" == "HEAD" ]]; then
  echo "0000000000000000000000000000000000000000"
fi
exit 0
EOF
  chmod +x "${_STUB_DIR}/git"

  local out rc=0
  out="$(run_setup --non-interactive --install-liquid-dsp)" || rc=$?
  teardown_stubs
  assert_exit "commit mismatch: exits non-zero" 1 "${rc}"
  assert_contains "commit mismatch: error message shown" "commit hash mismatch" "${out}"
}

test_dry_run_all_flags() {
  setup_stubs
  local out rc=0
  out="$(run_setup --non-interactive \
    --install-multimon-ng --install-rtl_433 --install-liquid-dsp \
    --dry-run)" || rc=$?
  teardown_stubs
  assert_exit "all flags --dry-run exits 0" 0 "${rc}"
  assert_contains "all flags: multimon-ng in output" "multimon-ng" "${out}"
  assert_contains "all flags: rtl_433 in output" "rtl-433" "${out}"
  assert_contains "all flags: liquid-dsp in output" "liquid-dsp" "${out}"
}

test_non_interactive_no_tty() {
  # When stdin is not a TTY and --non-interactive is absent, the script
  # should still behave non-interactively (not hang waiting for input).
  setup_stubs
  local out rc=0
  out="$(run_setup)" || rc=$?
  teardown_stubs
  assert_exit "non-TTY stdin exits 0" 0 "${rc}"
  assert_contains "non-TTY: nothing to install" "Nothing to install" "${out}"
}

test_platform_check_runs() {
  setup_stubs
  local out rc=0
  out="$(run_setup --non-interactive --dry-run)" || rc=$?
  teardown_stubs
  assert_exit "platform check exits 0" 0 "${rc}"
  assert_contains "platform: arch detected" "Arch:" "${out}"
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
echo "=== tests/test_setup.sh ==="
test_help_flag
test_unknown_flag_fails
test_no_decoders_exits_zero
test_dry_run_multimon
test_dry_run_rtl_433
test_dry_run_liquid_dsp
test_liquid_dsp_commit_mismatch_fails
test_dry_run_all_flags
test_non_interactive_no_tty
test_platform_check_runs

echo ""
echo "Results: ${_PASS} passed, ${_FAIL} failed."
if [[ $_FAIL -gt 0 ]]; then
  exit 1
fi
