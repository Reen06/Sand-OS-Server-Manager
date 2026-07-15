#!/usr/bin/env bash
# Start the Sand-OS Server Manager (apps screen + orchestration API).
#   SM_LAN_IP=10.0.0.164 ./run.sh
# Then open http://<host>:8170
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

export SM_LAN_IP="${SM_LAN_IP:-10.0.0.164}"
export SM_PORT="${SM_PORT:-8170}"
# Hub SSO: identify users by their SandOS Hub session instead of an anonymous
# per-browser cookie, so real usernames reach app SSO headers (e.g. Open WebUI
# signs you in as your Hub user, not a random ID).
export SM_HUB_URL="${SM_HUB_URL:-https://vpn1603.duckdns.org}"
# Faster LAN path for the identity check; the cert name won't match the raw IP,
# which is fine — SM_HUB_VERIFY_TLS defaults to false.
export SM_HUB_INTERNAL_URL="${SM_HUB_INTERNAL_URL:-https://10.0.0.177}"
exec ./.venv/bin/uvicorn app.main:app --host "${SM_HOST:-0.0.0.0}" --port "${SM_PORT}"
