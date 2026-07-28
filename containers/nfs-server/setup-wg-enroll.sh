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
# Whoever is really driving this. The old default was a hardcoded "control",
# the account name on the machine this was first written for — so on a box
# logged in AS root (no SUDO_USER set), which is the normal case for a rented
# VPS or any remote server, it resolved to a user that does not exist. The
# chown below then failed, and with set -e the script aborted BEFORE installing
# the helper or writing the sudoers rule, leaving install.sh's next step to die
# with "sandos-wg-enroll: command not found".
SM_USER="${SUDO_USER:-$(id -un)}"
SM_GROUP="$(id -gn "$SM_USER" 2>/dev/null || echo "$SM_USER")"
if ! id "$SM_USER" >/dev/null 2>&1; then
  echo "error: user '$SM_USER' does not exist on this system" >&2
  exit 1
fi
STAGED_CONF=/etc/sandos/wg-enroll-staging.conf

if ! command -v wg-quick >/dev/null; then
  echo "[*] installing wireguard-tools…"
  apt-get update -qq && apt-get install -y -qq wireguard-tools
fi

mkdir -p /etc/sandos
chown "${SM_USER}:${SM_GROUP}" /etc/sandos

install -m 0755 "$HERE/sandos-wg-enroll" /usr/local/bin/sandos-wg-enroll

# root already has every privilege this rule would grant, so writing one is
# noise at best and a confusing artefact at worst. Only non-root service users
# need it.
if [ "$SM_USER" = "root" ]; then
  rm -f "$SUDOERS_FILE"
  echo "[*] running as root — no sudoers rule needed"
else
  # Build and validate in a temp file: a malformed rule dropped straight into
  # /etc/sudoers.d can break sudo system-wide, so it must never be written live
  # even briefly.
  _tmp="$(mktemp)"
  {
    echo "${SM_USER} ALL=(root) NOPASSWD: /usr/local/bin/sandos-wg-enroll up ${STAGED_CONF}"
    echo "${SM_USER} ALL=(root) NOPASSWD: /usr/local/bin/sandos-wg-enroll down"
  } > "$_tmp"
  if visudo -cf "$_tmp" >/dev/null; then
    install -m 440 -o root -g root "$_tmp" "$SUDOERS_FILE"
  else
    rm -f "$_tmp"
    echo "error: generated sudoers rule failed validation — nothing written" >&2
    exit 1
  fi
  rm -f "$_tmp"
fi

echo "Done — remote-enrollment is ready. To join a Hub's fleet, run:"
echo "  curl -s \"<enrollment_url>\" -o ${STAGED_CONF} && sudo sandos-wg-enroll up ${STAGED_CONF}"
