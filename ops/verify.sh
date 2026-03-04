#!/usr/bin/env bash
# ops/verify.sh — Verify process-worker.service hardening properties
# Usage: sudo bash ops/verify.sh
set -euo pipefail

SERVICE="process-worker"

echo "=== ${SERVICE} hardening verification ==="

check() {
  local key="$1"
  local expected="$2"
  local actual
  actual=$(systemctl show "${SERVICE}" --property="${key}" 2>/dev/null | cut -d= -f2-)
  if [[ "${actual}" == "${expected}" ]]; then
    echo "  [PASS] ${key}=${actual}"
  else
    echo "  [FAIL] ${key}=${actual} (expected '${expected}')"
  fi
}

echo ""
echo "--- Filesystem protection ---"
check ProtectSystem           "full"
check ProtectHome             "yes"
check PrivateTmp              "yes"

echo ""
echo "--- Privilege reduction ---"
check NoNewPrivileges         "yes"
check MemoryDenyWriteExecute  "yes"
check RestrictSUIDSGID        "yes"
check RestrictNamespaces      "yes"
check LockPersonality         "yes"

echo ""
echo "--- Kernel protection ---"
check ProtectClock            "yes"
check ProtectKernelLogs       "yes"
check ProtectKernelModules    "yes"

echo ""
echo "--- Resource limits ---"
check LimitNOFILE             "4096"
check TasksMax                "2048"

echo ""
echo "--- Service status ---"
sudo systemctl status "${SERVICE}" --no-pager -l || true

echo ""
echo "--- ReadWritePaths ---"
systemctl show "${SERVICE}" --property=ReadWritePaths

echo ""
echo "=== Verify complete ==="
