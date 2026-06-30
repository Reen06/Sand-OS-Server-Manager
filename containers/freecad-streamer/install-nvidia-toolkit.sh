#!/usr/bin/env bash
# Installs the NVIDIA Container Toolkit and sets up CDI mode (no docker restart).
# Run as root:  sudo bash install-nvidia-toolkit.sh
set -euo pipefail

echo "[1/4] GPG keyring"
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

echo "[2/4] apt source"
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

echo "[3/4] install nvidia-container-toolkit"
apt-get update
apt-get install -y nvidia-container-toolkit

echo "[4/4] generate CDI spec (/etc/cdi/nvidia.yaml)"
nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml >/dev/null
nvidia-ctk --version | head -1
echo "CDI devices:"
nvidia-ctk cdi list 2>/dev/null | sed -n '1,6p' || true

echo CDI_DONE
