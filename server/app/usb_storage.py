"""USB storage hotplug — detect thumbdrives on the NAS node, assign them to a
user's profile or to general storage, and remember the choice on the drive.

v1 semantics (per KB 'Storage Fleet Plan'):
  - Detection: `lsblk -J` → unmounted USB partitions.
  - Assign: mount via udisksctl (unprivileged; needs the polkit rule below on
    a headless box), write a `sandos-storage.md` marker at the drive root
    (uuid + assignment), and register it in a local state file. Re-inserting a
    marked drive auto-mounts on the next poll.
  - Exposure: the mounted drive appears as an extra root in the per-user cloud
    file picker ("USB <label>") and — for shared drives — for every user.
    (Filebrowser/NFS in-export visibility needs the crossmnt export follow-up
    tracked in the KB plan; the picker works today.)

Polkit rule for headless mounting as the SM user (install once):
  /etc/polkit-1/rules.d/60-sandos-usb.rules
    polkit.addRule(function(action, subject) {
      if (action.id.indexOf("org.freedesktop.udisks2.filesystem-mount") == 0 &&
          subject.user == "control") { return polkit.Result.YES; }
    });
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import config

MARKER = ".sandos-storage.md"          # hidden by default
_LEGACY_MARKERS = ["sandos-storage.md"]  # still honored/cleaned up
_STATE_FILE = os.path.join(config.NAS_ROOT, ".usb-state.json")
_POLL_S = 10

_lock = threading.Lock()
_devices: dict[str, dict] = {}   # uuid -> {name, label, size, mountpoint, assign}


def _lsblk() -> list[dict]:
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TRAN,SIZE,LABEL,UUID,MOUNTPOINT,TYPE,HOTPLUG"],
            capture_output=True, text=True, timeout=10).stdout
        return json.loads(out).get("blockdevices", [])
    except Exception:  # noqa: BLE001
        return []


def usb_partitions() -> list[dict]:
    """Partitions on hotplug/USB disks: [{name,uuid,label,size,mountpoint}]."""
    out = []
    for disk in _lsblk():
        if disk.get("type") != "disk":
            continue
        if disk.get("tran") != "usb" and not disk.get("hotplug"):
            continue
        for part in disk.get("children") or []:
            if part.get("type") == "part" and part.get("uuid"):
                out.append({
                    "name": part["name"],
                    "uuid": part["uuid"],
                    "label": part.get("label") or part["name"],
                    "size": part.get("size", ""),
                    "mountpoint": part.get("mountpoint"),
                })
    return out


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


def _mount(name: str) -> str | None:
    """udisksctl mount; returns the mountpoint or None."""
    r = subprocess.run(["udisksctl", "mount", "-b", f"/dev/{name}",
                        "--no-user-interaction"],
                       capture_output=True, text=True, timeout=30)
    m = re.search(r"at (\S+)", r.stdout + r.stderr)
    return m.group(1).rstrip(".") if m else None


def _nas_target(assign_to: str, label: str) -> str:
    """Where an assigned drive grafts into the NAS tree (thus every app):
    user drives inside the user's home, shared drives inside the media library."""
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "drive"
    if assign_to == "shared":
        return os.path.join(config.NAS_ROOT, config.NAS_SHARED_SUBPATH,
                            "media", f"USB-{safe_label}")
    user = re.sub(r"[^a-z0-9]+", "-", assign_to[len("user:"):].lower()).strip("-")
    return os.path.join(config.NAS_ROOT, config.NAS_USERS_SUBPATH, user,
                        f"USB-{safe_label}")


def _graft(mountpoint: str, assign_to: str, label: str) -> str | None:
    """Bind the drive into the NAS tree via the root helper (sudoers-gated)."""
    target = _nas_target(assign_to, label)
    r = subprocess.run(["sudo", "-n", "/usr/local/bin/sandos-usb-bind",
                        "bind", mountpoint, target],
                       capture_output=True, text=True, timeout=15)
    return target if r.returncode == 0 else None


def _ungraft(assign_to: str, label: str) -> None:
    subprocess.run(["sudo", "-n", "/usr/local/bin/sandos-usb-bind",
                    "unbind", _nas_target(assign_to, label)],
                   capture_output=True, text=True, timeout=15)


def _marker_path(mountpoint: str) -> str | None:
    for name in [MARKER, *_LEGACY_MARKERS]:
        path = os.path.join(mountpoint, name)
        if os.path.isfile(path):
            return path
    return None


def _read_marker(mountpoint: str) -> dict | None:
    path = _marker_path(mountpoint)
    if path is None:
        return None
    meta = {}
    for line in open(path).read().splitlines():
        mm = re.match(r"^(uuid|assign|label):\s*(.+)$", line.strip())
        if mm:
            meta[mm.group(1)] = mm.group(2).strip()
    return meta or None


def _write_marker(mountpoint: str, uuid: str, assign: str, label: str) -> None:
    with open(os.path.join(mountpoint, MARKER), "w") as f:
        f.write(
            "---\n"
            f"uuid: {uuid}\n"
            f"assign: {assign}\n"
            f"label: {label}\n"
            "managed-by: SandOS Server Manager\n"
            "---\n\n"
            "This drive is registered with the SandOS fleet NAS.\n"
            f"Assignment: **{assign}** — re-inserting it auto-mounts with the\n"
            "same assignment. Delete this file to unregister.\n"
        )


def list_devices() -> list[dict]:
    """Current USB partitions annotated with known assignments."""
    state = _load_state()
    out = []
    for part in usb_partitions():
        known = state.get(part["uuid"], {})
        marker = _read_marker(part["mountpoint"]) if part["mountpoint"] else None
        out.append({**part, "assign": (marker or known).get("assign", "")})
    return out


def assign(uuid: str, target: str) -> dict:
    """Mount the partition and register it. target = 'user:<name>' | 'shared'."""
    if not (target == "shared" or target.startswith("user:")):
        raise ValueError("target must be 'shared' or 'user:<name>'")
    part = next((p for p in usb_partitions() if p["uuid"] == uuid), None)
    if part is None:
        raise FileNotFoundError(f"no USB partition with uuid {uuid}")
    mountpoint = part["mountpoint"] or _mount(part["name"])
    if not mountpoint:
        raise RuntimeError(
            "mount failed — headless mounting needs the polkit rule in "
            "usb_storage.py's docstring installed once")
    _write_marker(mountpoint, uuid, target, part["label"])
    nas_path = _graft(mountpoint, target, part["label"])
    state = _load_state()
    state[uuid] = {"assign": target, "label": part["label"]}
    _save_state(state)
    return {"uuid": uuid, "mountpoint": mountpoint, "assign": target,
            "nas_path": nas_path,
            "nas_visible": nas_path is not None}


def forget(uuid: str) -> dict:
    """Unregister a drive from this OS: delete its marker file(s) and the
    server-side state entry. The drive's DATA is untouched."""
    state = _load_state()
    state.pop(uuid, None)
    _save_state(state)
    removed = False
    part = next((p for p in usb_partitions() if p["uuid"] == uuid), None)
    if part:
        marker = _read_marker(part["mountpoint"]) if part["mountpoint"] else None
        info = marker or {}
        if info.get("assign"):
            _ungraft(info["assign"], info.get("label") or part["label"])
    if part and part["mountpoint"]:
        path = _marker_path(part["mountpoint"])
        while path:
            os.remove(path)
            removed = True
            path = _marker_path(part["mountpoint"])
    return {"uuid": uuid, "forgotten": True, "marker_removed": removed}


def format_drive(uuid: str, fs: str = "vfat") -> dict:
    """FULL ERASE: unmount and reformat the partition (everything is lost).
    Uses udisks2 over D-Bus so the same polkit grant covers it headless."""
    if fs not in ("vfat", "exfat", "ext4"):
        raise ValueError("fs must be vfat, exfat or ext4")
    part = next((p for p in usb_partitions() if p["uuid"] == uuid), None)
    if part is None:
        raise FileNotFoundError(uuid)
    known = _load_state().get(uuid, {})
    if known.get("assign"):
        _ungraft(known["assign"], known.get("label") or part["label"])
    if part["mountpoint"]:
        subprocess.run(["udisksctl", "unmount", "-b", f"/dev/{part['name']}",
                        "--no-user-interaction"],
                       capture_output=True, text=True, timeout=30)
    r = subprocess.run(
        ["busctl", "call", "org.freedesktop.UDisks2",
         f"/org/freedesktop/UDisks2/block_devices/{part['name']}",
         "org.freedesktop.UDisks2.Block", "Format", "sa{sv}", fs, "0"],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"format failed: {r.stderr.strip() or r.stdout.strip()}")
    state = _load_state()
    state.pop(uuid, None)   # old uuid is gone with the old filesystem
    _save_state(state)
    return {"uuid": uuid, "formatted": True, "fs": fs}


def eject(uuid: str) -> dict:
    part = next((p for p in usb_partitions() if p["uuid"] == uuid), None)
    if part is None:
        raise FileNotFoundError(uuid)
    known = _load_state().get(uuid, {})
    if known.get("assign"):
        _ungraft(known["assign"], known.get("label") or part["label"])
    if part["mountpoint"]:
        subprocess.run(["udisksctl", "unmount", "-b", f"/dev/{part['name']}",
                        "--no-user-interaction"],
                       capture_output=True, text=True, timeout=30)
    return {"uuid": uuid, "ejected": True}


def mountpoint_for(uuid: str) -> str | None:
    """Live mountpoint of an assigned drive, or None if it isn't currently
    plugged in/mounted. Used by app_storage.py to bind an app's data straight
    onto the drive — and to fail fast (not silently fall back to an empty
    volume) when the drive a running app depends on isn't there."""
    part = next((p for p in usb_partitions() if p["uuid"] == uuid), None)
    return part["mountpoint"] if part else None


def free_bytes_for(uuid: str) -> int | None:
    """Free space on an assigned drive's own filesystem right now — lets the
    storage-move UI show 'will it fit' before committing to a move. None if
    the drive isn't currently mounted."""
    mountpoint = mountpoint_for(uuid)
    if not mountpoint:
        return None
    try:
        return shutil.disk_usage(mountpoint).free
    except OSError:
        return None


def roots_for(user: str) -> list[dict]:
    """Extra file-picker roots from mounted, assigned drives."""
    from .files import _safe_user  # lazy: avoid import cycle

    out = []
    for dev in list_devices():
        if not dev["mountpoint"] or not dev["assign"]:
            continue
        if dev["assign"] == "shared" or dev["assign"] == f"user:{_safe_user(user)}":
            out.append({
                "id": f"usb:{dev['uuid']}",
                "label": f"USB {dev['label']}",
                "path": dev["mountpoint"],
            })
    return out


def _poll_loop() -> None:
    while True:
        try:
            state = _load_state()
            for part in usb_partitions():
                if part["mountpoint"] is None and part["uuid"] in state:
                    mp = _mount(part["name"])  # re-inserted known drive
                    info = state[part["uuid"]]
                    if mp:
                        if not _read_marker(mp):
                            _write_marker(mp, part["uuid"], info["assign"], info["label"])
                        _graft(mp, info["assign"], info["label"])
                elif part["mountpoint"] and part["uuid"] in state:
                    info = state[part["uuid"]]
                    target = _nas_target(info["assign"], info["label"])
                    if not os.path.ismount(target):
                        _graft(part["mountpoint"], info["assign"], info["label"])
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_POLL_S)


def start_poller() -> None:
    threading.Thread(target=_poll_loop, daemon=True, name="usb-poll").start()
