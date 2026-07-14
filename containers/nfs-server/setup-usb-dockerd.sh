#!/usr/bin/env bash
# One-time root setup for USB app-hosting AND the drive-provisioning wizard
# (Fleet page's "Enable app hosting" toggle + "Provision…"/"Repartition…").
# Installs both privileged helpers, the dockerd systemd template, the
# exFAT/ext4 tools they need, and NOPASSWD sudoers rules scoped to exactly
# those two helper scripts — nothing broader. Idempotent: safe to re-run.
#
#   sudo bash setup-usb-dockerd.sh
#
# Why this can't be fully automatic from the dashboard: the Server Manager
# runs as an unprivileged user on purpose (same as every other SandOS
# helper — sandos-usb-bind, sand-* on the node). Granting it the power to
# install NEW sudoers rules on its own would mean anything that can reach
# its HTTP API could hand itself root — a real security regression, not a
# convenience. This script IS the one-time bridge: run it once (per node),
# and every USB feature then works with a single click forever after.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SUDOERS_FILE=/etc/sudoers.d/62-sandos-usb-dockerd
SM_USER="${SUDO_USER:-control}"

if ! command -v mkfs.exfat >/dev/null || ! command -v mkfs.ext4 >/dev/null; then
  echo "[*] installing exfatprogs + e2fsprogs…"
  apt-get update -qq && apt-get install -y -qq exfatprogs e2fsprogs
fi

install -m 0755 "$HERE/sandos-usb-dockerd" /usr/local/bin/sandos-usb-dockerd
install -m 0644 "$HERE/sandos-usb-dockerd@.service" /etc/systemd/system/sandos-usb-dockerd@.service
install -m 0644 "$HERE/sandos-usb-containerd@.service" /etc/systemd/system/sandos-usb-containerd@.service
install -m 0755 "$HERE/sandos-usb-provision" /usr/local/bin/sandos-usb-provision
systemctl daemon-reload

{
  echo "${SM_USER} ALL=(root) NOPASSWD: /usr/local/bin/sandos-usb-dockerd"
  echo "${SM_USER} ALL=(root) NOPASSWD: /usr/local/bin/sandos-usb-provision"
} > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE"   # fail loudly rather than leave a broken sudoers file

echo "Done — USB app-hosting + drive provisioning are ready, no further setup needed."
