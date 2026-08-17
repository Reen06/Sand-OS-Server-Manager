"""Everything this node contributes to the mesh NAS pool.

A node's contribution is not one number. It can be:

  - its **internal reservation** — a preallocated ext4 image on the system disk
    (scripts/sandos-sm-pool). Image-backed because that disk is shared with the
    OS and other users, so the space has to be genuinely taken to be safe to
    promise. Resizable in both directions.

  - each **assigned USB partition** — already exclusively the pool's, so it is
    used directly at its natural size. Wrapping a partition in an image would
    add a layer, cost the same space, and make resizing harder rather than
    easier.

The distinction the pool actually cares about is not "internal vs USB" but
whether a source can hold POSIX ownership and symlinks. exFAT and vfat cannot.
A drive formatted that way stays readable on a Windows machine — which is why
it is worth keeping that way — but it can carry bulk media only, never user
homes, whose whole model is per-user ownership. Placement must respect that or
it will write files nobody can be shown to own.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import config, usb_storage

_POOL_HELPER = "/usr/local/lib/sandos-sm-pool"
_CLUSTER_HELPER = "/usr/local/lib/sandos-sm-cluster"


def _helper_cmd(*args: str, helper: str = _POOL_HELPER) -> list[str]:
    """The helper invocation for this process.

    Only reaches for sudo when it is actually needed. A Server Manager
    installed by a provisioning script or on a root-login distribution runs AS
    root, and some of those images — Proxmox among them — ship no sudo at all.
    Prefixing it unconditionally turned a working helper into a hard failure
    that surfaced only as "this node contributes no storage".
    """
    if os.geteuid() == 0:
        return [helper, *args]
    return ["sudo", "-n", helper, *args]


def _fs_usage(path: str) -> tuple[int, int, int]:
    """(total, used, free) for the filesystem holding `path`, zeros if gone."""
    try:
        u = shutil.disk_usage(path)
        return u.total, u.used, u.free
    except OSError:
        return 0, 0, 0


def _internal() -> dict | None:
    """This node's image-backed reservation, or None if it contributes none."""
    if not os.path.exists(_POOL_HELPER):
        return None
    try:
        import json
        r = subprocess.run(_helper_cmd("status"),
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001
        return None
    if not d.get("exists"):
        return None
    return {
        "id": f"{config.NODE_NAME}:internal",
        "node": config.NODE_NAME,
        "kind": "internal",
        "role": "nas",
        "label": ("Dedicated volume" if d.get("backing") == "dedicated"
                  else "Internal reservation"),
        "path": d.get("mount") or "",
        "fstype": "ext4",
        "posix": True,
        # Only an image-backed pool can be resized from here. A dedicated volume
        # (an LVM LV, a partition) is grown with LVM or a partition tool — the
        # service resizing storage it does not own would be overreach.
        "resizable": d.get("backing") != "dedicated",
        "backing": d.get("backing") or "image",
        "online": bool(d.get("mounted")),
        "total_bytes": int(d.get("image_bytes") or 0),
        "used_bytes": int(d.get("used_bytes") or 0),
        "free_bytes": int(d.get("avail_bytes") or 0),
        # How much further this source COULD grow, which is a property of the
        # host disk rather than of the pool — the Hub needs it to offer a
        # sensible maximum instead of letting someone fill the machine.
        "growth_headroom_bytes": int(d.get("host_avail_bytes") or 0),
    }


def _dedupe_by_filesystem(usage: list[dict]) -> list[dict]:
    """Keep one `usage` entry per underlying filesystem, dropping the rest.

    A seaweed volume server can be configured with several data directories
    for different purposes (general volumes, a "pinned"/replicated-critical
    set, ...) that legitimately live on the SAME physical disk. `used_bytes`/
    `avail_bytes` per entry come from a whole-filesystem statfs, not from
    what's actually IN that one directory — so two entries on the same disk
    report the IDENTICAL number, byte for byte, no matter how much data sits
    in each. Summing them counts that disk's capacity twice (or three times:
    confirmed live, UbuntuNAS reports the SAME used/avail across all of
    `.cluster`/`.cluster-bulk`/`.cluster-pinned`, tripling its real ~2 TB
    into a phantom ~5.9 TB). Identified by `st_dev` (the actual device a path
    resolves to), not by string-matching directory names, since the naming
    convention isn't guaranteed to stay the same. A directory that can't be
    stat'd (already gone) is dropped rather than kept — an entry for space
    that isn't there is worse than one fewer entry.
    """
    seen_devs: set[int] = set()
    out: list[dict] = []
    for u in usage:
        path = u.get("dir") or ""
        try:
            dev = os.stat(path).st_dev
        except OSError:
            continue
        if dev in seen_devs:
            continue
        seen_devs.add(dev)
        out.append(u)
    return out


def _cluster() -> dict | None:
    """This node's SeaweedFS cluster contribution — a THIRD kind of source,
    alongside the image-backed internal reservation and assigned USB drives.

    The mesh NAS migration (see docs/CLAUDE.md history) moved a node's real
    contribution from the internal-reservation/USB-partition model above into
    the cluster, but nothing ever taught this module about that move — so a
    node's tier-eligible capacity was still computed from whatever pre-cluster
    reservation happened to be lying around, however small or stale, while the
    cluster itself (potentially hundreds of GB) was invisible here.
    Confirmed live on CortexPC: a leftover 5 GB pre-cluster pool.img — 24 KB
    actually used, functionally dead — was the ENTIRE reported tier capacity,
    while the node's real, active cluster contribution (a dedicated drive,
    ~138 GB free) was not counted at all. The Tiers section read "4 GB
    available" on a page whose own Cluster section, two scrolls up, correctly
    showed hundreds of gigabytes free — same node, two contradictory numbers.

    SeaweedFS's filer preserves POSIX ownership (that is what makes per-user
    homes and NFS-style access work at all), so this source is posix=True same
    as the internal reservation — never excluded from tiers 2/3 the way a
    non-POSIX USB filesystem is.
    """
    if not os.path.exists(_CLUSTER_HELPER):
        return None
    try:
        import json
        r = subprocess.run(_helper_cmd("status", helper=_CLUSTER_HELPER),
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout or "{}")
    except Exception:  # noqa: BLE001
        return None
    if not d.get("running"):
        return None
    usage = _dedupe_by_filesystem(d.get("usage") or [])
    used = sum(int(u.get("used_bytes") or 0) for u in usage)
    free = sum(int(u.get("avail_bytes") or 0) for u in usage)
    if not usage:
        return None
    return {
        "id": f"{config.NODE_NAME}:cluster",
        "node": config.NODE_NAME,
        "kind": "cluster",
        "role": "nas",
        "label": "Mesh NAS cluster",
        "path": d.get("dirs") or "",
        "fstype": "seaweedfs",
        "posix": True,
        "resizable": False,     # grown via the Drives modal, not from here
        "backing": "cluster",
        "online": True,
        "total_bytes": used + free,
        "used_bytes": used,
        "free_bytes": free,
        "growth_headroom_bytes": 0,
    }


def _usb() -> list[dict]:
    """Assigned USB partitions that are currently mounted and usable."""
    out: list[dict] = []
    try:
        parts = usb_storage.usb_partitions()
        state = usb_storage._load_state()
    except Exception:  # noqa: BLE001
        return out
    for p in parts:
        entry = (state.get(p["uuid"]) or {}) if isinstance(state, dict) else {}
        # Only drives the operator actually assigned. An unassigned drive is
        # someone's photo stick that happens to be plugged in, and quietly
        # counting it as fleet storage would be both wrong and alarming.
        if not entry.get("assign"):
            continue
        mnt = p.get("mountpoint")
        online = bool(mnt and os.path.ismount(mnt))
        total, used, free = _fs_usage(mnt) if online else (0, 0, 0)
        fstype = (p.get("fstype") or "").lower()
        # A drive given over to app hosting runs its own dockerd and stores
        # images and container volumes. Its space is spoken for, so it is
        # reported for visibility but never counted as pool capacity — doing so
        # would promise the same gigabytes to both Docker and the NAS.
        role = "apps" if entry.get("app_hosting") else "nas"
        out.append({
            "id": f"{config.NODE_NAME}:usb:{p['uuid']}",
            "node": config.NODE_NAME,
            "kind": "usb",
            "role": role,
            "label": p.get("label") or p["name"],
            "path": mnt or "",
            "fstype": fstype,
            # The property placement turns on. A non-POSIX filesystem cannot
            # record who owns a file, so it can hold shared bulk data but never
            # per-user homes.
            "posix": fstype not in usb_storage._NON_POSIX_FSTYPES,
            "resizable": False,     # the partition IS the contribution
            "online": online,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "growth_headroom_bytes": 0,
            "uuid": p["uuid"],
            "assigned_to": entry.get("assign"),
            # Surfaced so the dashboard can say WHY a drive cannot hold homes,
            # rather than silently offering fewer options than another drive.
            "limits": ([] if fstype not in usb_storage._NON_POSIX_FSTYPES else
                       [f"{fstype} keeps no file ownership — bulk data only, no user homes"]),
        })
    return out


def sources() -> list[dict]:
    """Every pool source on this node, cluster first.

    Cluster and internal-reservation are never both reported: they are two
    tellings of the same machine's story, from before and after the mesh NAS
    migration, and a node running the cluster has moved on from the pre-
    cluster model — nothing routes user files to the internal reservation any
    more. Reporting both would not add information, only show one tiny/stale
    number next to one real one and invite the exact confusion this was fixed
    for. Cluster wins when both are present; a node with no cluster
    contribution (app-only, or never migrated) still gets its internal/USB
    sources exactly as before.
    """
    out = []
    cluster = _cluster()
    if cluster:
        out.append(cluster)
    else:
        internal = _internal()
        if internal:
            out.append(internal)
    out.extend(_usb())
    return out


def summary() -> dict:
    """Node-level totals, counting only sources that are actually online.

    Offline sources are reported separately rather than folded into the totals:
    an unplugged drive's capacity is not capacity, and adding it to the pool
    figure would promise space that cannot be written to right now.
    """
    src = sources()
    # Only NAS-role sources are capacity. App-hosting drives appear in `sources`
    # so the dashboard can show where a machine's storage actually went, but
    # they are not part of the pool.
    live = [s for s in src if s["online"] and s.get("role", "nas") == "nas"]
    return {
        "node": config.NODE_NAME,
        "sources": src,
        "total_bytes": sum(s["total_bytes"] for s in live),
        "used_bytes": sum(s["used_bytes"] for s in live),
        "free_bytes": sum(s["free_bytes"] for s in live),
        "posix_free_bytes": sum(s["free_bytes"] for s in live if s["posix"]),
        "offline_sources": [s["label"] for s in src if not s["online"]],
        # Shown alongside the pool so a machine's storage adds up on screen:
        # without it, a 100G drive simply vanishes from the picture.
        "app_hosting_bytes": sum(s["total_bytes"] for s in src
                                 if s["online"] and s.get("role") == "apps"),
    }
