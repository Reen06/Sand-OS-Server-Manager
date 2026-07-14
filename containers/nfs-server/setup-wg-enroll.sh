#!/usr/bin/env bash
# One-time root setup for enrolling THIS Server Manager into a SandOS Hub's
# fleet over a scoped WireGuard tunnel — for a rented VPS, a school server,
# or any box that won't get a full Hub install. Installs the privileged
# helper + a NOPASSWD sudoers rule scoped to exactly its two fixed
# invocations, ensures wireguard-tools is present, and creates the staging
# dir the enrollment CLI writes its fetched conf into. Idempotent: safe to
# re-run.
#
#   sudo bash setup-wg-enroll.sh
#
# Same reasoning as setup-usb-dockerd.sh: the Server Manager runs
# unprivileged on purpose, so it can never install its own new sudoers
# rules — this script is the one-time human-run bridge, after which
# enrollment works with one CLI command, once, forever.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SUDOERS_FILE=/etc/sudoers.d/63-sandos-wg-enroll
SM_USER="${SUDO_USER:-control}"
STAGED_CONF=/etc/sandos/wg-enroll-staging.conf

if ! command -v wg-quick >/dev/null; then
  echo "[*] installing wireguard-tools…"
  apt-get update -qq && apt-get install -y -qq wireguard-tools
fi

mkdir -p /etc/sandos
chown "${SM_USER}:${SM_USER}" /etc/sandos

install -m 0755 "$HERE/sandos-wg-enroll" /usr/local/bin/sandos-wg-enroll

{
  echo "${SM_USER} ALL=(root) NOPASSWD: /usr/local/bin/sandos-wg-enroll up ${STAGED_CONF}"
  echo "${SM_USER} ALL=(root) NOPASSWD: /usr/local/bin/sandos-wg-enroll down"
} > "$SUDOERS_FILE"
chmod 440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE"   # fail loudly rather than leave a broken sudoers file

echo "Done — remote-enrollment is ready. To join a Hub's fleet, run:"
echo "  curl -s \"<enrollment_url>\" -o ${STAGED_CONF} && sudo sandos-wg-enroll up ${STAGED_CONF}"
