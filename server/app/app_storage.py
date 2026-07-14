"""Per-app storage location — move a Mount's data off the node's local disk (or
the fleet NAS) onto an assigned USB drive, and back again.

This is deliberately separate from `app_variants.py` (which relocates which
IMAGE bytes are installed) — this module relocates DATA. The two are
independent: an app's image always lives on the node's own Docker storage;
only its per-user/shared Mounts (settings, DB, caches — see models.Mount) can
be pointed at a USB drive.

Design, mirroring app_variants.py's caution:
  - Overrides are a small per-node JSON state file (NAS_ROOT — same root
    app_variants/usb_storage already use), keyed "{app_id}:{user}:{mount}".
    Absent an override, a Mount runs exactly as declared on its AppDef.
  - move()    = refuse while the instance is running (same guard as
    app_variants.uninstall), create the new backing, copy bytes with a
    throwaway alpine container, verify sizes roughly match, THEN flip the
    override. The OLD volume is left alone — freeing it is a separate,
    explicit step (delete_old), never implicit.
  - A `usb` target fails fast if the drive isn't currently mounted — no
    silent empty-volume fallback. That's what makes "unplug the drive, plug
    it into another Server Manager node, the app's data follows" safe: the
    app simply won't start pointed at nothing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from . import config, docker_backend, registry, usb_storage
from .models import Mount

_STATE_FILE = os.path.join(config.NAS_ROOT, ".app-storage-state.json")

_lock_file_guard = None  # module-level lock not needed: FastAPI threadpool + os is atomic enough for read-modify-write of a small JSON file guarded by move()'s running-instance check


def _safe(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _key(app_id: str, user: str, mount_name: str) -> str:
    return f"{app_id}:{user}:{mount_name}"


def _mount(app_id: str, mount_name: str) -> Mount | None:
    app = registry.APPS.get(app_id)
    if not app:
        return None
    return next((m for m in app.mounts if m.name == mount_name), None)


def _relocatable(app_id: str) -> list[Mount]:
    """Mounts eligible to move. Excludes scope='root' (Nextcloud's whole-NAS
    scoping mount — moving that would break every user's file access, not
    just one app's data) and read-only mounts (nothing to relocate: it's a
    shared library this app only reads)."""
    app = registry.APPS.get(app_id)
    if not app:
        return []
    return [m for m in app.mounts if m.scope != "root" and not m.ro]


def effective_storage(app_id: str, user: str, m: Mount) -> tuple[str, str | None]:
    """(mode, usb_uuid) a Mount should actually use — the state-file override
    if one is set for THIS (app, user, mount), else the Mount's own default."""
    override = _load_state().get(_key(app_id, user, m.name))
    if override:
        return override.get("mode", m.storage), override.get("usb_uuid")
    return getattr(m, "storage", "local"), None


def _volume_size_bytes(volume: str) -> int | None:
    """On-disk size of a docker volume's content — a throwaway alpine `du`,
    same idiom `move()` already uses for the copy+verify step. None if the
    volume doesn't exist yet (never created) or the probe fails."""
    r = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{volume}:/v", "alpine",
         "du", "-sb", "/v"],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def _current_volume(app_id: str, user: str, m: Mount) -> str:
    """The volume name currently backing this mount, whatever mode it's in —
    used to find the OLD data to copy from during a move."""
    mode, usb_uuid = effective_storage(app_id, user, m)
    if mode == "usb" and usb_uuid:
        return docker_backend.usb_volume_name(usb_uuid, app_id, user, m)
    if mode == "nfs" and config.NAS_ENABLED:
        return docker_backend.nfs_volume_name(user, m)
    return registry.resolve_volume(app_id, user, m)


def _local_free_bytes() -> int | None:
    try:
        return shutil.disk_usage("/var/lib/docker").free
    except OSError:
        return None


def _nas_free_bytes() -> int | None:
    try:
        return shutil.disk_usage(config.NAS_ROOT).free
    except OSError:
        return None


def list_locations(app_id: str, user: str) -> dict:
    """One row per relocatable Mount: current location + SIZE (so 'how much
    room will this take up' is answerable before moving anything), plus the
    menu of targets a 'manage storage' UI can move it to, each annotated with
    how much free space is actually there right now."""
    app = registry.APPS.get(app_id)
    if not app:
        raise KeyError(app_id)
    devices = [d for d in usb_storage.list_devices() if d.get("mountpoint") and d.get("assign")]
    local_free = _local_free_bytes()
    nas_free = _nas_free_bytes() if config.NAS_ENABLED else None
    rows = []
    for m in _relocatable(app_id):
        mode, usb_uuid = effective_storage(app_id, user, m)
        options = [{"mode": "local", "label": "This server (local disk)", "free_bytes": local_free}]
        if config.NAS_ENABLED:
            options.append({"mode": "nfs", "label": "Fleet NAS (shared across nodes)",
                             "free_bytes": nas_free})
        for d in devices:
            options.append({
                "mode": "usb", "usb_uuid": d["uuid"],
                "label": f"USB: {d['label']}",
                "free_bytes": usb_storage.free_bytes_for(d["uuid"]),
            })
        rows.append({
            "mount_name": m.name, "path": m.path, "scope": m.scope,
            "current_mode": mode, "current_usb_uuid": usb_uuid,
            "current_size_bytes": _volume_size_bytes(_current_volume(app_id, user, m)),
            "options": options,
        })
    return {"app_id": app_id, "mounts": rows}


def _instance_running(app_id: str, user: str) -> bool:
    name = registry.instance_name(app_id, user)
    return docker_backend.running(name)


def move(app_id: str, user: str, mount_name: str, target_mode: str,
         usb_uuid: str | None = None) -> dict:
    if target_mode not in ("local", "nfs", "usb"):
        raise ValueError("target_mode must be local, nfs or usb")
    if target_mode == "usb" and not usb_uuid:
        raise ValueError("usb_uuid is required when target_mode is usb")
    m = _mount(app_id, mount_name)
    if m is None or m not in _relocatable(app_id):
        raise KeyError(f"{app_id} has no relocatable mount {mount_name!r}")
    if _instance_running(app_id, user):
        raise ValueError(f"stop {app_id} before moving its storage")

    old_vol = _current_volume(app_id, user, m)

    if target_mode == "usb":
        new_vol = docker_backend.ensure_usb_volume(usb_uuid, app_id, user, m)
    elif target_mode == "nfs" and config.NAS_ENABLED:
        new_vol = docker_backend.ensure_nfs_volume(user, m)
    else:
        new_vol = registry.resolve_volume(app_id, user, m)
        subprocess.run(["docker", "volume", "create", new_vol],
                       capture_output=True, timeout=15)

    if new_vol == old_vol:
        raise ValueError("that's already where this mount's data lives")

    copy = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{old_vol}:/from", "-v", f"{new_vol}:/to",
         "alpine", "sh", "-c",
         "cp -a /from/. /to/ 2>/dev/null; echo OLD:$(du -sb /from 2>/dev/null | cut -f1); "
         "echo NEW:$(du -sb /to 2>/dev/null | cut -f1)"],
        capture_output=True, text=True, timeout=600)
    if copy.returncode != 0:
        raise RuntimeError(copy.stderr.strip() or "copy failed")
    sizes = dict(re.findall(r"(OLD|NEW):(\d+)", copy.stdout))
    old_sz, new_sz = int(sizes.get("OLD", 0)), int(sizes.get("NEW", 0))
    # Tolerate a little drift (sparse files, filesystem overhead) but catch an
    # obviously-truncated copy rather than silently switching to bad data.
    if old_sz > 0 and new_sz < old_sz * 0.9:
        raise RuntimeError(
            f"copy looks incomplete ({new_sz} of {old_sz} bytes) — not switching over")

    state = _load_state()
    state[_key(app_id, user, mount_name)] = {"mode": target_mode, "usb_uuid": usb_uuid}
    _save_state(state)
    return {"ok": True, "old_volume": old_vol, "new_volume": new_vol,
            "bytes_copied": new_sz, "mode": target_mode}


def delete_old(app_id: str, user: str, mount_name: str, old_volume: str) -> dict:
    """Explicit follow-up to move(): actually free the OLD copy. Refuses if
    `old_volume` is (now) the active one — you moved storage back, or never
    moved it — so this can never delete live data."""
    m = _mount(app_id, mount_name)
    if m is None:
        raise KeyError(f"{app_id} has no mount {mount_name!r}")
    if old_volume == _current_volume(app_id, user, m):
        raise ValueError("that volume is the ACTIVE one for this mount — refusing to delete it")
    r = subprocess.run(["docker", "volume", "rm", old_volume], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return {"ok": True, "removed": old_volume}
