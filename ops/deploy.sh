#!/usr/bin/env bash
# ops/deploy.sh — Atomic install of process-worker systemd unit + drop-ins
# Usage: sudo bash ops/deploy.sh [--dry-run]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="process-worker"
UNIT_SRC="${REPO_ROOT}/systemd/${SERVICE}.service"
DROPIN_SRC="${REPO_ROOT}/systemd/${SERVICE}.service.d"
UNIT_DST="/etc/systemd/system/${SERVICE}.service"
DROPIN_DST="/etc/systemd/system/${SERVICE}.service.d"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  echo "[dry-run] No changes will be written."
fi

run() {
  if $DRY_RUN; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

echo "=== rf_adapt_intel deploy ==="
echo "Unit src : ${UNIT_SRC}"
echo "Drop-in  : ${DROPIN_SRC}"

# Backup existing unit if present
if [[ -f "${UNIT_DST}" ]]; then
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  BACKUP="/root/${SERVICE}.service.bak.${TS}"
  run sudo cp "${UNIT_DST}" "${BACKUP}"
  echo "Backed up existing unit to ${BACKUP}"
fi

# Install unit file
run sudo install -m 644 -o root -g root "${UNIT_SRC}" "${UNIT_DST}"

# Install drop-in directory atomically via temp dir
TMPDIR=$(mktemp -d /tmp/pwdrop.XXXXXX)
trap 'rm -rf "${TMPDIR}"' EXIT
for conf in hardening.conf override.conf processor.conf; do
  if [[ -f "${DROPIN_SRC}/${conf}" ]]; then
    run sudo install -m 644 -o root -g root "${DROPIN_SRC}/${conf}" "${TMPDIR}/${conf}"
  fi
done
run sudo mkdir -p "${DROPIN_DST}"
for conf in "${TMPDIR}"/*.conf; do
  [[ -f "${conf}" ]] || continue
  run sudo install -m 644 -o root -g root "${conf}" "${DROPIN_DST}/$(basename "${conf}")"
done

# Create runtime data directory
run sudo mkdir -p /var/lib/rf-adapt-intel/{snapshots,incoming,processed}
if id rf_worker &>/dev/null; then
  run sudo chown -R rf_worker:rf_worker /var/lib/rf-adapt-intel
else
  echo "[WARN] User rf_worker does not exist — skipping chown. Create with: useradd -r -s /sbin/nologin rf_worker"
fi

# Reload and restart
run sudo systemctl daemon-reload
run sudo systemctl enable --now "${SERVICE}"
run sudo systemctl restart "${SERVICE}"

echo ""
echo "=== Service status ==="
run sudo systemctl status "${SERVICE}" --no-pager -l || true
echo ""
echo "=== Recent journal ==="
run sudo journalctl -u "${SERVICE}" -n 40 --no-pager || true
