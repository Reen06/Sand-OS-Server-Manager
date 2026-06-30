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
exec ./.venv/bin/uvicorn app.main:app --host "${SM_HOST:-0.0.0.0}" --port "${SM_PORT}"
