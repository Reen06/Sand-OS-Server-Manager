"""Whether this node can use the mesh NAS directly, or needs a gateway.

Most nodes mount the cluster and read chunks straight from whichever volume
servers hold them. One kind cannot: a machine running Docker Desktop keeps its
containers in a *separate* Linux VM from the shell you get when you log in, so a
FUSE mount made where the Server Manager runs is invisible to the containers it
starts. Verified rather than assumed — a container there lists the Docker
Desktop VM's own filesystem, not the one the mount lives on.

Such a node reads through an NFS gateway instead: another node re-exports its
mesh mount, and this one mounts that. It stores nothing either way, so the only
cost is its own read path going via one machine.

This module answers the question. Assigning the gateway is the Hub's job — it is
the only thing that can see which other nodes are capable of the role.
"""
from __future__ import annotations

import os
import subprocess

from . import config

MESH_MOUNT = os.environ.get("SM_MESH_NAS_MOUNT", "/mnt/sandos-nas")


def _docker_desktop() -> bool:
    """True when containers run in a VM separate from this filesystem."""
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.OperatingSystem}}"],
                           capture_output=True, text=True, timeout=15)
        return "docker desktop" in (r.stdout or "").strip().lower()
    except Exception:  # noqa: BLE001
        return False


def _is_wsl() -> bool:
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def mesh_mounted() -> bool:
    try:
        return os.path.ismount(MESH_MOUNT) and bool(os.listdir(MESH_MOUNT))
    except OSError:
        return False


def can_mount_natively() -> bool:
    """Can a mesh mount here actually be reached by this node's containers?

    The mount existing is not the question. On Docker Desktop it can exist,
    look correct to a shell, and still be invisible to every container the node
    starts — which is the worst possible failure, because everything appears
    fine until an app reports an empty NAS.
    """
    return not _docker_desktop()


def status() -> dict:
    """What this node needs in order to use the mesh NAS."""
    native = can_mount_natively()
    return {
        "mesh_mount": MESH_MOUNT,
        "mounted": mesh_mounted(),
        "can_mount_natively": native,
        # The Hub reads this to decide whether to assign a gateway, and to pick
        # which node provides it.
        "needs_gateway": not native,
        "reason": ("" if native else
                   "Docker Desktop runs containers in a separate VM, so a mount "
                   "made here is not visible to them"),
        "is_wsl": _is_wsl(),
        # Where it currently reads from, so a mismatch between assignment and
        # reality is visible rather than silent.
        "nas_host": config.NAS_HOST,
        "nas_root": config.NAS_ROOT,
    }
