#!/usr/bin/env bash
# tests/test_canary.sh — Shell tests for ops/canary.sh CLI flags and behaviour.
#
# Run with:
#   bash tests/test_canary.sh [-v]
#
# Tests exercise argument parsing, the --heal flag, heartbeat/DB staleness
# checks, and dry-run output of ops/canary.sh by stubbing systemctl / sqlite3
# so no real system changes happen.  No root, SDR hardware, or network access
# is required.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CANARY="${REPO_ROOT}/ops/canary.sh"
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
# Test environment helpers
# ---------------------------------------------------------------------------
_TMP=""
_STUB_DIR=""

setup_env() {
  _TMP="$(mktemp -d /tmp/rf-canary-test.XXXXXX)"
  _STUB_DIR="$(mktemp -d /tmp/rf-canary-stubs.XXXXXX)"

  # stub: sudo — run as current user (no privilege drop required in tests)
  cat > "${_STUB_DIR}/sudo" <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF

  # stub: systemctl — configurable via STUB_ACTIVE_STATE env var.
  # Default is "active".
  cat > "${_STUB_DIR}/systemctl" <<'EOF'
#!/usr/bin/env bash
# Stub systemctl: honours STUB_ACTIVE_STATE for show -p ActiveState queries
# and STUB_IS_ACTIVE for is-active checks.
ACTIVE_STATE="${STUB_ACTIVE_STATE:-active}"
IS_ACTIVE="${STUB_IS_ACTIVE:-0}"

case "$*" in
  *"is-active"*)
    exit "${IS_ACTIVE}"
    ;;
  *"ActiveState"*)
    echo "${ACTIVE_STATE}"
    exit 0
    ;;
  *"reset-failed"*|*"start"*|*"daemon-reload"*|*"restart"*)
    echo "[stub] systemctl $*"
    exit 0
    ;;
  *"show -p MainPID"*|*"MainPID"*)
    echo "0"
    exit 0
    ;;
  *)
    echo "[stub] systemctl $*"
    exit 0
    ;;
esac
EOF

  # stub: sqlite3 — returns a configurable last epoch timestamp.
  # Default: current epoch (fresh DB).
  cat > "${_STUB_DIR}/sqlite3" <<'EOF'
#!/usr/bin/env bash
# Stub sqlite3: returns STUB_LAST_EPOCH (defaults to current epoch seconds).
LAST_EPOCH="${STUB_LAST_EPOCH:-$(date '+%s')}"
echo "${LAST_EPOCH}"
exit 0
EOF

  chmod +x "${_STUB_DIR}/sudo" "${_STUB_DIR}/systemctl" "${_STUB_DIR}/sqlite3"
}

teardown_env() {
  rm -rf "${_TMP:-}" "${_STUB_DIR:-}"
}

# Run canary.sh with stubs injected and test-specific env vars.
# All extra args forwarded to canary.sh.
run_canary() {
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    HOME="${_TMP}" \
    RF_CANARY_SKIP_ROOT_CHECK=1 \
    RF_METRICS_FILE="${_TMP}/metrics.prom" \
    RF_WORKER_LOG="${_TMP}/worker.log" \
    RF_HEARTBEAT_FILE="${_TMP}/heartbeat" \
    RF_DB_PATH="${_TMP}/rf.db" \
    STUB_ACTIVE_STATE="${STUB_ACTIVE_STATE:-active}" \
    STUB_IS_ACTIVE="${STUB_IS_ACTIVE:-0}" \
    STUB_LAST_EPOCH="${STUB_LAST_EPOCH:-$(date '+%s')}" \
    bash "${CANARY}" "$@" 2>&1
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test_help_flag() {
  local out
  out="$(bash "${CANARY}" --help 2>&1 || true)"
  assert_contains "--help: mentions --status" "--status" "${out}"
  assert_contains "--help: mentions --heal" "--heal" "${out}"
  assert_contains "--help: mentions --promote" "--promote" "${out}"
  assert_contains "--help: mentions --rollback" "--rollback" "${out}"
}

test_unknown_flag_fails() {
  local rc=0
  bash "${CANARY}" --unknown-flag 2>/dev/null || rc=$?
  assert_exit "--unknown-flag exits non-zero" 1 "${rc}"
}

test_status_exits_zero_no_files() {
  setup_env
  local out rc=0
  out="$(run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status with no files exits 0" 0 "${rc}"
  assert_contains "--status: shows Prometheus section" "Prometheus metrics" "${out}"
}

test_status_shows_heartbeat_missing_warning() {
  setup_env
  # heartbeat file does not exist
  local out rc=0
  out="$(run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status (no heartbeat) exits 0" 0 "${rc}"
  assert_contains "--status: warns on missing heartbeat" "Heartbeat file not found" "${out}"
}

test_status_shows_heartbeat_fresh() {
  setup_env
  # Create a fresh heartbeat file (mtime = now)
  echo "ok $(date +%s)" > "${_TMP}/heartbeat"
  local out rc=0
  out="$(run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status (fresh heartbeat) exits 0" 0 "${rc}"
  assert_contains "--status: fresh heartbeat OK" "[OK] Heartbeat is fresh" "${out}"
  assert_not_contains "--status: no stale warning for fresh hb" "Heartbeat STALE" "${out}"
}

test_status_shows_heartbeat_stale() {
  setup_env
  # Create a heartbeat file with mtime 1 hour ago
  local old_file="${_TMP}/heartbeat"
  echo "ok 0" > "${old_file}"
  touch -d "1 hour ago" "${old_file}" 2>/dev/null \
    || touch -t "$(date -d '1 hour ago' '+%Y%m%d%H%M.%S' 2>/dev/null || echo '202001010000.00')" \
              "${old_file}" 2>/dev/null || true
  local out rc=0
  out="$(RF_STALE_HEARTBEAT_S=10 run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status (stale heartbeat) exits 0" 0 "${rc}"
  assert_contains "--status: stale heartbeat warning" "Heartbeat STALE" "${out}"
}

test_status_shows_db_query_failure() {
  setup_env
  # Simulate a broken/failing sqlite3 by placing a stub that exits non-zero.
  # With the updated check_db_staleness(), a non-zero exit code now produces
  # a distinct "DB query failed" warning (separate from the empty-result path),
  # which is the behaviour we want to test here.
  cat > "${_STUB_DIR}/sqlite3" <<'STUB_EOF'
#!/usr/bin/env bash
# Stub: simulate a failing sqlite3 query (rc=1, with an error message).
echo "Error: no such table: signals" >&2
exit 1
STUB_EOF
  chmod +x "${_STUB_DIR}/sqlite3"
  touch "${_TMP}/rf.db"
  local out rc=0
  out="$(run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status (failing sqlite3) exits 0" 0 "${rc}"
  assert_contains "--status: reports DB query failure" "DB query failed" "${out}"
}

test_status_shows_db_fresh() {
  setup_env
  # sqlite3 stub returns today's timestamp (default)
  touch "${_TMP}/rf.db"
  local out rc=0
  out="$(run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status (fresh DB) exits 0" 0 "${rc}"
  assert_contains "--status: fresh DB OK" "[OK] DB writes are recent" "${out}"
}

test_status_shows_db_stale() {
  setup_env
  touch "${_TMP}/rf.db"
  # Return an epoch from 2 days ago (UTC-safe: SQLite now returns epoch seconds).
  local stale_epoch
  stale_epoch=$(date -d "2 days ago" '+%s' 2>/dev/null \
    || date -v -2d '+%s' 2>/dev/null \
    || echo "0")
  local out rc=0
  # Use a 1-second DB_STALE_S threshold so any past epoch triggers it
  out="$(RF_DB_STALE_S=1 STUB_LAST_EPOCH="${stale_epoch}" run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status (stale DB) exits 0" 0 "${rc}"
  assert_contains "--status: stale DB warning" "DB writes STALE" "${out}"
}

test_status_with_metrics_file() {
  setup_env
  # Write a minimal metrics file that matches the actual Prometheus output from
  # render_prometheus_body() in include/meek/metrics.hpp.
  cat > "${_TMP}/metrics.prom" <<'EOF'
rf_frames_total 1234
rf_frames_rejected 42
rf_frames_candidate 100
rf_classifications_total{class="cw_like"} 10
rf_classifications_total{class="fsk_like"} 30
rf_classifications_total{class="psk_qam_like"} 25
rf_classifications_total{class="ook_am_like"} 35
EOF
  local out rc=0
  out="$(run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status with metrics file exits 0" 0 "${rc}"
  assert_contains "--status: shows rf_frames_total" "rf_frames_total" "${out}"
  assert_contains "--status: shows per-class section header" "Per-class frames" "${out}"
  assert_contains "--status: shows cw_like class" "cw_like" "${out}"
  assert_not_contains "--status with metrics: no rf_class_frames in output" "rf_class_frames" "${out}"
}

test_heal_active_service_no_restart() {
  setup_env
  local out rc=0
  out="$(STUB_ACTIVE_STATE=active run_canary --heal)" || rc=$?
  teardown_env
  assert_exit "--heal active service exits 0" 0 "${rc}"
  assert_contains "--heal: reports no recovery needed" "no recovery needed" "${out}"
  assert_not_contains "--heal: no reset-failed on active" "reset-failed" "${out}"
}

test_heal_failed_service_resets_and_starts() {
  setup_env
  local out rc=0
  out="$(STUB_ACTIVE_STATE=failed STUB_IS_ACTIVE=3 run_canary --heal)" || rc=$?
  teardown_env
  assert_exit "--heal failed service exits 0" 0 "${rc}"
  assert_contains "--heal: detects failed state" "FAILED state" "${out}"
  assert_contains "--heal: calls reset-failed" "reset-failed" "${out}"
  assert_contains "--heal: calls start" "start" "${out}"
}

test_heal_inactive_service_starts() {
  setup_env
  local out rc=0
  out="$(STUB_ACTIVE_STATE=inactive STUB_IS_ACTIVE=3 run_canary --heal)" || rc=$?
  teardown_env
  assert_exit "--heal inactive service exits 0" 0 "${rc}"
  assert_contains "--heal: detects inactive state" "INACTIVE" "${out}"
  assert_contains "--heal: starts service" "start" "${out}"
}

test_heal_dry_run_no_systemctl_side_effects() {
  setup_env
  local out rc=0
  out="$(STUB_ACTIVE_STATE=failed STUB_IS_ACTIVE=3 run_canary --heal --dry-run)" || rc=$?
  teardown_env
  assert_exit "--heal --dry-run exits 0" 0 "${rc}"
  assert_contains "--heal --dry-run: shows dry-run prefix" "[dry-run]" "${out}"
  # In dry-run mode the stub systemctl should not be called with real args
  assert_contains "--heal --dry-run: mentions reset-failed" "reset-failed" "${out}"
}

test_invalid_stale_heartbeat_s_falls_back_to_default() {
  setup_env
  echo "ok $(date +%s)" > "${_TMP}/heartbeat"
  local out rc=0
  out="$(RF_STALE_HEARTBEAT_S=5m run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status with invalid RF_STALE_HEARTBEAT_S exits 0" 0 "${rc}"
  assert_contains "--status: warns on invalid STALE_HEARTBEAT_S" "STALE_HEARTBEAT_S" "${out}"
  assert_contains "--status: falls back to default (300)" "using default 300" "${out}"
}

test_invalid_db_stale_s_falls_back_to_default() {
  setup_env
  touch "${_TMP}/rf.db"
  local out rc=0
  out="$(RF_DB_STALE_S=24h run_canary --status)" || rc=$?
  teardown_env
  assert_exit "--status with invalid RF_DB_STALE_S exits 0" 0 "${rc}"
  assert_contains "--status: warns on invalid DB_STALE_S" "DB_STALE_S" "${out}"
  assert_contains "--status: falls back to default (86400)" "using default 86400" "${out}"
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
echo "=== tests/test_canary.sh ==="
test_help_flag
test_unknown_flag_fails
test_status_exits_zero_no_files
test_status_shows_heartbeat_missing_warning
test_status_shows_heartbeat_fresh
test_status_shows_heartbeat_stale
test_status_shows_db_query_failure
test_status_shows_db_fresh
test_status_shows_db_stale
test_status_with_metrics_file
test_heal_active_service_no_restart
test_heal_failed_service_resets_and_starts
test_heal_inactive_service_starts
test_heal_dry_run_no_systemctl_side_effects
test_invalid_stale_heartbeat_s_falls_back_to_default
test_invalid_db_stale_s_falls_back_to_default

echo ""
echo "Results: ${_PASS} passed, ${_FAIL} failed."
if [[ $_FAIL -gt 0 ]]; then
  exit 1
fi
