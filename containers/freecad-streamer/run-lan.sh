#!/usr/bin/env bash
# LAN run: WebRTC reachable from OTHER devices on the same LAN, using the image's
# internal TURN server (bridge networking — for when host networking isn't an
# option because the this host :8080 is taken). Point the internal TURN at this
# this host LAN IP and publish the web + TURN + a small relay range.
#
#   LAN_IP=10.0.0.164 ./run-lan.sh
#
# Then open  http://<LAN_IP>:8099  from any device on the LAN (user "user").
# (On a host where :8080 is free, prefer --network=host: no TURN needed at all.)
set -euo pipefail

NAME="${NAME:-freecad-streamer}"
PASSWD="${PASSWD:-freecad}"
IMAGE="${IMAGE:-freecad-streamer:dev}"
PORT="${PORT:-8099}"
LAN_IP="${LAN_IP:?set LAN_IP to this this host LAN address, e.g. LAN_IP=10.0.0.164}"
RELAY_MIN="${RELAY_MIN:-49152}"
RELAY_MAX="${RELAY_MAX:-49200}"
# HTTPS=true serves the web UI over TLS (self-signed). Required for the PWA
# "Install app" button (Chrome only offers it in a secure context) — but a
# self-signed cert must be TRUSTED on the device, or Chrome still won't offer it.
# The clean path is fronting this with the Hub's real-cert TLS proxy.
HTTPS="${HTTPS:-false}"
HTTPS_ARG=""; SCHEME="http"
if [ "$HTTPS" = "true" ]; then HTTPS_ARG="-e SELKIES_ENABLE_HTTPS=true"; SCHEME="https"; fi
# RESIZE=false pins a fixed 1920x1080 display (no auto-resize). Use to test the
# cursor — auto-resize to an odd size can break Selkies' client-side cursor.
RESIZE="${RESIZE:-true}"

docker rm -f "$NAME" >/dev/null 2>&1 || true

docker run --name "$NAME" -d --rm \
  --device nvidia.com/gpu=all \
  -p "${PORT}:8080" \
  -p 3478:3478/tcp -p 3478:3478/udp \
  -p "${RELAY_MIN}-${RELAY_MAX}:${RELAY_MIN}-${RELAY_MAX}/udp" \
  --tmpfs /dev/shm:rw \
  ${HTTPS_ARG} \
  -e SELKIES_ENABLE_RESIZE="${RESIZE}" \
  -e TZ=UTC -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e DISPLAY_SIZEW=1920 -e DISPLAY_SIZEH=1080 -e DISPLAY_REFRESH=60 \
  -e SELKIES_ENCODER=nvh264enc -e SELKIES_VIDEO_BITRATE=16000 -e SELKIES_FRAMERATE=60 \
  -e SELKIES_TURN_HOST="${LAN_IP}" -e TURN_EXTERNAL_IP="${LAN_IP}" \
  -e SELKIES_TURN_PORT=3478 -e SELKIES_TURN_PROTOCOL=tcp \
  -e TURN_MIN_PORT="${RELAY_MIN}" -e TURN_MAX_PORT="${RELAY_MAX}" \
  -e SELKIES_BASIC_AUTH_USER=user \
  -e PASSWD="${PASSWD}" -e SELKIES_BASIC_AUTH_PASSWORD="${PASSWD}" \
  -v freecad-projects:/mnt/freecad-projects \
  "$IMAGE"

echo "Open from any LAN device → ${SCHEME}://${LAN_IP}:${PORT}   (user: user  pass: ${PASSWD})"
[ "$HTTPS" = "true" ] && echo "(HTTPS on, self-signed — accept/trust the cert; needed for the PWA install button)"
echo "logs: docker logs -f ${NAME}"
