#!/usr/bin/env bash
# tests/test_heartbeat_and_metrics.sh — Shell tests for
# scripts/heartbeat_and_metrics.sh.
#
# Run with:
#   bash tests/test_heartbeat_and_metrics.sh [-v]
#
# Tests run entirely in /tmp; no root, SDR hardware, or network access is
# required.  systemctl is stubbed so the service-up metric can be exercised
# without a live systemd environment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/heartbeat_and_metrics.sh"
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
  _TMP="$(mktemp -d /tmp/rf-heartbeat-test.XXXXXX)"
  _STUB_DIR="$(mktemp -d /tmp/rf-heartbeat-stubs.XXXXXX)"

  # stub: systemctl — is-active exits per STUB_IS_ACTIVE (default 1 = inactive)
  cat > "${_STUB_DIR}/systemctl" <<'EOF'
#!/usr/bin/env bash
IS_ACTIVE="${STUB_IS_ACTIVE:-1}"
case "$*" in
  *"is-active"*)
    exit "${IS_ACTIVE}"
    ;;
  *)
    exit 0
    ;;
esac
EOF
  chmod +x "${_STUB_DIR}/systemctl"
}

teardown_env() {
  rm -rf "${_TMP:-}" "${_STUB_DIR:-}"
}

run_once() {
  # Run the script in once-mode with temp dirs for all output files.
  # Extra env vars can be passed as arguments, e.g. run_once VAR=val ...
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    RF_METRICS_FILE="${_TMP}/metrics.prom" \
    RF_HEARTBEAT_FILE="${_TMP}/heartbeat" \
    RF_WORKER_LOG="${RF_WORKER_LOG:-${_TMP}/worker.log}" \
    "$@" \
    bash "${SCRIPT}" once 2>&1
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test_once_exits_zero() {
  setup_env
  local rc=0
  run_once > /dev/null || rc=$?
  teardown_env
  assert_exit "once mode exits 0" 0 "${rc}"
}

test_heartbeat_file_written() {
  setup_env
  run_once > /dev/null
  local content
  content="$(cat "${_TMP}/heartbeat")"
  teardown_env
  assert_contains "heartbeat file starts with 'ok'" "ok " "${content}"
}

test_heartbeat_file_contains_timestamp() {
  setup_env
  run_once > /dev/null
  local content
  content="$(cat "${_TMP}/heartbeat")"
  teardown_env
  # timestamp is a positive integer following "ok "
  if echo "${content}" | grep -qE '^ok [0-9]+$'; then
    ok "heartbeat file contains unix timestamp"
  else
    fail "heartbeat file contains unix timestamp" "got: ${content}"
  fi
}

test_metrics_file_written() {
  setup_env
  run_once > /dev/null
  local exists=false
  [[ -f "${_TMP}/metrics.prom" ]] && exists=true
  teardown_env
  if ${exists}; then
    ok "metrics.prom file created"
  else
    fail "metrics.prom file created" "file not found"
  fi
}

test_metrics_contains_required_keys() {
  setup_env
  run_once > /dev/null
  local content
  content="$(cat "${_TMP}/metrics.prom")"
  teardown_env
  assert_contains "metrics: rf_worker_up present"               "rf_worker_up"               "${content}"
  assert_contains "metrics: rf_log_frames_total present"        "rf_log_frames_total"        "${content}"
  assert_contains "metrics: rf_log_frames_rejected present"     "rf_log_frames_rejected"     "${content}"
  assert_contains "metrics: rf_log_frames_candidate present"    "rf_log_frames_candidate"    "${content}"
  assert_contains "metrics: rf_log_confidence_avg present"      "rf_log_confidence_avg"      "${content}"
  assert_contains "metrics: rf_heartbeat_timestamp_seconds present" \
    "rf_heartbeat_timestamp_seconds"                             "${content}"
}

test_metrics_contains_help_and_type_comments() {
  setup_env
  run_once > /dev/null
  local content
  content="$(cat "${_TMP}/metrics.prom")"
  teardown_env
  assert_contains "metrics: # HELP lines present" "# HELP" "${content}"
  assert_contains "metrics: # TYPE lines present" "# TYPE" "${content}"
}

test_stdout_contains_heartbeat_prefix() {
  setup_env
  local out
  out="$(run_once)"
  teardown_env
  assert_contains "stdout: [heartbeat] prefix" "[heartbeat]" "${out}"
}

test_stdout_contains_ts_field() {
  setup_env
  local out
  out="$(run_once)"
  teardown_env
  assert_contains "stdout: ts= field present" "ts=" "${out}"
}

test_no_worker_log_totals_zero() {
  setup_env
  # RF_WORKER_LOG points to a non-existent file — totals should all be 0
  local out
  out="$(RF_WORKER_LOG="${_TMP}/nonexistent.log" run_once)"
  teardown_env
  assert_contains "no log: total=0" "total=0" "${out}"
  assert_contains "no log: rejected=0" "rejected=0" "${out}"
  assert_contains "no log: candidates=0" "candidates=0" "${out}"
}

test_worker_log_counts_frames() {
  setup_env
  # Write three synthetic JSON log lines: all three have confidence > 0 so all
  # three count as candidates; one has snr_gate_pass=false so it counts as a
  # rejection.  The script defines candidates as confidence > 0, independent of
  # gate pass, so candidates=3 and rejected=1.
  local log="${_TMP}/worker.log"
  printf '{"mod":"GMSK","confidence":0.80,"snr_gate_pass":true,"bw_gate_pass":true}\n' >> "${log}"
  printf '{"mod":"FSK","confidence":0.60,"snr_gate_pass":false,"bw_gate_pass":true}\n' >> "${log}"
  printf '{"mod":"PSK","confidence":0.75,"snr_gate_pass":true,"bw_gate_pass":true}\n' >> "${log}"
  local out
  out="$(RF_WORKER_LOG="${log}" run_once)"
  teardown_env
  assert_contains "log counting: total=3" "total=3" "${out}"
  assert_contains "log counting: rejected=1" "rejected=1" "${out}"
  assert_contains "log counting: candidates=3" "candidates=3" "${out}"
}

test_service_up_metric_when_active() {
  setup_env
  # STUB_IS_ACTIVE=0 means systemctl is-active returns 0 (= active).
  local content
  content="$(STUB_IS_ACTIVE=0 run_once)"
  local metrics
  metrics="$(cat "${_TMP}/metrics.prom")"
  teardown_env
  assert_contains "service up: rf_worker_up 1" "rf_worker_up 1" "${metrics}"
}

test_service_down_metric_when_inactive() {
  setup_env
  # STUB_IS_ACTIVE=1 means systemctl is-active returns 1 (= inactive).
  run_once > /dev/null
  local metrics
  metrics="$(cat "${_TMP}/metrics.prom")"
  teardown_env
  assert_contains "service down: rf_worker_up 0" "rf_worker_up 0" "${metrics}"
}

test_custom_output_paths_honoured() {
  setup_env
  local alt_metrics="${_TMP}/alt/my_metrics.prom"
  local alt_heartbeat="${_TMP}/alt/my_heartbeat"
  local alt_worker_log="${_TMP}/alt/worker.log"
  env \
    PATH="${_STUB_DIR}:${PATH}" \
    RF_METRICS_FILE="${alt_metrics}" \
    RF_HEARTBEAT_FILE="${alt_heartbeat}" \
    RF_WORKER_LOG="${alt_worker_log}" \
    bash "${SCRIPT}" once > /dev/null
  local metrics_ok=false heartbeat_ok=false
  [[ -f "${alt_metrics}" ]]   && metrics_ok=true
  [[ -f "${alt_heartbeat}" ]] && heartbeat_ok=true
  teardown_env
  if ${metrics_ok}; then
    ok "custom RF_METRICS_FILE path honoured"
  else
    fail "custom RF_METRICS_FILE path honoured" "${alt_metrics} not created"
  fi
  if ${heartbeat_ok}; then
    ok "custom RF_HEARTBEAT_FILE path honoured"
  else
    fail "custom RF_HEARTBEAT_FILE path honoured" "${alt_heartbeat} not created"
  fi
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
echo "=== tests/test_heartbeat_and_metrics.sh ==="
test_once_exits_zero
test_heartbeat_file_written
test_heartbeat_file_contains_timestamp
test_metrics_file_written
test_metrics_contains_required_keys
test_metrics_contains_help_and_type_comments
test_stdout_contains_heartbeat_prefix
test_stdout_contains_ts_field
test_no_worker_log_totals_zero
test_worker_log_counts_frames
test_service_up_metric_when_active
test_service_down_metric_when_inactive
test_custom_output_paths_honoured

echo ""
echo "Results: ${_PASS} passed, ${_FAIL} failed."
if [[ $_FAIL -gt 0 ]]; then
  exit 1
fi
