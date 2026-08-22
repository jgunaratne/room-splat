#!/usr/bin/env bash
# Install and enable the systemd unit so RoomSplat survives reboot.
#
#   sudo /home/junius/git/room-splat/deploy/install-service.sh
#
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TARGET=/etc/systemd/system/roomsplat.service

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

echo "==> installing systemd unit to $TARGET"
install -o root -g root -m 644 "$HERE/roomsplat.service" "$TARGET"

echo "==> reloading systemd daemon"
systemctl daemon-reload

echo "==> enabling and starting roomsplat.service"
systemctl enable --now roomsplat

echo "==> roomsplat.service status:"
systemctl status roomsplat --no-pager || true

echo
echo "==> done: systemctl status roomsplat"
