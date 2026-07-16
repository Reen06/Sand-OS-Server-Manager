# Thin layer on erichough/nfs-server: starts nfsdcld right before nfsd.
#
# The base image never starts nfsdcld (the NFSv4 client-recovery-tracking
# daemon) at all — confirmed by reading its entrypoint.sh. Without it running
# before nfsd initializes, the kernel logs:
#   NFSD: Unable to initialize client recovery tracking! (-110)
#   NFSD: Is nfsdcld running? If not, enable CONFIG_NFSD_LEGACY_CLIENT_TRACKING.
# and falls back to a legacy tracking mode. Observed live (2026-07-16): NFS
# clients on this degraded server could still READ instantly, but WRITE/CREATE
# operations (e.g. `mkdir` for a new per-user home directory) hung
# indefinitely — the exact case that blocked FreeCAD, OpenMapper, and every
# other app whose first launch creates its NAS home directory.
#
# A wrapper script run BEFORE the base entrypoint does NOT work: nfsdcld
# needs rpc_pipefs mounted first (done by the base script's own
# boot_main_mounts step), so it silently fails to properly initialize if
# started too early even though the process itself stays resident. The only
# reliable fix is patching the base entrypoint.sh in place, inserting the
# nfsdcld call between boot_main_idmapd and boot_main_nfsd — exactly where
# the base script itself starts nfsd, so all of ITS OWN prior setup
# (rpc_pipefs mount included) has already happened.
FROM erichough/nfs-server:latest

RUN sed -i \
    's|^\(\s*\)boot_main_nfsd$|\1mkdir -p /var/lib/nfs/v4recovery \&\& /sbin/nfsdcld\n\1boot_main_nfsd|' \
    /usr/local/bin/entrypoint.sh \
    && grep -q 'nfsdcld' /usr/local/bin/entrypoint.sh
