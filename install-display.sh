#!/usr/bin/env bash
# Sand-OS display panel installer.
#
# Turns a small machine with a screen into a wall panel: permanently signed in,
# showing one app, dimming when nobody is there and waking the moment somebody
# touches it.
#
# This is NOT install.sh with the apps removed. A display node hosts nothing —
# no Docker, no containers, no storage contribution, no app images. It consumes
# the mesh and nothing else, which is why a Pi 3 with 905MB of RAM is enough
# hardware for it and would be nowhere near enough to be a real node.
#
# Nothing about any particular mesh is baked in here. Everything specific — the
# Hub's address and the screen's own token — is passed in at install time, and
# everything else (which app, the PIN, the timings) is decided by the Hub and
# can be changed later without touching the device.
set -euo pipefail

SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
LIB=/usr/local/lib/sandos-display
CONF=/etc/sandos-display.env
UNITS=/etc/systemd/system
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/display"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
info() { printf '  \033[36m·\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

echo
echo "Sand-OS display panel"
echo "─────────────────────"

# ── inputs ───────────────────────────────────────────────────────────────────
HUB_URL="${SANDOS_HUB_URL:-}"
TOKEN="${SANDOS_DISPLAY_TOKEN:-}"
DISPLAY_USER="${SANDOS_DISPLAY_USER:-}"

if [ -z "$HUB_URL" ]; then
  read -rp "Hub URL (e.g. https://hub.example.org): " HUB_URL
fi
if [ -z "$TOKEN" ]; then
  # Created in the Hub: Displays → Add a screen. Shown once, because it IS the
  # screen's credential.
  read -rp "Screen enrolment token (from the Hub): " TOKEN
fi
[ -n "$HUB_URL" ] || die "a Hub URL is required"
[ -n "$TOKEN" ]   || die "an enrolment token is required"

# The account that owns the graphical session. Guessed from whoever the display
# manager autologins, because that is the session the browser must join — a
# kiosk started as the wrong user has no compositor to draw on and fails in a
# way that looks like the browser being broken.
if [ -z "$DISPLAY_USER" ]; then
  DISPLAY_USER=$($SUDO grep -rhoP '^autologin-user=\K.*' /etc/lightdm/lightdm.conf \
                 /etc/lightdm/lightdm.conf.d/*.conf 2>/dev/null | head -1 || true)
  [ -z "$DISPLAY_USER" ] && DISPLAY_USER="${SUDO_USER:-$(id -un)}"
fi
id "$DISPLAY_USER" >/dev/null 2>&1 || die "user '$DISPLAY_USER' does not exist"
ok "Graphical session user: $DISPLAY_USER"

# ── dependencies ─────────────────────────────────────────────────────────────
if ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  info "Installing chromium…"
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq chromium || \
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq chromium-browser
fi
ok "Browser present"

# ── files ────────────────────────────────────────────────────────────────────
[ -d "$SRC" ] || die "display/ not found next to this script"
$SUDO install -d -m 755 "$LIB"
$SUDO install -m 755 "$SRC/kiosk.sh" "$LIB/kiosk.sh"
$SUDO install -m 755 "$SRC/dim.py"   "$LIB/dim.py"
$SUDO install -m 755 "$SRC/exit-kiosk.sh" "$LIB/exit-kiosk.sh"
ok "Installed to $LIB"

# Desktop launchers, so the panel is a thing you start and stop rather than a
# state the machine is stuck in. Without these the only way back to the desktop
# is killing chromium over SSH — and Restart=always brings it straight back,
# which reads as the panel refusing to close.
for _d in "/home/$DISPLAY_USER/Desktop" "/home/$DISPLAY_USER/.local/share/applications"; do
  $SUDO install -d -o "$DISPLAY_USER" -g "$DISPLAY_USER" -m 755 "$_d"
  $SUDO install -o "$DISPLAY_USER" -g "$DISPLAY_USER" -m 755 \
    "$SRC/sandos-display.desktop" "$_d/sandos-display.desktop"
  $SUDO tee "$_d/sandos-display-exit.desktop" >/dev/null <<EOF
[Desktop Entry]
Type=Application
Name=Exit Wall Panel
Comment=Close the panel and return to the desktop
Icon=application-exit
Exec=$LIB/exit-kiosk.sh
Terminal=false
Categories=Utility;
EOF
  $SUDO chown "$DISPLAY_USER:$DISPLAY_USER" "$_d/sandos-display-exit.desktop"
  $SUDO chmod 755 "$_d/sandos-display-exit.desktop"
done
ok "Desktop launchers (start + exit)"

# 0600 root: the token is the screen's entire credential. Anyone who can read
# it can be this screen — which is a limited identity by design, but still one.
umask 077
$SUDO tee "$CONF" >/dev/null <<EOF
# Sand-OS display panel — written by install-display.sh
# The Hub decides what this screen shows; only identity lives here.
SANDOS_HUB_URL=${HUB_URL%/}
SANDOS_DISPLAY_TOKEN=$TOKEN
# Dimming is local so the panel behaves the same when the Hub is unreachable.
SANDOS_DIM_AFTER=${SANDOS_DIM_AFTER:-90}
SANDOS_OFF_AFTER=${SANDOS_OFF_AFTER:-300}
SANDOS_LOCK_OFF_AFTER=${SANDOS_LOCK_OFF_AFTER:-300}
SANDOS_DIM_LEVEL=${SANDOS_DIM_LEVEL:-12}
EOF
# root:<display user> 0640, not 0600 root. The browser runs AS that user and
# reads this file for the Hub URL and token, so 0600 root means the kiosk exits
# instantly with an unset-variable error that looks nothing like a permissions
# problem. Still unreadable to every other account on the box.
$SUDO chown "root:$DISPLAY_USER" "$CONF"
$SUDO chmod 640 "$CONF"
ok "Wrote $CONF (0640 root:$DISPLAY_USER)"

# The dimmer writes the backlight and reads input devices. Group membership is
# the least-privilege way in; the alternative is running it as root, which is a
# lot of authority for something that sets one integer.
$SUDO usermod -aG video,input "$DISPLAY_USER" 2>/dev/null || true

# udev rule so the backlight is group-writable. Without it the sysfs file is
# root-only and the dimmer fails with EACCES on the very first write.
$SUDO tee /etc/udev/rules.d/90-sandos-backlight.rules >/dev/null <<'EOF'
SUBSYSTEM=="backlight", ACTION=="add", \
  RUN+="/bin/chgrp video /sys/class/backlight/%k/brightness", \
  RUN+="/bin/chmod g+w /sys/class/backlight/%k/brightness"
EOF
$SUDO udevadm control --reload 2>/dev/null || true
$SUDO udevadm trigger -s backlight 2>/dev/null || true
# Timings changed from the lock screen are written here, so they survive a
# reboot instead of silently reverting to the installed defaults.
$SUDO install -d -o "$DISPLAY_USER" -g "$DISPLAY_USER" -m 755 /var/lib/sandos-display
ok "Backlight permissions"

# ── services ─────────────────────────────────────────────────────────────────
$SUDO install -m 644 "$SRC/systemd/sandos-display-dim.service" \
  "$UNITS/sandos-display-dim.service"
$SUDO sed -i "s/__DISPLAY_USER__/$DISPLAY_USER/" "$UNITS/sandos-display-dim.service"

# The browser runs as a USER unit so it lands inside the autologin session.
USER_UNITS="/home/$DISPLAY_USER/.config/systemd/user"
$SUDO install -d -o "$DISPLAY_USER" -g "$DISPLAY_USER" -m 755 "$USER_UNITS"
$SUDO install -o "$DISPLAY_USER" -g "$DISPLAY_USER" -m 644 \
  "$SRC/systemd/sandos-display-kiosk.service" "$USER_UNITS/sandos-display-kiosk.service"

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now sandos-display-dim.service
# Lingering so the panel comes up after a power cut without anyone logging in.
$SUDO loginctl enable-linger "$DISPLAY_USER" 2>/dev/null || true
$SUDO -u "$DISPLAY_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$DISPLAY_USER")" \
  systemctl --user daemon-reload 2>/dev/null || true
$SUDO -u "$DISPLAY_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$DISPLAY_USER")" \
  systemctl --user enable --now sandos-display-kiosk.service 2>/dev/null || \
  info "Kiosk service enabled; it starts with the graphical session"
ok "Services installed"

echo
echo "  Done. The panel signs itself in and opens whatever the Hub has"
echo "  assigned to this screen. To change what it shows, edit the screen"
echo "  in the Hub — this device does not need to be touched again."
echo
echo "  Logs:  journalctl --user -u sandos-display-kiosk -f"
echo "         journalctl -u sandos-display-dim -f"
echo
