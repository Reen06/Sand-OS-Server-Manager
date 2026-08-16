#!/bin/bash
# Launch the browser for a Sand-OS display panel.
#
# The whole job is: open ONE url, full screen, forever, and come back from
# anything that goes wrong without a person being present. Everything the panel
# actually shows — what app, when to lock, the PIN — is decided by the Hub and
# lives in the page it serves. This script deliberately knows none of it, so
# repointing a screen never means touching the device.
#
# The URL is the enrolment endpoint, not the app. Hitting it on every start
# means a browser profile that was wiped (an update, a full disk, someone
# clearing it) signs itself back in instead of parking on a login screen nobody
# is standing there to fill in.
set -u

CONF=/etc/sandos-display.env
[ -r "$CONF" ] && . "$CONF"

: "${SANDOS_HUB_URL:?SANDOS_HUB_URL missing from /etc/sandos-display.env}"
: "${SANDOS_DISPLAY_TOKEN:?SANDOS_DISPLAY_TOKEN missing from /etc/sandos-display.env}"

URL="${SANDOS_HUB_URL%/}/display/login?token=${SANDOS_DISPLAY_TOKEN}"
PROFILE="${SANDOS_PROFILE_DIR:-$HOME/.sandos-kiosk}"
mkdir -p "$PROFILE"

# Wait for the compositor. Starting before it is up is the single most common
# way a kiosk ends as a black screen: chromium exits immediately, systemd
# restarts it into the same emptiness, and the panel never recovers on its own.
for _ in $(seq 1 60); do
  [ -n "${WAYLAND_DISPLAY:-}" ] && [ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/${WAYLAND_DISPLAY}" ] && break
  [ -n "${DISPLAY:-}" ] && command -v xset >/dev/null && xset q >/dev/null 2>&1 && break
  sleep 1
done

# A crash that leaves these set makes chromium open a "didn't shut down
# correctly" bubble over the app and wait for a click nobody will give it.
PREFS="$PROFILE/Default/Preferences"
if [ -f "$PREFS" ]; then
  sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/; s/"exited_cleanly":false/"exited_cleanly":true/' \
    "$PREFS" 2>/dev/null || true
fi

BROWSER=$(command -v chromium || command -v chromium-browser || true)
[ -n "$BROWSER" ] || { echo "kiosk: no chromium found" >&2; exit 1; }

exec "$BROWSER" \
  --user-data-dir="$PROFILE" \
  --kiosk --start-fullscreen \
  --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-features=TranslateUI \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --autoplay-policy=no-user-gesture-required \
  --overscroll-history-navigation=0 \
  `# A wall panel has no keyboard and nobody to answer a certificate prompt.` \
  `# The Hub uses its own CA on the mesh; a prompt here is a dead screen.` \
  --ignore-certificate-errors \
  `# 905MB of RAM on a Pi 3. These keep chromium from ballooning into swap,` \
  `# which on an SD card is the difference between slow and unusable.` \
  --disable-dev-shm-usage \
  --renderer-process-limit=2 \
  --disable-background-networking \
  "$URL"
