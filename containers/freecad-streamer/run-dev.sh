#!/usr/bin/env bash
# Dev run: GPU GL (CDI) + software (x264) video encode + lower bitrate.
# NOTE: this Selkies egl-desktop base needs the GPU to boot its GL desktop even
# with software *encode* — so the NVIDIA container toolkit (CDI) is required.
# See run-gpu.sh header for the one-time toolkit setup. Difference vs run-gpu.sh
# is just the encoder: x264enc (CPU) here vs nvh264enc (NVENC) there.
#
# Publishes the web UI on PORT (default 8099, since host :8080 is often taken).
# Open http://<host>:8099  (basic-auth user "user", password below).
# For full WebRTC over the LAN from another device you may need host networking
# (--network=host, requires :8080 free) or to publish the TURN ports.
set -euo pipefail

NAME="${NAME:-freecad-streamer}"
PASSWD="${PASSWD:-freecad}"
IMAGE="${IMAGE:-freecad-streamer:dev}"
PORT="${PORT:-8099}"

docker rm -f "$NAME" >/dev/null 2>&1 || true

docker run --name "$NAME" -d --rm \
  --device nvidia.com/gpu=all \
  -p "${PORT}:8080" \
  --tmpfs /dev/shm:rw \
  -e TZ=UTC \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e DISPLAY_SIZEW=1920 -e DISPLAY_SIZEH=1080 -e DISPLAY_REFRESH=60 \
  -e SELKIES_ENCODER=x264enc \
  -e SELKIES_VIDEO_BITRATE=12000 -e SELKIES_FRAMERATE=60 \
  -e SELKIES_BASIC_AUTH_USER=user \
  -e PASSWD="$PASSWD" \
  -e SELKIES_BASIC_AUTH_PASSWORD="$PASSWD" \
  -v freecad-projects:/mnt/freecad-projects \
  "$IMAGE"

echo "FreeCAD streamer up → http://localhost:${PORT}   (user: user  pass: $PASSWD)"
echo "logs: docker logs -f $NAME"
