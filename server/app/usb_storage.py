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
import subprocess
import threading
import time

from . import config

MARKER = "sandos-storage.md"
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


def _read_marker(mountpoint: str) -> dict | None:
    path = os.path.join(mountpoint, MARKER)
    if not os.path.isfile(path):
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
    state = _load_state()
    state[uuid] = {"assign": target, "label": part["label"]}
    _save_state(state)
    return {"uuid": uuid, "mountpoint": mountpoint, "assign": target}


def eject(uuid: str) -> dict:
    part = next((p for p in usb_partitions() if p["uuid"] == uuid), None)
    if part is None:
        raise FileNotFoundError(uuid)
    if part["mountpoint"]:
        subprocess.run(["udisksctl", "unmount", "-b", f"/dev/{part['name']}",
                        "--no-user-interaction"],
                       capture_output=True, text=True, timeout=30)
    return {"uuid": uuid, "ejected": True}


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
                    if mp and not _read_marker(mp):
                        info = state[part["uuid"]]
                        _write_marker(mp, part["uuid"], info["assign"], info["label"])
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_POLL_S)


def start_poller() -> None:
    threading.Thread(target=_poll_loop, daemon=True, name="usb-poll").start()
