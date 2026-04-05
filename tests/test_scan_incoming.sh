#!/usr/bin/env bash
# tests/test_scan_incoming.sh - Smoke tests for scripts/scan_incoming.sh
#
# scan_incoming.sh is configured via environment variables not CLI flags:
#   INCOMING_DIR    Directory to scan
#   PROCESSED_DIR   Move target after processing
#   PROCESS_SCRIPT  Path to process_incoming.sh (stubbed in tests)
#
# Run with:
#   bash tests/test_scan_incoming.sh [-v]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_INCOMING="$REPO_ROOT/scripts/scan_incoming.sh"
PASS=0
FAIL=0
VERBOSE=0

# Validate arguments
for arg in "$@"; do
    if [ "$arg" != "-v" ]; then
        echo "Usage: $0 [-v]" >&2
        exit 1
    fi
done
[ "${1:-}" = "-v" ] && VERBOSE=1

log()  { [ "$VERBOSE" -eq 1 ] && echo "[INFO] $*" || true; }
ok()   { [ "$VERBOSE" -eq 1 ] && echo "OK: $1" || true; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1" >&2; FAIL=$((FAIL + 1)); }

if [ ! -f "$SCAN_INCOMING" ]; then
    echo "FATAL: scan_incoming.sh not found at $SCAN_INCOMING"
    exit 2
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

INCOMING="$TMP/incoming"
PROCESSED="$TMP/processed"
STUB="$TMP/process_stub.sh"
mkdir -p "$INCOMING" "$PROCESSED"

# Stub process_incoming.sh that always succeeds
cat > "$STUB" <<'STUB_EOF'
#!/usr/bin/env bash
exit 0
STUB_EOF
chmod +x "$STUB"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# create_test_iq_file <path> — write a minimal CF32 file (32 samples = 256 bytes)
create_test_iq_file() {
    python3 -c "
import struct, math, sys
data = b''
for i in range(32):
    data += struct.pack('<ff', math.cos(i * 0.1), math.sin(i * 0.1))
open(sys.argv[1], 'wb').write(data)
" "$1"
}

# Pre-populate INCOMING for the first test that needs it
create_test_iq_file "$INCOMING/test_signal.cf32"

run_scan() {
    INCOMING_DIR="$INCOMING" \
    PROCESSED_DIR="$PROCESSED" \
    PROCESS_SCRIPT="$STUB" \
    bash "$SCAN_INCOMING" 2>&1
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test_empty_dir() {
    local empty_dir out rc=0
    empty_dir="$(mktemp -d)"
    out="$(INCOMING_DIR="$empty_dir" PROCESSED_DIR="$PROCESSED" PROCESS_SCRIPT="$STUB" \
           bash "$SCAN_INCOMING" 2>&1)" || rc=$?
    rm -rf "$empty_dir"
    if [ "$rc" -eq 0 ]; then
        ok "empty dir: exits 0"
    else
        fail "empty dir: expected exit 0, got $rc"
    fi
    if echo "$out" | grep -q "0 processed, 0 failed"; then
        ok "empty dir: reports 0 processed, 0 failed"
    else
        fail "empty dir: expected '0 processed, 0 failed' in output"
        log "output was: $out"
    fi
}

test_cf32_file_processed_and_moved() {
    # Reset state: ensure test_signal.cf32 is in INCOMING
    create_test_iq_file "$INCOMING/test_signal.cf32"
    local out rc=0
    out="$(run_scan)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        ok "cf32 file: exits 0"
    else
        fail "cf32 file: expected exit 0, got $rc"
    fi
    if echo "$out" | grep -q "1 processed, 0 failed"; then
        ok "cf32 file: reports 1 processed, 0 failed"
    else
        fail "cf32 file: expected '1 processed, 0 failed'"
        log "output was: $out"
    fi
    if [ ! -f "$INCOMING/test_signal.cf32" ] && [ -f "$PROCESSED/test_signal.cf32" ]; then
        ok "cf32 file: moved to PROCESSED_DIR"
    else
        fail "cf32 file: expected file in PROCESSED_DIR and absent from INCOMING_DIR"
    fi
    # Clean up for subsequent tests
    rm -f "$PROCESSED/test_signal.cf32"
}

test_raw_file_processed_and_moved() {
    create_test_iq_file "$INCOMING/test_signal.raw"
    local out rc=0
    out="$(run_scan)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        ok "raw file: exits 0"
    else
        fail "raw file: expected exit 0, got $rc"
    fi
    if [ ! -f "$INCOMING/test_signal.raw" ] && [ -f "$PROCESSED/test_signal.raw" ]; then
        ok "raw file: moved to PROCESSED_DIR"
    else
        fail "raw file: expected file in PROCESSED_DIR and absent from INCOMING_DIR"
    fi
    rm -f "$PROCESSED/test_signal.raw"
}

test_failed_process_still_moves_file() {
    # Stub that always fails
    local fail_stub="$TMP/fail_stub.sh"
    cat > "$fail_stub" <<'FAIL_EOF'
#!/usr/bin/env bash
exit 1
FAIL_EOF
    chmod +x "$fail_stub"

    create_test_iq_file "$INCOMING/fail_signal.cf32"
    local out rc=0
    out="$(INCOMING_DIR="$INCOMING" PROCESSED_DIR="$PROCESSED" PROCESS_SCRIPT="$fail_stub" \
           bash "$SCAN_INCOMING" 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        ok "failed process: scan exits 0 despite PROCESS_SCRIPT failure"
    else
        fail "failed process: expected exit 0, got $rc"
    fi
    if echo "$out" | grep -q "0 processed, 1 failed"; then
        ok "failed process: reports 0 processed, 1 failed"
    else
        fail "failed process: expected '0 processed, 1 failed'"
        log "output was: $out"
    fi
    if [ ! -f "$INCOMING/fail_signal.cf32" ] && [ -f "$PROCESSED/fail_signal.cf32" ]; then
        ok "failed process: file still moved to PROCESSED_DIR"
    else
        fail "failed process: expected file moved to PROCESSED_DIR even on PROCESS_SCRIPT failure"
    fi
    rm -f "$PROCESSED/fail_signal.cf32"
}

test_processed_dir_created_if_absent() {
    local new_processed="$TMP/new_processed_dir"
    create_test_iq_file "$INCOMING/create_dir.cf32"
    local out rc=0
    out="$(INCOMING_DIR="$INCOMING" PROCESSED_DIR="$new_processed" PROCESS_SCRIPT="$STUB" \
           bash "$SCAN_INCOMING" 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        ok "auto-create PROCESSED_DIR: exits 0"
    else
        fail "auto-create PROCESSED_DIR: expected exit 0, got $rc"
        log "output was: $out"
    fi
    if [ -d "$new_processed" ]; then
        ok "auto-create PROCESSED_DIR: directory created"
    else
        fail "auto-create PROCESSED_DIR: expected directory to be created"
    fi
    if [ ! -f "$INCOMING/create_dir.cf32" ]; then
        ok "auto-create PROCESSED_DIR: source file removed from INCOMING_DIR"
    else
        fail "auto-create PROCESSED_DIR: source file should be moved from INCOMING_DIR"
    fi
    if [ -f "$new_processed/create_dir.cf32" ]; then
        ok "auto-create PROCESSED_DIR: file moved to processed directory"
    else
        fail "auto-create PROCESSED_DIR: expected file in created PROCESSED_DIR"
    fi
    rm -rf "$new_processed"
    rm -f "$INCOMING/create_dir.cf32"
}

test_non_iq_files_ignored() {
    echo "not an IQ file" > "$INCOMING/readme.txt"
    local out rc=0
    out="$(run_scan)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        ok "non-IQ files: exits 0"
    else
        fail "non-IQ files: expected exit 0, got $rc"
    fi
    if echo "$out" | grep -q "0 processed, 0 failed"; then
        ok "non-IQ files: .txt file not processed"
    else
        fail "non-IQ files: expected '0 processed, 0 failed'"
        log "output was: $out"
    fi
    if [ -f "$INCOMING/readme.txt" ]; then
        ok "non-IQ files: .txt file not moved"
    else
        fail "non-IQ files: .txt file should remain in INCOMING_DIR"
    fi
    rm -f "$INCOMING/readme.txt"
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
echo "=== tests/test_scan_incoming.sh ==="
test_empty_dir
test_cf32_file_processed_and_moved
test_raw_file_processed_and_moved
test_failed_process_still_moves_file
test_processed_dir_created_if_absent
test_non_iq_files_ignored

echo ""
echo "Results: $PASS passed, $FAIL failed."
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
