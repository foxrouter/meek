#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="${HOME}/rf-adapt-intel"
BUILD_DIR="${PROJECT_DIR}/build"
SERVICE="rf-adapt-intel"

export PKG_CONFIG_PATH=/usr/local/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}
echo "PKG_CONFIG_PATH=$PKG_CONFIG_PATH"

echo "Building..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cmake -S "$PROJECT_DIR" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD_DIR" -- -j$(nproc)

echo "Stopping service to avoid Text-file-busy..."
sudo systemctl stop "$SERVICE" || true

echo "Installing binary (atomic move)..."
TMPFILE=$(mktemp /tmp/rf_adapt_intel.XXXXXX)
sudo cp "$BUILD_DIR/rf_adapt_intel" "$TMPFILE"
sudo chmod 755 "$TMPFILE"
sudo mv "$TMPFILE" /usr/local/bin/rf_adapt_intel
sudo chown root:root /usr/local/bin/rf_adapt_intel || true

echo "Reloading dynamic linker cache..."
sudo ldconfig

echo "Reloading systemd units and restarting service..."
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sudo systemctl restart "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager

echo "Showing last 60 journal lines for $SERVICE:"
sudo journalctl -u "$SERVICE" -n 60 --no-pager