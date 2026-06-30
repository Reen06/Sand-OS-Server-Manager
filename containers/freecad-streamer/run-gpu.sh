#!/usr/bin/env bash
# GPU run: hardware GL (VirtualGL/EGL) + NVENC (nvh264enc). Requires the NVIDIA
# container toolkit on the host, set up in CDI mode (NO docker daemon restart, so
# other running containers are undisturbed):
#   sudo apt-get install -y nvidia-container-toolkit
#   sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
# Then this script passes the GPU in via CDI (--device nvidia.com/gpu=all).
# Host driver must NOT be the -headless variant (needs graphics/display caps).
# PORT publishes the web UI (default 8099 to avoid a host :8080 clash).
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
  -e SELKIES_ENCODER=nvh264enc \
  -e SELKIES_VIDEO_BITRATE=16000 -e SELKIES_FRAMERATE=60 \
  -e SELKIES_BASIC_AUTH_USER=user \
  -e PASSWD="$PASSWD" \
  -e SELKIES_BASIC_AUTH_PASSWORD="$PASSWD" \
  -v freecad-projects:/mnt/freecad-projects \
  "$IMAGE"

echo "FreeCAD streamer (GPU) up → http://localhost:${PORT}   (user: user  pass: $PASSWD)"
echo "logs: docker logs -f $NAME"
