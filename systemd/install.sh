#!/usr/bin/env bash
# Install + start the Server Manager as a systemd service. Run as root:
#   sudo bash systemd/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT=sandos-server-manager.service
VENV="$(cd "$HERE/.." && pwd)/server/.venv"

# Ensure the venv exists (service runs uvicorn from it).
if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "[*] creating venv…"
  sudo -u "${SUDO_USER:-control}" bash -lc "cd '$HERE/../server' && python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt"
fi

# Stop any detached dev instance holding :8170.
pkill -f "uvicorn app.main" 2>/dev/null || true
sleep 1

cp "$HERE/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable --now "$UNIT"
sleep 2
systemctl --no-pager --lines=6 status "$UNIT" || true

# USB app-hosting (Fleet page's "Enable app hosting" toggle) needs its own
# narrowly-scoped one-time root setup — bundled into the SAME installer so a
# fresh/re-run install never needs a separate manual step. Safe to re-run.
bash "$HERE/../containers/nfs-server/setup-usb-dockerd.sh"

echo
echo "INSTALLED → http://10.0.0.164:8170   (logs: journalctl -u $UNIT -f)"
