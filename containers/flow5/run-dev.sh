#!/usr/bin/env bash
# Dev run: GPU GL (CDI) + software (x264) video encode — same shape as
# freecad-streamer/run-dev.sh. Publishes on PORT (default 8098, distinct from
# freecad's 8099). Open http://<host>:8098 (basic-auth user "user", password below).
set -euo pipefail

NAME="${NAME:-flow5-streamer}"
PASSWD="${PASSWD:-flow5}"
IMAGE="${IMAGE:-sm-flow5:dev}"
PORT="${PORT:-8098}"

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
  "$IMAGE"

echo "flow5 streamer up -> http://localhost:${PORT}   (user: user  pass: $PASSWD)"
echo "logs: docker logs -f $NAME"
