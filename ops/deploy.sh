#!/usr/bin/env bash
# ops/deploy.sh — Atomic install of process-worker systemd unit + drop-ins
# Usage: sudo bash ops/deploy.sh [--dry-run] [--setup]
#
# Options:
#   --dry-run   Print actions without executing them.
#   --setup     Run ops/setup.sh first to install optional decoders (interactive).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="process-worker"
UNIT_SRC="${REPO_ROOT}/systemd/${SERVICE}.service"
DROPIN_SRC="${REPO_ROOT}/systemd/${SERVICE}.service.d"
UNIT_DST="/etc/systemd/system/${SERVICE}.service"
DROPIN_DST="/etc/systemd/system/${SERVICE}.service.d"
MONITOR_SVC_SRC="${REPO_ROOT}/systemd/rf-adapt-intel-monitor.service"
MONITOR_TMR_SRC="${REPO_ROOT}/systemd/rf-adapt-intel-monitor.timer"
MONITOR_SHARE="/usr/local/share/rf-adapt-intel"

DRY_RUN=false
RUN_SETUP=false
for _arg in "$@"; do
  case "$_arg" in
    --dry-run) DRY_RUN=true ;;
    --setup)   RUN_SETUP=true ;;
    *)
      echo "[ERROR] Unknown option: ${_arg}" >&2
      exit 1
      ;;
  esac
done
if $DRY_RUN; then
  echo "[dry-run] No changes will be written."
fi

# Runtime data directory.  Override via _RF_DATA_DIR for testing so the
# symlink/permission checks can exercise real filesystem paths without
# touching /var/lib/ on the test host.
_RF_DATA_DIR="${_RF_DATA_DIR:-/var/lib/rf-adapt-intel}"

# Sanity-check _RF_DATA_DIR to avoid operating on unintended locations.
if [[ "${_RF_DATA_DIR}" != /* ]]; then
  echo "[ERROR] _RF_DATA_DIR must be an absolute path, got: ${_RF_DATA_DIR}" >&2
  exit 1
fi
# Non-default _RF_DATA_DIR is test-only; refuse it in production deploys.
if [[ "${_RF_DATA_DIR}" != "/var/lib/rf-adapt-intel" ]] && ! $DRY_RUN; then
  echo "[ERROR] _RF_DATA_DIR may only be overridden in --dry-run mode." >&2
  echo "        Production deploys always use /var/lib/rf-adapt-intel." >&2
  exit 1
fi
# Use -L before -e: -e is false for dangling symlinks, so check -L first.
if [[ -L "${_RF_DATA_DIR}" ]]; then
  echo "[ERROR] _RF_DATA_DIR must not be a symlink: ${_RF_DATA_DIR}" >&2
  exit 1
fi
if [[ -e "${_RF_DATA_DIR}" && ! -d "${_RF_DATA_DIR}" ]]; then
  echo "[ERROR] _RF_DATA_DIR exists but is not a directory: ${_RF_DATA_DIR}" >&2
  exit 1
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

# Optional: run interactive decoder setup before deploying the service
if $RUN_SETUP; then
  echo ""
  echo "--- Running optional decoder setup (ops/setup.sh) ---"
  SETUP_ARGS=()
  $DRY_RUN && SETUP_ARGS+=(--dry-run)
  # Pass --non-interactive when stdin is not a TTY (e.g. CI, scripted deploy)
  [[ ! -t 0 ]] && SETUP_ARGS+=(--non-interactive)
  bash "${REPO_ROOT}/ops/setup.sh" "${SETUP_ARGS[@]}"
  echo "--- Decoder setup done ---"
  echo ""
fi

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

# Create service account first so directory ownership can be set correctly
if ! id rf_worker &>/dev/null; then
  echo "Creating rf_worker system account..."
  run sudo useradd -r -s /sbin/nologin rf_worker
fi
# Ensure rf_worker is in plugdev so the udev rule (GROUP="plugdev", MODE="0664")
# grants it access to the RTL-SDR USB device.
if ! id -nG rf_worker 2>/dev/null | grep -qw plugdev; then
  echo "Adding rf_worker to plugdev group (required for RTL-SDR USB access)..."
  run sudo usermod -aG plugdev rf_worker
fi

# Create runtime data directories owned by rf_worker.
#
# To close a TOCTOU window: first lock the parent to 0750 (no group-write),
# then validate + create each subdirectory while the parent is non-writable by
# the group, and only re-enable g+w on the parent once all subdirs are in place.
# This prevents a group member from pre-planting a symlink and having a
# subsequent privileged install/chmod follow it.

# Step 1 — ensure the parent exists and is NOT group-writable during setup.
run sudo install -d -m 0750 -o rf_worker -g rf_worker "${_RF_DATA_DIR}"

# Step 2 — validate each subdir and create it immediately after its check,
#           while the parent is still locked to 0750.
for _dir in snapshots incoming processed; do
  _subdir="${_RF_DATA_DIR}/${_dir}"
  if [[ -L "${_subdir}" ]]; then
    echo "[ERROR] ${_subdir} is a symlink — refusing to operate on it." >&2
    echo "        Remove the symlink manually and re-run deploy.sh:" >&2
    echo "          sudo rm -f '${_subdir}'" >&2
    exit 1
  fi
  if [[ -e "${_subdir}" && ! -d "${_subdir}" ]]; then
    echo "[ERROR] ${_subdir} exists but is not a directory (unexpected file type)." >&2
    echo "        Remove it manually and re-run deploy.sh:" >&2
    echo "          sudo rm -f '${_subdir}'" >&2
    exit 1
  fi
  run sudo install -d -m 0750 -o rf_worker -g rf_worker "${_subdir}"
done

# Step 3 — all subdirs are verified; now grant group-write on the parent.
run sudo chmod g+w "${_RF_DATA_DIR}"

# Set up the SSH directory for rf_worker under ${_RF_DATA_DIR}/.ssh/
# Note: iq-transfer-watcher.service hard-codes HOME=/var/lib/rf-adapt-intel
# (and WorkingDirectory=/var/lib/rf-adapt-intel); _RF_DATA_DIR is test-only
# unless the unit files are updated to match.  SSH and rsync store keys and
# known_hosts under that fixed path, which is writable under ReadWritePaths
# (ProtectHome=yes blocks the real home dir).
#
# Guard against a symlink at .ssh: rf_worker owns the parent directory, so a
# compromised rf_worker could plant a symlink here.  Following it with a
# privileged chown/chmod could affect an unintended path.  Fail hard and ask
# the operator to remove the symlink manually before re-running deploy.
_SSH_DIR="${_RF_DATA_DIR}/.ssh"
if ! $DRY_RUN && [[ -L "${_SSH_DIR}" ]]; then
  echo "[ERROR] ${_SSH_DIR} is a symlink — refusing to operate on it." >&2
  echo "        Remove the symlink manually and re-run deploy.sh:" >&2
  echo "          sudo rm -f '${_SSH_DIR}'" >&2
  exit 1
fi
if ! $DRY_RUN && [[ -e "${_SSH_DIR}" && ! -d "${_SSH_DIR}" ]]; then
  echo "[ERROR] ${_SSH_DIR} exists but is not a directory (unexpected file type)." >&2
  echo "        Remove it manually and re-run deploy.sh:" >&2
  echo "          sudo rm -f '${_SSH_DIR}'" >&2
  exit 1
fi
# Use `install -d` which sets ownership and mode in a single command,
# reducing the window between creation and permission-setting compared to
# separate mkdir/chown/chmod calls.  The guards above ensure this path is
# not a symlink or an unexpected non-directory file.
run sudo install -d -m 0700 -o rf_worker -g rf_worker "${_SSH_DIR}"
# Generate an Ed25519 key pair for rf_worker if one does not already exist.
if ! $DRY_RUN; then
  priv_key="${_SSH_DIR}/id_ed25519"
  # Refuse to use a symlink or non-regular file for the private key path to
  # avoid an attacker-controlled target being treated as an existing key.
  if sudo test -L "${priv_key}"; then
    echo "[ERROR] ${priv_key} is a symlink; refusing to use it as the rf_worker SSH private key." >&2
    echo "        Remove the symlink and re-run this deploy script to generate a fresh key:" >&2
    echo "          sudo rm -f '${priv_key}'" >&2
    exit 1
  elif sudo test -e "${priv_key}" && ! sudo test -f "${priv_key}"; then
    echo "[ERROR] ${priv_key} exists but is not a regular file (unexpected file type)." >&2
    echo "        Remove it and re-run this deploy script to generate a fresh key:" >&2
    echo "          sudo rm -f '${priv_key}'" >&2
    exit 1
  elif ! sudo test -e "${priv_key}"; then
    echo "Generating SSH key pair for rf_worker..."
    sudo -u rf_worker \
      HOME="${_RF_DATA_DIR}" \
      ssh-keygen -t ed25519 -N "" \
        -f "${priv_key}" \
        -C "rf_worker@$(hostname -s 2>/dev/null || echo localhost)"
  fi
  echo ""
  echo "=== rf_worker SSH public key ==="
  echo "Copy this key to Brian's /home/rf_worker/.ssh/authorized_keys (or the"
  echo "authorized_keys file for whatever user owns the destination path):"
  echo ""
  pub_key="${_SSH_DIR}/id_ed25519.pub"
  if sudo -u rf_worker test -L "${pub_key}"; then
    echo "[ERROR] ${pub_key} is a symlink; refusing to print target contents." >&2
    echo "        Remove the symlink and regenerate the key with ssh-keygen as rf_worker." >&2
    echo "        Then re-run this deploy script to verify the public key." >&2
    exit 1
  elif ! sudo -u rf_worker test -f "${pub_key}"; then
    echo "[ERROR] Expected SSH public key ${pub_key} to be a regular file but it is missing or not a file." >&2
    echo "        Regenerate the key with ssh-keygen as rf_worker, then re-run this deploy script." >&2
    exit 1
  else
    sudo -u rf_worker cat "${pub_key}"
  fi
  echo ""
  echo "  On Brian, log in as the destination user and append the public key above to:"
  echo "    ~/.ssh/authorized_keys    # on Brian"
  echo "  (This deploy step only generates/verifies the key on this node; it does not"
  echo "   install it into any remote authorized_keys file.)"
  echo ""
  echo "  Optional: from the rf_adapt_intel repo checkout on Brian, run:"
  echo "    sudo bash scripts/check_ssh_permissions.sh --fix"
  echo "  to tighten SSH directory/file permissions. That script will also print a"
  echo "  helper command you can use instead of manually editing authorized_keys."
else
  echo "[dry-run] Would generate SSH key pair for rf_worker if absent"
fi

# Verify the directory is accessible by rf_worker before starting the service
if ! $DRY_RUN; then
  if ! sudo -u rf_worker test -w "${_RF_DATA_DIR}"; then
    echo "[WARN] ${_RF_DATA_DIR} is not writable by rf_worker — check ownership"
    ls -la "$(dirname "${_RF_DATA_DIR}")" | grep "$(basename "${_RF_DATA_DIR}")" || true
  else
    echo "    [OK] ${_RF_DATA_DIR} is writable by rf_worker"
  fi
fi

# Install canary monitor service + 30-minute timer
echo ""
echo "=== Installing canary monitor timer ==="
run sudo install -m 644 -o root -g root "${MONITOR_SVC_SRC}" \
    "/etc/systemd/system/rf-adapt-intel-monitor.service"
run sudo install -m 644 -o root -g root "${MONITOR_TMR_SRC}" \
    "/etc/systemd/system/rf-adapt-intel-monitor.timer"
# Install monitor service hardening drop-in
MONITOR_DROPIN_SRC="${REPO_ROOT}/systemd/rf-adapt-intel-monitor.service.d"
MONITOR_DROPIN_DST="/etc/systemd/system/rf-adapt-intel-monitor.service.d"
run sudo mkdir -p "${MONITOR_DROPIN_DST}"
run sudo install -m 644 -o root -g root \
    "${MONITOR_DROPIN_SRC}/hardening.conf" \
    "${MONITOR_DROPIN_DST}/hardening.conf"
# Install ops scripts into /usr/local/share for the monitor service to call
run sudo mkdir -p "${MONITOR_SHARE}/ops"
run sudo install -m 755 -o root -g root "${REPO_ROOT}/ops/canary.sh" \
    "${MONITOR_SHARE}/ops/canary.sh"

# Reload first so both the updated monitor unit (no Wants= dependency) and the
# main service unit are picked up before any `enable --now` fires the timer.
run sudo systemctl daemon-reload
run sudo systemctl enable --now rf-adapt-intel-monitor.timer

# Enable process-worker (create the WantedBy symlink without starting yet)
run sudo systemctl enable "${SERVICE}"

# Roll out unit changes on already-running instances and ensure the service
# is running on freshly provisioned nodes.  We skip start/restart when the
# ExecStart binary is absent to avoid consuming StartLimitBurst on a
# guaranteed failure (the unit is still enabled and will start automatically
# once the binary is installed).
_EXEC_BIN=/usr/local/bin/rf_adapt_intel
if $DRY_RUN; then
  if [[ -x "${_EXEC_BIN}" ]]; then
    echo "[dry-run] sudo systemctl try-restart ${SERVICE}"
    echo "[dry-run] sudo systemctl start ${SERVICE}"
  else
    echo "[dry-run] skip start — ${_EXEC_BIN} not found; unit enabled, will start after install"
  fi
elif [[ ! -x "${_EXEC_BIN}" ]]; then
  echo ""
  echo "[WARN] ${_EXEC_BIN} not found — skipping start of ${SERVICE}."
  echo "       The unit is enabled and will start automatically once the binary is installed."
else
  # Binary is present.
  #   1. try-restart: if already active, restart immediately so unit/drop-in
  #      changes take effect.  No-op (exits 0) when inactive.
  #   2. start: always issued so the service comes up on freshly provisioned
  #      nodes; idempotent when already running after step 1.
  # Failures are non-fatal.
  # NOTE: Restart=always retries automatically, but StartLimitBurst=5 within
  # StartLimitIntervalSec=120s caps consecutive fast failures.  If the start
  # limit is hit, run the following to unblock automatic restarts:
  #   sudo systemctl reset-failed ${SERVICE} && sudo systemctl start ${SERVICE}
  sudo systemctl try-restart "${SERVICE}" || true
  _start_rc=0
  sudo systemctl start "${SERVICE}" || _start_rc=$?
  if [[ ${_start_rc} -ne 0 ]]; then
    echo ""
    echo "[WARN] ${SERVICE} failed to start (exit ${_start_rc})."
    echo "       This is usually caused by a missing SDR device or a unit misconfiguration."
    echo "       The service is enabled and will retry (Restart=always,"
    echo "       StartLimitBurst=5 per 120 s window)."
    echo "       If the start limit is reached, unblock with:"
    echo "         sudo systemctl reset-failed ${SERVICE}"
    echo "         sudo systemctl start ${SERVICE}"
  fi
fi

echo ""
echo "=== Service status ==="
run sudo systemctl status "${SERVICE}" --no-pager -l || true
echo ""
echo "=== Recent journal ==="
run sudo journalctl -u "${SERVICE}" -n 40 --no-pager || true
