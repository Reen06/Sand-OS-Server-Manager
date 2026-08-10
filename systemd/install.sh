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

# Fill the unit template from what this machine actually is. Nothing about the
# host is hardcoded in the shipped file — see the note in the template itself.
SM_USER="${SUDO_USER:-$(id -un)}"
SM_REPO="$(cd "$HERE/.." && pwd)"
SM_PORT="${SM_PORT:-8170}"
SM_LAN_IP="${SM_LAN_IP:-$(ip -4 route get 1.1.1.1 2>/dev/null \
  | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')}"
# The tunnel address, if this machine is on one. Empty is correct and harmless
# when it is not; a leftover value would advertise a TURN relay that cannot
# answer, which fails as a stream that connects and never paints.
SM_TURN_EXTRA_HOST="${SM_TURN_EXTRA_HOST:-$(ip -4 -o addr show 2>/dev/null \
  | awk '$2 ~ /^wg/ {split($4,a,"/"); print a[1]; exit}')}"

sed -e "s|@@SM_USER@@|${SM_USER}|g" \
    -e "s|@@SM_REPO@@|${SM_REPO}|g" \
    -e "s|@@SM_PORT@@|${SM_PORT}|g" \
    -e "s|@@SM_LAN_IP@@|${SM_LAN_IP}|g" \
    -e "s|@@SM_TURN_EXTRA_HOST@@|${SM_TURN_EXTRA_HOST}|g" \
    "$HERE/$UNIT" > "/etc/systemd/system/$UNIT"
if grep -q '@@' "/etc/systemd/system/$UNIT"; then
  echo "[!] unit still has unsubstituted placeholders:" >&2
  grep -n '@@' "/etc/systemd/system/$UNIT" >&2
  exit 1
fi
systemctl daemon-reload
systemctl enable --now "$UNIT"
sleep 2
systemctl --no-pager --lines=6 status "$UNIT" || true

# USB app-hosting (Fleet page's "Enable app hosting" toggle) needs its own
# narrowly-scoped one-time root setup — bundled into the SAME installer so a
# fresh/re-run install never needs a separate manual step. Safe to re-run.
bash "$HERE/../containers/nfs-server/setup-usb-dockerd.sh"

# docker0's NAT/FORWARD rules have been observed missing after dockerd
# restarts (see the ensure script's own comment for the full story) —
# install the oneshot fixer so every future boot self-heals instead of
# silently losing container-initiated outbound internet access again.
cp "$HERE/sandos-docker0-forward-fix.service" /etc/systemd/system/
chmod +x "$HERE/ensure-docker0-forward-rules.sh"
systemctl daemon-reload
systemctl enable --now sandos-docker0-forward-fix.service

echo
echo "INSTALLED → http://${SM_LAN_IP:-localhost}:${SM_PORT}   (logs: journalctl -u $UNIT -f)"
