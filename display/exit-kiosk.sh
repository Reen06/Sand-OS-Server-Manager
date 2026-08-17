#!/bin/bash
# Leave the panel and return to the desktop.
#
# Stops the kiosk unit rather than killing the browser: a killed chromium is
# restarted within seconds by Restart=always, which reads to the person standing
# there as the panel refusing to close.
#
# The backlight is put back to full on the way out. The power agent keeps running
# and would dim an idle desktop exactly as it dims the panel, which is not what
# someone who just asked for the desktop expects.
set -u
systemctl --user stop sandos-display-kiosk.service 2>/dev/null
curl -s -m 2 -X POST http://127.0.0.1:8371/wake -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1
echo "Panel stopped. Start it again from the Wall Panel desktop icon."
