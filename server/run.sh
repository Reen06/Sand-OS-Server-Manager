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
# Hub LLM Router API key (must match the Hub's 'llm_api_key' setting). Kept in
# an untracked file so it never lands in git; when set, Open WebUI is seeded
# with the Hub router as its OpenAI connection (fleet-wide smart routing).
if [ -z "${SM_LLM_API_KEY:-}" ] && [ -f "$HOME/.config/sandos/llm-api-key" ]; then
  export SM_LLM_API_KEY="$(cat "$HOME/.config/sandos/llm-api-key")"
fi
# Shared NAS staging dir for model copy/move between servers (export here on the
# source, import from here on the target — no re-download from ollama.com). MUST
# be the same shared NAS location on every node: on the NAS host it's the real
# dir; on other nodes it's that dir via the NFS mount. Default suits the NAS host.
export SM_OLLAMA_NAS_TRANSFER="${SM_OLLAMA_NAS_TRANSFER:-/home/control/sandos-nas/shared/ollama-transfer}"
# OPT-IN auto-start. Empty by default: nothing launches on boot — every app is
# on-demand (starts when you open it). Only set this (e.g. "ollama,open-webui")
# if you deliberately want specific apps always-on across reboots.
export SM_AUTOSTART_APPS="${SM_AUTOSTART_APPS:-}"
exec ./.venv/bin/uvicorn app.main:app --host "${SM_HOST:-0.0.0.0}" --port "${SM_PORT}"
