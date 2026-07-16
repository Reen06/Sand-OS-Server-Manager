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

mkdir -p "$NAS_ROOT/users" "$NAS_ROOT/shared"
docker rm -f sandos-nfs >/dev/null 2>&1 || true
# :rshared + crossmnt: USB drives bind-mounted into the NAS tree AFTER the
# container starts still propagate into /nfs and get exported to clients.
docker run -d --name sandos-nfs --privileged --restart unless-stopped \
  --mount type=bind,source="$NAS_ROOT",target=/nfs,bind-propagation=rshared \
  -v /lib/modules:/lib/modules:ro \
  -e NFS_EXPORT_0="/nfs *(rw,fsid=0,crossmnt,sync,no_subtree_check,insecure,all_squash,anonuid=${NAS_UID},anongid=${NAS_GID})" \
  -p 2049:2049 \
  sandos-nfs-server:latest
echo "sandos-nfs started — exporting $NAS_ROOT over NFSv4 (:2049), all_squash -> ${NAS_UID}:${NAS_GID}"
