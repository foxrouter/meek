#!/usr/bin/env bash
# tests/test_check_ssh_permissions.sh — Shell tests for
# scripts/check_ssh_permissions.sh CLI flags and behavior.
#
# Run with:
#   bash tests/test_check_ssh_permissions.sh [-v]
#
# Tests exercise argument parsing, check/fix logic, and dry-run output of
# scripts/check_ssh_permissions.sh by running it under a temporary fake root
# environment with stub id/ssh-keygen/ssh-keyscan commands and a synthetic
# directory tree.  No real root privileges are required.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/check_ssh_permissions.sh"
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
# Test environment helpers
# ---------------------------------------------------------------------------
_TMP=""
_STUB_DIR=""

# Set up a fake root environment with stub commands.
# The script bypasses the root guard by setting `_RF_TEST_NO_ROOT=1`, so
# tests can run without real root privileges.
setup_env() {
  _TMP="$(mktemp -d /tmp/rf-ssh-perm-test.XXXXXX)"
  _STUB_DIR="$(mktemp -d /tmp/rf-ssh-perm-stubs.XXXXXX)"
  _CURRENT_USER="$(id -un)"

  # stub: id — pretend the service user (= current user in tests) exists
  cat > "${_STUB_DIR}/id" <<EOF
#!/usr/bin/env bash
# If querying for the test service user existence, succeed.
if [[ "\${*}" == "${_CURRENT_USER}" ]]; then
  exit 0
fi
exec /usr/bin/id "\$@"
EOF

  # stub: ssh-keygen — handle -F (host lookup), -l (fingerprint), and key generation
  cat > "${_STUB_DIR}/ssh-keygen" <<'EOF'
#!/usr/bin/env bash
# Stub: -F <host> returns "not found"; -l prints a fake fingerprint;
# key generation creates stub files.
if [[ "${1:-}" == "-F" ]]; then
  # Simulate: host NOT found in known_hosts (so keyscan is triggered).
  exit 1
fi
if [[ "${1:-}" == "-l" ]]; then
  # Simulate fingerprint output for the given file.
  echo "256 SHA256:AAABBBCCC stub-fingerprint (ED25519)"
  exit 0
fi
# Otherwise simulate key generation.
keyfile=""
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "-f" ]]; then
    j=$(( i + 1 ))
    keyfile="${!j}"
    break
  fi
done
if [[ -n "${keyfile}" ]]; then
  touch "${keyfile}" "${keyfile}.pub"
  echo "ssh-ed25519 AAAA stub-key rf_worker@testhost" > "${keyfile}.pub"
  chmod 600 "${keyfile}"
fi
exit 0
EOF

  # stub: ssh-keyscan — return a fake hashed known_hosts line
  cat > "${_STUB_DIR}/ssh-keyscan" <<'EOF'
#!/usr/bin/env bash
echo "|1|fakedhashedhost|AAAAB3NzaC1yc2EAAAADAQABAAAAgQC stub host key"
exit 0
EOF

  # stub: sudo — strip -u <user> and VAR=val env assignments, then exec the command
  cat > "${_STUB_DIR}/sudo" <<'EOF'
#!/usr/bin/env bash
# Strip 'sudo -u <user>' prefix and any VAR=value env assignments so the
# remaining command can be executed directly without real privilege escalation.
while [[ $# -gt 0 ]]; do
  case "$1" in
    -u) shift 2 ;;       # skip -u and the username argument
    --) shift; break ;;  # end of sudo options
    *=*) export "$1"; shift ;;  # export VAR=value and continue
    *) break ;;          # first non-option/non-env-assign arg is the command
  esac
done
exec "$@"
EOF

  # stub: hostname — return a predictable value
  cat > "${_STUB_DIR}/hostname" <<'EOF'
#!/usr/bin/env bash
echo "testhost"
EOF

  # stub: chown — no-op; real chown requires root to change ownership,
  # which is unavailable on CI runners.
  cat > "${_STUB_DIR}/chown" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  # stub: chmod — no-op; avoids failures when the script tries to chmod
  # paths that may have been created without root permissions in tests.
  # Pre-existing test fixtures use the real chmod (before PATH override).
  cat > "${_STUB_DIR}/chmod" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  chmod +x "${_STUB_DIR}/id" "${_STUB_DIR}/ssh-keygen" "${_STUB_DIR}/ssh-keyscan" \
            "${_STUB_DIR}/sudo" "${_STUB_DIR}/hostname" \
            "${_STUB_DIR}/chown" "${_STUB_DIR}/chmod"

  # Create the fake application data directory under _TMP
  mkdir -p "${_TMP}/var/lib/rf-adapt-intel"
  chown "$(id -u):$(id -g)" "${_TMP}/var/lib/rf-adapt-intel"
}

teardown_env() {
  rm -rf "${_TMP:-}" "${_STUB_DIR:-}"
}

# Run the script with the given args, injecting the test environment:
#   - PATH: stub commands before real ones
#   - SERVICE_USER: current user (exists on this machine without root)
#   - SSH_BASE: redirected to a temp dir (no real system paths touched)
#   - _RF_TEST_NO_ROOT: skip the root-privilege check
run_check() {
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" "$@" 2>&1 || true
}

# Run and capture exit code separately (for assert_exit tests).
run_check_rc() {
  local rc=0
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" "$@" 2>&1 || rc=$?
  echo "${rc}"
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test_help_flag() {
  local out
  out="$(bash "${SCRIPT}" --help 2>&1 || true)"
  assert_contains "--help: shows --fix option"    "--fix"    "${out}"
  assert_contains "--help: shows --dry-run option" "--dry-run" "${out}"
  assert_contains "--help: shows --host option"   "--host"   "${out}"
}

test_unknown_flag_fails() {
  local rc=0
  bash "${SCRIPT}" --unknown-flag < /dev/null 2>/dev/null || rc=$?
  assert_exit "--unknown-flag exits non-zero" 1 "${rc}"
}

test_requires_root() {
  # Without _RF_TEST_NO_ROOT set, the script must exit non-zero with a root error.
  local out rc=0
  out="$(bash "${SCRIPT}" 2>&1)" || rc=$?
  assert_exit "non-root: exits non-zero" 1 "${rc}"
  assert_contains "non-root: shows root error" "must be run as root" "${out}"
}

test_dry_run_reports_issues() {
  setup_env
  local out
  out="$(run_check --dry-run)"
  teardown_env
  # Should report that .ssh dir does not exist and mention dry-run actions
  assert_contains "dry-run: mentions .ssh" ".ssh" "${out}"
  assert_contains "dry-run: shows dry-run label" "dry-run" "${out}"
}

test_fix_creates_ssh_dir() {
  setup_env
  local out
  out="$(run_check --fix)"
  teardown_env
  # The script should have created the .ssh directory and reported it fixed
  assert_contains "fix: creates .ssh dir" ".ssh" "${out}"
  assert_contains "fix: reports FIXED or PASS" "FIXED" "${out}"
}

test_fix_generates_key_pair() {
  setup_env
  local out
  out="$(run_check --fix)"
  teardown_env
  assert_contains "fix: generates key pair" "key pair" "${out}"
}

test_dry_run_shows_key_generation() {
  setup_env
  local out
  out="$(run_check --dry-run)"
  teardown_env
  assert_contains "dry-run: mentions key generation" "key" "${out}"
}

test_host_flag_triggers_keyscan() {
  setup_env
  local out
  out="$(run_check --fix --host testbrianhost)"
  teardown_env
  # ssh-keyscan should have been invoked for testbrianhost
  assert_contains "host: triggers keyscan" "testbrianhost" "${out}"
}

test_host_flag_dry_run() {
  setup_env
  local out
  out="$(run_check --dry-run --host testbrianhost)"
  teardown_env
  assert_contains "host dry-run: mentions ssh-keyscan" "ssh-keyscan" "${out}"
  assert_contains "host dry-run: shows dry-run label" "dry-run" "${out}"
}

test_invalid_key_type_fails() {
  local rc=0
  bash "${SCRIPT}" --key-type badtype < /dev/null 2>/dev/null || rc=$?
  assert_exit "--key-type invalid exits non-zero" 1 "${rc}"
}

test_valid_rsa_key_type() {
  setup_env
  local out
  out="$(run_check --dry-run --key-type rsa)"
  teardown_env
  assert_contains "--key-type rsa: mentions rsa" "rsa" "${out}"
}

test_host_requires_argument() {
  local rc=0
  bash "${SCRIPT}" --host < /dev/null 2>/dev/null || rc=$?
  assert_exit "--host without arg exits non-zero" 1 "${rc}"
}

test_already_correct_permissions() {
  setup_env
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  local ssh_key="${ssh_dir}/id_ed25519"
  # Pre-create everything correctly so no fixes are needed
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  touch "${ssh_key}" "${ssh_key}.pub"
  echo "stub-public-key rf_worker@testhost" > "${ssh_key}.pub"
  chmod 600 "${ssh_key}"
  touch "${ssh_dir}/known_hosts"
  chmod 600 "${ssh_dir}/known_hosts"
  local out
  out="$(run_check)"
  teardown_env
  # All checks should pass
  assert_contains "correct perms: all pass" "All checks passed" "${out}"
  assert_not_contains "correct perms: no FAIL" "[FAIL]" "${out}"
}

test_summary_shows_public_key() {
  setup_env
  # Pre-create a public key file
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  local ssh_key="${ssh_dir}/id_ed25519"
  touch "${ssh_key}" "${ssh_key}.pub"
  echo "ssh-ed25519 AAAA stub-key rf_worker@testhost" > "${ssh_key}.pub"
  chmod 600 "${ssh_key}"
  touch "${ssh_dir}/known_hosts"
  chmod 600 "${ssh_dir}/known_hosts"
  local out
  out="$(run_check)"
  teardown_env
  # Should display the public key for copying
  assert_contains "summary: shows public key" "authorized_keys" "${out}"
}

test_ssh_dir_symlink_rejected() {
  setup_env
  # Plant a symlink at .ssh — the script must refuse to operate on it
  ln -s /tmp "${_TMP}/var/lib/rf-adapt-intel/.ssh"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" 2>&1)" || rc=$?
  teardown_env
  assert_contains "symlink .ssh: reports symlink failure" "symlink" "${out}"
  assert_contains "symlink .ssh: reports FAIL" "[FAIL]" "${out}"
}

test_ssh_dir_symlink_fix_replaces_it() {
  setup_env
  # Plant a symlink at .ssh — with --fix it should be replaced by a real dir
  ln -s /tmp "${_TMP}/var/lib/rf-adapt-intel/.ssh"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" --fix 2>&1)" || rc=$?
  teardown_env
  assert_contains "symlink .ssh --fix: reports FIXED" "FIXED" "${out}"
}

test_base_dir_write_access_checked() {
  setup_env
  # Make the base directory not writable by the current user (mode 0555)
  chmod 0555 "${_TMP}/var/lib/rf-adapt-intel"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" 2>&1)" || rc=$?
  # Restore mode so teardown_env can remove the temp dir
  chmod 0755 "${_TMP}/var/lib/rf-adapt-intel" 2>/dev/null || true
  teardown_env
  assert_contains "write access: detects non-writable dir" "not writable" "${out}"
}

test_exit_nonzero_when_fix_fails_for_some() {
  setup_env
  # In check-only mode with .ssh absent, failures are reported without resolution.
  local rc=0
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" >/dev/null 2>&1 || rc=$?
  teardown_env
  # Check-only mode with .ssh absent should exit non-zero
  assert_exit "check-only with failures: exits non-zero" 1 "${rc}"
}

test_tofu_warning_printed_on_host_keyscan() {
  setup_env
  local out
  out="$(run_check --fix --host testbrianhost)"
  teardown_env
  assert_contains "tofu warning: mentions TOFU" "TOFU" "${out}"
  assert_contains "tofu warning: mentions verify" "erify" "${out}"
}

test_fingerprint_shown_after_keyscan() {
  setup_env
  local out
  out="$(run_check --fix --host testbrianhost)"
  teardown_env
  # After adding a host key the script should display the fingerprint
  assert_contains "fingerprint: mentions fingerprint" "fingerprint" "${out}"
}

test_ssh_dir_not_a_directory_rejected() {
  setup_env
  # Plant a regular file at .ssh — the script should report it as an error
  touch "${_TMP}/var/lib/rf-adapt-intel/.ssh"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" 2>&1)" || rc=$?
  teardown_env
  assert_exit  "non-dir .ssh: exits non-zero" 1 "${rc}"
  assert_contains "non-dir .ssh: reports unexpected file type" "not a directory" "${out}"
  assert_contains "non-dir .ssh: reports FAIL" "[FAIL]" "${out}"
}

test_ssh_dir_not_a_directory_fix_replaces_it() {
  setup_env
  # Plant a regular file at .ssh — with --fix it should be replaced by a real dir
  touch "${_TMP}/var/lib/rf-adapt-intel/.ssh"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" --fix 2>&1)" || rc=$?
  teardown_env
  assert_contains "non-dir .ssh --fix: reports FIXED" "FIXED" "${out}"
}

test_known_hosts_not_a_file_rejected() {
  setup_env
  # Create .ssh dir correctly, then put a *directory* where known_hosts should be
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  mkdir -p "${ssh_dir}/known_hosts"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" 2>&1)" || rc=$?
  teardown_env
  assert_exit  "non-file known_hosts: exits non-zero" 1 "${rc}"
  assert_contains "non-file known_hosts: reports not a regular file" "not a regular file" "${out}"
  assert_contains "non-file known_hosts: reports FAIL" "[FAIL]" "${out}"
}

test_write_access_checked_in_dry_run() {
  setup_env
  # Make the base directory not writable and run with --dry-run; the check
  # should still run (it's read-only) and report a failure.
  chmod 0555 "${_TMP}/var/lib/rf-adapt-intel"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" --dry-run 2>&1)" || rc=$?
  chmod 0755 "${_TMP}/var/lib/rf-adapt-intel" 2>/dev/null || true
  teardown_env
  assert_exit  "dry-run write check: exits non-zero" 1 "${rc}"
  assert_contains "dry-run write check: detects non-writable dir" "not writable" "${out}"
}


test_check_only_reports_but_not_fix() {
  setup_env
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  local ssh_key="${ssh_dir}/id_ed25519"
  touch "${ssh_key}" "${ssh_key}.pub"
  echo "ssh-ed25519 AAAA stub-key rf_worker@testhost" > "${ssh_key}.pub"
  chmod 600 "${ssh_key}"
  touch "${ssh_dir}/known_hosts"
  chmod 600 "${ssh_dir}/known_hosts"
  local out
  out="$(run_check)"
  teardown_env
  # In check-only mode with no issues, should not print [FIXED]
  assert_not_contains "check-only: no FIXED output" "[FIXED]" "${out}"
}

test_ssh_key_symlink_rejected() {
  setup_env
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  # Plant a symlink at the private key path — script must detect and report it
  ln -s /dev/null "${ssh_dir}/id_ed25519"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" 2>&1)" || rc=$?
  teardown_env
  assert_contains "ssh key symlink: reports symlink failure" "symlink" "${out}"
  assert_contains "ssh key symlink: reports FAIL" "[FAIL]" "${out}"
}

test_ssh_key_symlink_fix_replaces_it() {
  setup_env
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  # Plant a symlink at the private key path — with --fix it should be replaced
  ln -s /dev/null "${ssh_dir}/id_ed25519"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" --fix 2>&1)" || rc=$?
  teardown_env
  assert_contains "ssh key symlink --fix: reports FIXED" "FIXED" "${out}"
}

test_ssh_pubkey_symlink_rejected() {
  setup_env
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  local ssh_key="${ssh_dir}/id_ed25519"
  touch "${ssh_key}"
  chmod 600 "${ssh_key}"
  # Plant a symlink at the public key path — script must detect and report it
  ln -s /dev/null "${ssh_dir}/id_ed25519.pub"
  touch "${ssh_dir}/known_hosts"
  chmod 600 "${ssh_dir}/known_hosts"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" 2>&1)" || rc=$?
  teardown_env
  assert_contains "ssh pubkey symlink: reports symlink failure" "symlink" "${out}"
  assert_contains "ssh pubkey symlink: reports FAIL" "[FAIL]" "${out}"
}

test_ssh_pubkey_symlink_fix_replaces_it() {
  setup_env
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  local ssh_key="${ssh_dir}/id_ed25519"
  touch "${ssh_key}"
  chmod 600 "${ssh_key}"
  # Plant a symlink at the public key path — with --fix it should be replaced
  ln -s /dev/null "${ssh_dir}/id_ed25519.pub"
  touch "${ssh_dir}/known_hosts"
  chmod 600 "${ssh_dir}/known_hosts"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" --fix 2>&1)" || rc=$?
  teardown_env
  assert_contains "ssh pubkey symlink --fix: reports FIXED" "FIXED" "${out}"
}

# Regression: when .pub is a symlink, the script must NOT print the symlink
# target's contents, even in check-only mode (script runs as root).
test_ssh_pubkey_symlink_does_not_print_target_contents() {
  setup_env
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  local ssh_key="${ssh_dir}/id_ed25519"
  touch "${ssh_key}"
  chmod 600 "${ssh_key}"
  # Create a file with a unique sentinel that must never appear in script output
  local target_file="${_TMP}/secret_target.txt"
  echo "UNIQUE_SECRET_MARKER_MUST_NOT_BE_PRINTED" > "${target_file}"
  # Plant the symlink at the pubkey path pointing at the sentinel file
  ln -s "${target_file}" "${ssh_dir}/id_ed25519.pub"
  touch "${ssh_dir}/known_hosts"
  chmod 600 "${ssh_dir}/known_hosts"
  local out rc=0
  out="$(env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" 2>&1)" || rc=$?
  teardown_env
  assert_contains "pubkey symlink no-print: reports symlink failure" "symlink" "${out}"
  assert_not_contains "pubkey symlink no-print: symlink target NOT printed" \
    "UNIQUE_SECRET_MARKER_MUST_NOT_BE_PRINTED" "${out}"
}

# Regression: --fix --host must exit non-zero when ssh-keyscan fails.
# This guards against _FIXED over-inflation masking the scan failure:
# host-key additions are not repairs of [FAIL] items, so a keyscan failure
# must not be hidden by a previously-incremented _FIXED counter.
test_keyscan_failure_exits_nonzero() {
  setup_env
  # Override the stub ssh-keyscan with one that returns nothing (simulates
  # unreachable host / network failure).
  cat > "${_STUB_DIR}/ssh-keyscan" <<'EOF'
#!/usr/bin/env bash
# Simulate an unreachable host: print nothing and exit non-zero.
exit 1
EOF
  chmod +x "${_STUB_DIR}/ssh-keyscan"
  local rc=0
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" --fix --host unreachable.example.invalid \
    >/dev/null 2>&1 || rc=$?
  teardown_env
  assert_exit "keyscan failure: --fix --host exits 1 when scan fails" 1 "${rc}"
}

# Test that --fix exits 0 when every detected issue is successfully repaired.
test_fix_exits_zero_when_all_fixed() {
  setup_env
  local rc=0
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    SERVICE_USER="${_CURRENT_USER}" \
    SSH_BASE="${_TMP}/var/lib/rf-adapt-intel" \
    _RF_TEST_NO_ROOT=1 \
    bash "${SCRIPT}" --fix >/dev/null 2>&1 || rc=$?
  teardown_env
  assert_exit "fix mode: exits 0 when all issues fixed" 0 "${rc}"
}

# Test that the atomic known_hosts creation creates a real file (not a symlink).
test_known_hosts_created_atomically() {
  setup_env
  local ssh_dir="${_TMP}/var/lib/rf-adapt-intel/.ssh"
  mkdir -p "${ssh_dir}"
  chmod 700 "${ssh_dir}"
  local kh="${ssh_dir}/known_hosts"
  run_check --fix >/dev/null 2>&1
  # Check before teardown removes the temp tree
  local is_real_file=false
  if [[ -e "${kh}" ]] && [[ ! -L "${kh}" ]]; then
    is_real_file=true
  fi
  teardown_env
  if $is_real_file; then
    ok "atomic known_hosts: created as real file (not symlink)"
  else
    fail "atomic known_hosts: created as real file (not symlink)"
  fi
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
echo "=== tests/test_check_ssh_permissions.sh ==="
test_help_flag
test_unknown_flag_fails
test_requires_root
test_dry_run_reports_issues
test_fix_creates_ssh_dir
test_fix_generates_key_pair
test_dry_run_shows_key_generation
test_host_flag_triggers_keyscan
test_host_flag_dry_run
test_invalid_key_type_fails
test_valid_rsa_key_type
test_host_requires_argument
test_already_correct_permissions
test_summary_shows_public_key
test_ssh_dir_symlink_rejected
test_ssh_dir_symlink_fix_replaces_it
test_base_dir_write_access_checked
test_exit_nonzero_when_fix_fails_for_some
test_tofu_warning_printed_on_host_keyscan
test_fingerprint_shown_after_keyscan
test_check_only_reports_but_not_fix
test_ssh_dir_not_a_directory_rejected
test_ssh_dir_not_a_directory_fix_replaces_it
test_known_hosts_not_a_file_rejected
test_write_access_checked_in_dry_run
test_ssh_key_symlink_rejected
test_ssh_key_symlink_fix_replaces_it
test_ssh_pubkey_symlink_rejected
test_ssh_pubkey_symlink_fix_replaces_it
test_ssh_pubkey_symlink_does_not_print_target_contents
test_keyscan_failure_exits_nonzero
test_fix_exits_zero_when_all_fixed
test_known_hosts_created_atomically

echo ""
echo "Results: ${_PASS} passed, ${_FAIL} failed."
if [[ $_FAIL -gt 0 ]]; then
  exit 1
fi
