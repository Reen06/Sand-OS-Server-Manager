#!/bin/bash
# Regenerate the NAS exports from the Hub's per-node trust settings.
#
# The Hub is where trust lives (Fleet page → each node's "NAS" selector); this
# turns that into the export list nfsd actually enforces. Run it after changing
# a node's trust level, or on a timer.
#
#   ./sync-nas-policy.sh                      # reads the node's configured Hub
#   HUB_URL=https://<hub> ./sync-nas-policy.sh
#
# Talks to the Hub over the MESH address, not the public hostname: the Hub
# serves its API to the mesh only and answers a deliberate 404 to anything
# arriving from the public internet.
set -euo pipefail

# The Hub this node was installed against, from its own environment file. Not a
# literal: a baked-in address belongs to the mesh this was written on, so every
# other deployment would silently point at a stranger's machine.
HUB_URL="${HUB_URL:-}"
if [ -z "$HUB_URL" ] && [ -r /etc/sandos-server-manager.env ]; then
  # \042 \047 = double and single quote, stripped in case the env file quotes
  # the value. Octal because the alternative is nested-quote soup.
  HUB_URL=$(sed -n 's/^[[:space:]]*SM_HUB_URL=//p' /etc/sandos-server-manager.env | tail -1 | tr -d '\042\047')
fi
[ -n "$HUB_URL" ] || { echo "sync-nas-policy: no Hub configured (set HUB_URL or SM_HUB_URL)" >&2; exit 2; }
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

NAS_UID="${NAS_UID:-1000}"
NAS_GID="${NAS_GID:-1000}"
NAS_ROOT="${NAS_ROOT:-/home/control/sandos-nas}"

# Apply LIVE when the server is already up: rewrite /etc/exports and reload with
# `exportfs -ra`. Recreating the container instead would drop every client's
# mount, and clients that had one hang on their next access until the volume is
# recreated — far too destructive for something a dropdown can trigger. Only
# fall back to a full start when there is no server running to reload.
if docker inspect -f '{{.State.Running}}' sandos-nfs 2>/dev/null | grep -q true; then
  _exports=""
  for _host in $NAS_TRUSTED; do
    _exports="${_exports}/nfs  ${_host}(rw,fsid=0,crossmnt,sync,no_subtree_check,insecure,all_squash,anonuid=${NAS_UID},anongid=${NAS_GID})
"
  done
  for _entry in ${NAS_APP_CLIENTS:-}; do
    _addr="${_entry%%:*}"; _dir="${_entry##*:}"
    mkdir -p "${NAS_ROOT}/staging/${_dir}"
    _exports="${_exports}/nfs/staging/${_dir}  ${_addr}(rw,fsid=0,sync,no_subtree_check,insecure,all_squash,anonuid=${NAS_UID},anongid=${NAS_GID})
"
  done
  printf '%s' "$_exports" | docker exec -i sandos-nfs sh -c 'cat > /etc/exports && exportfs -ra'
  echo "  applied live (exportfs -ra) — existing mounts undisturbed"
else
  echo "  no running server — starting one"
  NAS_TRUSTED="$NAS_TRUSTED" NAS_APP_CLIENTS="$NAS_APP_CLIENTS" bash "$HERE/run-nas.sh"
fi
