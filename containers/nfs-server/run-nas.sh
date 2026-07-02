#!/bin/bash
# Fleet NAS — containerized NFSv4 server exporting the storage root that every
# streamed app mounts (per-user homes under users/, shared folders under shared/).
#
# Runs WITHOUT host sudo: a privileged Docker container using the host's nfsd
# kernel module. `all_squash,anonuid/anongid` maps every client to one uid so
# FreeCAD (root), Nextcloud (www-data) and Filebrowser all read/write the SAME
# files with consistent ownership. NFSv4 = single port 2049 → tunnels cleanly
# over WireGuard for an off-LAN NAS later.
set -e
NAS_ROOT="${NAS_ROOT:-/home/control/sandos-nas}"
NAS_UID="${NAS_UID:-1000}"       # owner all files map to (this host's storage user)
NAS_GID="${NAS_GID:-1000}"

mkdir -p "$NAS_ROOT/users" "$NAS_ROOT/shared"
docker rm -f sandos-nfs >/dev/null 2>&1 || true
docker run -d --name sandos-nfs --privileged --restart unless-stopped \
  -v "$NAS_ROOT":/nfs \
  -v /lib/modules:/lib/modules:ro \
  -e NFS_EXPORT_0="/nfs *(rw,fsid=0,sync,no_subtree_check,insecure,all_squash,anonuid=${NAS_UID},anongid=${NAS_GID})" \
  -p 2049:2049 \
  erichough/nfs-server:latest
echo "sandos-nfs started — exporting $NAS_ROOT over NFSv4 (:2049), all_squash -> ${NAS_UID}:${NAS_GID}"
