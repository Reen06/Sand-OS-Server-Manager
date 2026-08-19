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

# Find the compositor and WAIT for it.
#
# A user service does NOT inherit the graphical session's environment, so
# WAYLAND_DISPLAY is simply absent here even though the compositor is running
# perfectly. Discover it from the socket instead of trusting the variable —
# otherwise chromium starts with no display, exits, and systemd restarts it into
# the same emptiness forever. That is the single most common way a kiosk ends up
# as a permanently black screen.
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR

for _ in $(seq 1 60); do
  if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    for s in "$XDG_RUNTIME_DIR"/wayland-*; do
      case "$s" in *.lock) continue ;; esac
      [ -S "$s" ] && { WAYLAND_DISPLAY=$(basename "$s"); export WAYLAND_DISPLAY; break; }
    done
  fi
  [ -n "${WAYLAND_DISPLAY:-}" ] && [ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ] && break
  [ -n "${DISPLAY:-}" ] && command -v xset >/dev/null && xset q >/dev/null 2>&1 && break
  sleep 1
done
[ -n "${WAYLAND_DISPLAY:-}" ] && echo "kiosk: using WAYLAND_DISPLAY=$WAYLAND_DISPLAY"

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
  `# WebGL by software rasterisation, deliberately.` \
  `# The Pi 3's Broadcom VC4 is OpenGL ES 2.0, which can only ever back WebGL 1.` \
  `# three.js dropped WebGL 1 in r163, so the toolpath viewer (r171) asks for a` \
  `# WebGL 2 context, the GPU cannot give one, and three THROWS from the` \
  `# WebGLRenderer constructor — which, being inside a React effect, took down` \
  `# the entire app and left a blank screen until Viewer3D learned to catch it.` \
  `#` \
  `# --enable-unsafe-swiftshader is NOT sufficient on its own, which is the` \
  `# non-obvious part: it only PERMITS the software fallback, and the fallback` \
  `# is reached only when GPU init fails outright. Here it succeeds — ANGLE` \
  `# comes up on the real VC4 as --use-angle=gles — so WebGL 2 kept being` \
  `# refused with the flag present and doing nothing. ANGLE has to be pointed` \
  `# at SwiftShader explicitly. Measured on this panel: no flags -> webgl and` \
  `# webgl2 both unavailable; --ignore-gpu-blocklist --enable-gpu -> unchanged` \
  `# (a hardware limit, not a blocklist); --enable-unsafe-swiftshader alone ->` \
  `# still no webgl2 in the real browser; the pair below -> a genuine` \
  `# "WebGL 2.0 (OpenGL ES 3.0 Chromium)" context and a working 3D viewer.` \
  `#` \
  `# This puts ALL compositing on the CPU, not just WebGL. That is affordable` \
  `# only because the viewer renders on demand instead of running a` \
  `# requestAnimationFrame loop: measured idle after load, every chromium` \
  `# process sits at ~0% CPU with a load average of 0.2. If the viewer ever` \
  `# goes back to a continuous render loop, this flag will hold a core at 100%` \
  `# forever on a fanless board.` \
  --use-gl=angle --use-angle=swiftshader \
  `# 905MB of RAM on a Pi 3. These keep chromium from ballooning into swap,` \
  `# which on an SD card is the difference between slow and unusable.` \
  --disable-dev-shm-usage \
  --renderer-process-limit=2 \
  --disable-background-networking \
  "$URL"
