#!/usr/bin/env bash
# One-time root setup for USB app-hosting (the Fleet page's "Enable app
# hosting" toggle). Installs the sandos-usb-dockerd helper, its templated
# systemd unit, and a NOPASSWD sudoers rule scoped to exactly that one
# helper script — nothing broader. Idempotent: safe to re-run.
#
#   sudo bash setup-usb-dockerd.sh
#
# Why this can't be fully automatic from the dashboard: the Server Manager
# runs as an unprivileged user on purpose (same as every other SandOS
# helper — sandos-usb-bind, sand-* on the node). Granting it the power to
# install NEW sudoers rules on its own would mean anything that can reach
# its HTTP API could hand itself root — a real security regression, not a
# convenience. This script IS the one-time bridge: run it once (per node),
# and "Enable app hosting" then works with a single click forever after.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SUDOERS_FILE=/etc/sudoers.d/62-sandos-usb-dockerd
SM_USER="${SUDO_USER:-control}"

install -m 0755 "$HERE/sandos-usb-dockerd" /usr/local/bin/sandos-usb-dockerd
install -m 0644 "$HERE/sandos-usb-dockerd@.service" /etc/systemd/system/sandos-usb-dockerd@.service
systemctl daemon-reload

echo "${SM_USER} ALL=(root) NOPASSWD: /usr/local/bin/sandos-usb-dockerd" > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE"   # fail loudly rather than leave a broken sudoers file

echo "Done — USB app-hosting is ready. \"Enable app hosting\" on an assigned drive now works without any further setup."
