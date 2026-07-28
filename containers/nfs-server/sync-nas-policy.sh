#!/bin/bash
# Regenerate the NAS exports from the Hub's per-node trust settings.
#
# The Hub is where trust lives (Fleet page → each node's "NAS" selector); this
# turns that into the export list nfsd actually enforces. Run it after changing
# a node's trust level, or on a timer.
#
#   ./sync-nas-policy.sh                      # uses HUB_URL default below
#   HUB_URL=https://10.79.114.1 ./sync-nas-policy.sh
#
# Talks to the Hub over the MESH address, not the public hostname: the Hub
# serves its API to the mesh only and answers a deliberate 404 to anything
# arriving from the public internet.
set -euo pipefail

HUB_URL="${HUB_URL:-https://10.79.114.1}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# -k: the Hub is normally fronted by Caddy's internal CA. Same posture as every
# other Hub call in this project.
_policy="$(curl -fsSk --max-time 10 "${HUB_URL%/}/api/fleet/nas-policy")" || {
  echo "sync-nas-policy: could not reach the Hub at ${HUB_URL}" >&2
  echo "  exports left UNCHANGED — a Hub outage must not silently widen or" >&2
  echo "  revoke access to storage." >&2
  exit 1
}

_parsed="$(printf '%s' "$_policy" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(" ".join(t for t in d.get("trusted", []) if t))
pairs = []
for a in d.get("app_only", []):
    addr, stg = a.get("addr"), a.get("staging")
    if addr and stg:
        pairs.append(addr + ":" + stg)
print(" ".join(pairs))
')"
NAS_TRUSTED_REMOTE="$(printf '%s' "$_parsed" | sed -n 1p)"
NAS_APP_CLIENTS="$(printf '%s' "$_parsed" | sed -n 2p)"

# 127.0.0.1 is added unconditionally: the NAS host serves its own apps over the
# same NFS, and losing that would break them the moment the Hub reported
# anything unexpected.
NAS_TRUSTED="127.0.0.1 ${NAS_TRUSTED_REMOTE}"

if [ -z "${NAS_TRUSTED_REMOTE// /}" ] && [ -z "${NAS_APP_CLIENTS// /}" ]; then
  echo "sync-nas-policy: Hub returned an empty policy — refusing to apply." >&2
  echo "  That would leave only loopback able to mount, which is far more likely" >&2
  echo "  to be a Hub-side mistake than a deliberate fleet-wide revocation." >&2
  exit 1
fi

echo "Applying NAS policy from ${HUB_URL}:"
echo "  trusted:  ${NAS_TRUSTED}"
echo "  app-only: ${NAS_APP_CLIENTS:-<none>}"

NAS_TRUSTED="$NAS_TRUSTED" NAS_APP_CLIENTS="$NAS_APP_CLIENTS" bash "$HERE/run-nas.sh"
