"""Storage tiers: what a file is worth, and therefore where it may live.

A tier is a POLICY, not a directory. It answers two questions:

  1. how hard the fleet works to keep this file (copies, snapshots, backups)
  2. which storage it may sit on

Tier is decided per FOLDER, with a per-file override:

    users/<u>/Bulk/      → tier 1
    users/<u>/Critical/  → tier 3
    everything else      → tier 2

Folders lead because a tier has to survive the way applications save. Most
rewrite a file rather than modify it — new inode, new name, the old one
unlinked — so a tag attached to a file is routinely lost by the act of editing
it, while a tag attached to the folder it sits in is not. Folders are also
visible: the tier is legible in Filebrowser, FreeCAD or a shell, not only in
this dashboard.

Tier 2 is the default because it is the safe answer for a home directory. A
default of tier 1 would silently make "may not be recoverable" the norm for
files nobody classified, which is exactly the wrong way round.

Per-file overrides exist for the file that does not match its neighbours — one
irreplaceable thing in a folder of scratch. They can only raise a tier, never
lower it. Lowering by override would let a file quietly leave the protection its
folder promises, and the way to demote something should be to move it, which is
visible.
"""
from __future__ import annotations

import json
import os
import threading
import time

from . import config
from .files import _safe_user

TIER_BULK, TIER_PROTECTED, TIER_CRITICAL = 1, 2, 3
DEFAULT_TIER = TIER_PROTECTED

# Folder name → tier. Only tiers that differ from the default need a folder:
# giving tier 2 a folder of its own would imply files outside it are unprotected.
TIER_DIRS = {"Bulk": TIER_BULK, "Critical": TIER_CRITICAL}

# `copies` here is DESCRIPTIVE, not enforcing — nothing in this codebase runs
# `weed shell fs.configure` from these numbers; the filer's per-path
# replication rules were set by hand and are the actual source of truth. If
# they are ever changed there (`weed shell` → `fs.configure` with no args
# lists the live rules), update these to match, or the Hub's replicated-
# capacity math (SandOS Hub sm_fleet.py's _tier_replicated_capacity) will use
# the wrong replication factor. Corrected 2026-08 to match what was actually
# configured: Protected was documented as 1 copy but the filer has been
# running 2 (replication "010") the whole time; Critical was documented as 2
# but the filer runs 3 (replication "020").
TIER_META = {
    TIER_BULK: {
        "id": TIER_BULK, "name": "Bulk", "dir": "Bulk",
        "blurb": "Replaceable. One copy, on one server. May not be recoverable.",
        # The only tier that may sit on a filesystem which cannot record file
        # ownership, because nothing here is private enough for that to matter.
        "requires_posix": False, "copies": 1, "snapshots": False,
    },
    TIER_PROTECTED: {
        "id": TIER_PROTECTED, "name": "Protected", "dir": None,
        "blurb": "Replicated to 2 machines, plus snapshots.",
        "requires_posix": True, "copies": 2, "snapshots": True,
    },
    TIER_CRITICAL: {
        "id": TIER_CRITICAL, "name": "Critical", "dir": "Critical",
        "blurb": "Encrypted, versioned, replicated to 3 independent machines.",
        "requires_posix": True, "copies": 3, "snapshots": True,
    },
}

_OVERRIDES = os.path.join(config.NAS_ROOT, ".tier-overrides.json")
_lock = threading.Lock()

# usage() walks the whole user tree. The NAS page polls every 15s, and the
# answer cannot change faster than someone can move files, so a short cache
# turns a repeated full walk into one. Deliberately short: a stale tier total
# after moving a folder would look like the move silently failed.
_USAGE_TTL = 30.0
_usage_cache: dict = {"at": 0.0, "value": None}


def _load() -> dict:
    try:
        with open(_OVERRIDES) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    tmp = _OVERRIDES + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, _OVERRIDES)      # atomic: a torn file would lose every tag


def _key(user: str, rel: str) -> str:
    return f"{_safe_user(user)}/{(rel or '').strip('/')}"


def tier_of_path(rel: str) -> int:
    """Tier implied by where a path sits, ignoring overrides."""
    first = (rel or "").strip("/").split("/", 1)[0]
    return TIER_DIRS.get(first, DEFAULT_TIER)


def effective_tier(user: str, rel: str) -> dict:
    """The tier that actually applies, and why it applies.

    The reason travels with the answer because "why is this file tier 3" is the
    question people actually ask, and a bare number cannot answer it.
    """
    folder = tier_of_path(rel)
    override = _load().get(_key(user, rel))
    if override and int(override) > folder:
        return {"tier": int(override), "source": "file override",
                "folder_tier": folder}
    return {"tier": folder,
            "source": "folder" if tier_of_path(rel) != DEFAULT_TIER else "default",
            "folder_tier": folder}


def set_override(user: str, rel: str, tier: int | None) -> dict:
    """Pin one file to a higher tier, or clear the pin.

    Refuses to pin BELOW the folder's tier. A file that opted out of its
    folder's protection while still sitting in it would be a trap: the folder
    says one thing, the file does another, and nothing on disk shows it.
    """
    rel = (rel or "").strip("/")
    if not rel:
        raise ValueError("no path given")
    folder = tier_of_path(rel)
    with _lock:
        d = _load()
        k = _key(user, rel)
        if tier is None:
            d.pop(k, None)
        else:
            t = int(tier)
            if t not in TIER_META:
                raise ValueError(f"unknown tier: {tier}")
            if t < folder:
                raise ValueError(
                    f"cannot pin below the folder's tier {folder} — "
                    f"move the file to a lower-tier folder instead")
            if t == folder:
                d.pop(k, None)       # same as the folder: nothing to record
            else:
                d[k] = t
        _save(d)
    return effective_tier(user, rel)


def ensure_tier_dirs(user: str) -> list[str]:
    """Create this user's tier folders if missing. Idempotent."""
    home = os.path.join(config.nas_data_root(), config.NAS_USERS_SUBPATH, _safe_user(user))
    made = []
    for name in TIER_DIRS:
        p = os.path.join(home, name)
        if not os.path.exists(p):
            os.makedirs(p, exist_ok=True)
            made.append(name)
    return made


def _dir_bytes(path: str) -> tuple[int, int]:
    """(bytes, files) under a path, not crossing filesystem boundaries.

    Not crossing is deliberate: a USB drive grafted inside the tree is its own
    storage with its own capacity, and folding it into a home's total would
    double-count it against the pool.
    """
    total = files = 0
    if not os.path.isdir(path):
        return 0, 0
    try:
        root_dev = os.stat(path).st_dev
    except OSError:
        return 0, 0
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda e: None):
        try:
            if os.stat(dirpath).st_dev != root_dev:
                dirnames[:] = []
                continue
        except OSError:
            continue
        for fn in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, fn))
            except OSError:
                continue
            if os.path.isfile(os.path.join(dirpath, fn)):
                total += st.st_size
                files += 1
    return total, files


def usage(refresh: bool = False) -> dict:
    """How much of the NAS sits in each tier, per user and overall.

    Walks the tree rather than trusting a running total: a tier is decided by
    where a file IS, and files are moved by every tool that touches the NAS —
    Filebrowser, an app writing output, a shell. A counter maintained on our own
    writes would drift the first time anything else moved a file.
    """
    now = time.time()
    if not refresh and _usage_cache["value"] and now - _usage_cache["at"] < _USAGE_TTL:
        return _usage_cache["value"]
    users_root = os.path.join(config.nas_data_root(), config.NAS_USERS_SUBPATH)
    per_tier = {t: {"bytes": 0, "files": 0} for t in TIER_META}
    per_user: list[dict] = []
    if os.path.isdir(users_root):
        for u in sorted(os.listdir(users_root)):
            home = os.path.join(users_root, u)
            if not os.path.isdir(home):
                continue
            row = {"user": u, "tiers": {}}
            counted_dirs = set()
            for name, t in TIER_DIRS.items():
                b, f = _dir_bytes(os.path.join(home, name))
                row["tiers"][t] = {"bytes": b, "files": f}
                per_tier[t]["bytes"] += b
                per_tier[t]["files"] += f
                counted_dirs.add(name)
            # Everything not in a tier folder is the default tier.
            b_all, f_all = _dir_bytes(home)
            b_tiered = sum(row["tiers"][t]["bytes"] for t in row["tiers"])
            f_tiered = sum(row["tiers"][t]["files"] for t in row["tiers"])
            row["tiers"][DEFAULT_TIER] = {"bytes": max(0, b_all - b_tiered),
                                          "files": max(0, f_all - f_tiered)}
            per_tier[DEFAULT_TIER]["bytes"] += row["tiers"][DEFAULT_TIER]["bytes"]
            per_tier[DEFAULT_TIER]["files"] += row["tiers"][DEFAULT_TIER]["files"]
            per_user.append(row)
    out = {
        "tiers": [{**TIER_META[t], **per_tier[t]} for t in sorted(per_tier)],
        "users": per_user,
        "overrides": len(_load()),
        "computed_at": int(now),
    }
    _usage_cache.update({"at": now, "value": out})
    return out


def eligible_sources(tier: int, sources: list[dict]) -> list[dict]:
    """Which pool sources may hold data of this tier.

    The only hard rule today is ownership: tiers 2 and 3 keep per-user files, so
    a filesystem that cannot record an owner is disqualified outright rather
    than merely discouraged. Copy count and snapshot policy are declared in
    TIER_META and enforced by the replication layer, not here.
    """
    meta = TIER_META.get(int(tier)) or TIER_META[DEFAULT_TIER]
    out = []
    for s in sources:
        if s.get("role") == "apps" or not s.get("online"):
            continue
        if meta["requires_posix"] and not s.get("posix"):
            continue
        out.append(s)
    return out
