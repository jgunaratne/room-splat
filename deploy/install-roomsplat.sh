#!/usr/bin/env bash
# Point https://sea.octo80.com/ at the RoomSplat app (reverse-proxy to the FastAPI
# server on 127.0.0.1:8000) instead of the static LichtFeld viewer. The LAN vhost and
# the deny-all catch-all are unchanged. Roll back from the printed backup at the end.
#
#   sudo /home/junius/git/room-splat/deploy/install-roomsplat.sh
#
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TARGET=/etc/nginx/sites-available/default
BACKUP=$TARGET.bak-$(date +%Y%m%d-%H%M%S)

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

# The app must be up first, or the proxy has nothing to talk to.
if ! curl -fsS -o /dev/null http://127.0.0.1:8000/; then
    echo "!! RoomSplat server is not answering on 127.0.0.1:8000." >&2
    echo "   Start it first:  /home/junius/git/room-splat/server/run-server.sh" >&2
    exit 1
fi

echo "==> backing up $TARGET -> $BACKUP"
cp -a "$TARGET" "$BACKUP"

echo "==> installing RoomSplat vhost"
install -o root -g root -m 644 "$HERE/nginx-roomsplat" "$TARGET"

# A bad config must never be left in place.
if ! nginx -t; then
    echo "!! nginx rejected the new config — restoring $BACKUP" >&2
    cp -a "$BACKUP" "$TARGET"
    nginx -t
    exit 1
fi

echo "==> reloading nginx"
systemctl reload nginx

echo
echo "==> done: https://sea.octo80.com/"
echo "Rolled back with:  sudo cp -a $BACKUP $TARGET && sudo systemctl reload nginx"
