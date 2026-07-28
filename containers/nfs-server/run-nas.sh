#!/bin/bash
# Fleet NAS — containerized NFSv4 server exporting the storage root that every
# streamed app mounts (per-user homes under users/, shared folders under shared/).
#
# Runs WITHOUT host sudo: a privileged Docker container using the host's nfsd
# kernel module. `all_squash,anonuid/anongid` maps every client to one uid so
# FreeCAD (root), Nextcloud (www-data) and Filebrowser all read/write the SAME
# files with consistent ownership. NFSv4 = single port 2049 → tunnels cleanly
# over WireGuard for an off-LAN NAS later.
#
# sandos-nfs-server (NOT the bare erichough/nfs-server image): a thin local
# layer that also starts nfsdcld before nfsd — the base image never does,
# which silently degrades NFSv4 client-recovery tracking and causes new
# per-user home directories (any app's first launch) to hang on creation
# while reads keep working fine. Rebuild after ever bumping the base image:
#   cd containers/nfs-server && docker build -t sandos-nfs-server:latest .
set -e
NAS_ROOT="${NAS_ROOT:-/home/control/sandos-nas}"
NAS_UID="${NAS_UID:-1000}"       # owner all files map to (this host's storage user)
NAS_GID="${NAS_GID:-1000}"

mkdir -p "$NAS_ROOT/users" "$NAS_ROOT/shared" "$NAS_ROOT/staging"

# ── Who may see what ──────────────────────────────────────────────────────────
# TRUSTED hosts get the whole tree as their NFS root. Everyone else must be
# granted an explicit per-client export below, and gets NOTHING otherwise —
# there is deliberately no `*` fallback.
#
# Space-separated list of addresses/CIDRs. The NAS host itself must be here or
# its own apps lose their mounts.
NAS_TRUSTED="${NAS_TRUSTED:-127.0.0.1 ${NAS_SELF_IP:-10.0.0.164}}"

_export_args=()
_n=0
for _host in $NAS_TRUSTED; do
  _export_args+=(-e "NFS_EXPORT_${_n}=/nfs ${_host}(rw,fsid=0,crossmnt,sync,no_subtree_check,insecure,all_squash,anonuid=${NAS_UID},anongid=${NAS_GID})")
  _n=$((_n + 1))
done

# APP-ONLY clients: each gets its OWN fsid=0 rooted at its staging directory.
# This is the only arrangement that actually scopes an NFSv4 client — a shared
# pseudo-root with per-subtree grants beneath it does NOT restrict anything,
# because a client holding the pseudo-root can navigate the whole filesystem
# (verified three separate ways). Format: "<addr>:<staging-subdir>".
for _entry in ${NAS_APP_CLIENTS:-}; do
  _addr="${_entry%%:*}"; _dir="${_entry##*:}"
  mkdir -p "$NAS_ROOT/staging/$_dir"
  _export_args+=(-e "NFS_EXPORT_${_n}=/nfs/staging/${_dir} ${_addr}(rw,fsid=0,sync,no_subtree_check,insecure,all_squash,anonuid=${NAS_UID},anongid=${NAS_GID})")
  _n=$((_n + 1))
done

docker rm -f sandos-nfs >/dev/null 2>&1 || true
# --network host is LOAD-BEARING, not a preference. With a published port
# (-p 2049:2049) Docker's userland proxy terminates every connection and opens
# a fresh one from the bridge gateway, so nfsd sees 172.17.0.1 as the source for
# EVERY client and cannot tell them apart at all — which silently defeats every
# per-client rule above. Reintroducing -p will quietly disable all scoping.
#
# :rshared + crossmnt: USB drives bind-mounted into the NAS tree AFTER the
# container starts still propagate into /nfs and get exported to clients.
docker run -d --name sandos-nfs --privileged --restart unless-stopped \
  --network host \
  --mount type=bind,source="$NAS_ROOT",target=/nfs,bind-propagation=rshared \
  -v /lib/modules:/lib/modules:ro \
  "${_export_args[@]}" \
  sandos-nfs-server:latest
echo "sandos-nfs started — NFSv4 on :2049, all_squash -> ${NAS_UID}:${NAS_GID}"
echo "  trusted (whole tree): ${NAS_TRUSTED}"
echo "  app-only (staging):   ${NAS_APP_CLIENTS:-<none>}"
