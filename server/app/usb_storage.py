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

App hosting (install once, see containers/nfs-server/sandos-usb-dockerd[@.service]):
  - Copy sandos-usb-dockerd to /usr/local/bin/, chmod +x.
  - Copy sandos-usb-dockerd@.service to /etc/systemd/system/, `systemctl daemon-reload`.
  - Sudoers (/etc/sudoers.d/62-sandos-usb-dockerd):
      control ALL=(root) NOPASSWD: /usr/local/bin/sandos-usb-dockerd
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

# Everything SandOS ever puts ON a drive's own filesystem (data-mount
# bind-targets, app-hosting's dockerd storage, portable app manifests) lives
# under this ONE visible top-level folder — never scattered loose alongside
# the drive owner's own files.
SANDOS_ROOT_DIRNAME = "SandOS"
_README = """# SandOS — please don't edit these folders by hand

This drive is registered with a Sand-OS Server Manager. Everything it needs
lives inside THIS "SandOS" folder — your own files elsewhere on the drive
are never touched or moved.

- `app-hosting/docker-data/` — a second Docker daemon's private image
  storage (so an app's image can live on this drive instead of the
  server's own disk). Opaque internal format — editing or deleting
  anything in here can corrupt every app whose image lives on this drive.
- `app-hosting/apps/<app-id>/appdef.json` — a small, portable description
  of one app (name, icon, how to launch it), so plugging this drive into
  a DIFFERENT Server Manager lets it offer to import that app. Safe to
  read, not meant to be hand-edited.
- `data-mounts/<app-id>/<user>/<mount-name>/` — one app's actual saved
  data (settings, files) when you've chosen to store it here instead of
  on the server's own disk.

All of this is managed through the Sand-OS dashboard (Fleet page, and each
app's gear menu → Storage/Image location) — you shouldn't need to touch
any of it by hand. Deleting a folder here permanently removes that
data/image.
"""


def ensure_sandos_readme(mountpoint: str) -> None:
    """Write/refresh the explanatory README in a drive's SandOS/ folder.
    Idempotent and cheap — safe to call every time anything is written
    there (data-mount creation, app-hosting start, manifest write)."""
    root = os.path.join(mountpoint, SANDOS_ROOT_DIRNAME)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write(_README)

_lock = threading.Lock()
_devices: dict[str, dict] = {}   # uuid -> {name, label, size, mountpoint, assign}


def _lsblk() -> list[dict]:
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,TRAN,SIZE,LABEL,UUID,MOUNTPOINT,TYPE,HOTPLUG,FSTYPE"],
            capture_output=True, text=True, timeout=10).stdout
        return json.loads(out).get("blockdevices", [])
    except Exception:  # noqa: BLE001
        return []


# Filesystems with no Unix ownership/permission model — dockerd's data-root
# needs real chown/chmod/hardlinks (overlay2), which these can never provide.
# Fine for plain data storage (the existing NAS-graft feature), fundamentally
# incompatible with app-hosting (a second dockerd) — checked upfront in
# set_app_hosting() so this fails with one clear message instead of dockerd
# crash-looping trying and failing to chown its data-root.
_NON_POSIX_FSTYPES = {"vfat", "exfat", "ntfs", "ntfs3"}


def usb_partitions() -> list[dict]:
    """Partitions on hotplug/USB disks: [{name,uuid,label,size,mountpoint,fstype}]."""
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
                    "fstype": (part.get("fstype") or "").lower(),
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


_DOCKERD_HELPER = "/usr/local/bin/sandos-usb-dockerd"
_DOCKERD_UNIT = "/etc/systemd/system/sandos-usb-dockerd@.service"
_SETUP_SCRIPT_HINT = (
    "containers/nfs-server/setup-usb-dockerd.sh (in the Sand-OS-Server-Manager repo)")


def dockerd_setup_status() -> dict:
    """Whether the one-time root setup for USB app-hosting is done — checked
    live (not assumed) so the UI can show EXACTLY what's missing instead of
    a generic 'if it isn't set up, this fails' warning. The helper file and
    unit are just world-readable file checks; the sudoers grant is read via
    `sudo -n -l` (listing your OWN grants doesn't need a password, unlike
    running an arbitrary new command would)."""
    helper_ok = os.access(_DOCKERD_HELPER, os.X_OK)
    unit_ok = os.path.isfile(_DOCKERD_UNIT)
    sudoers_ok = False
    try:
        r = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True, timeout=10)
        sudoers_ok = r.returncode == 0 and _DOCKERD_HELPER in r.stdout
    except Exception:  # noqa: BLE001
        pass
    ready = helper_ok and unit_ok and sudoers_ok
    return {
        "ready": ready, "helper_installed": helper_ok, "unit_installed": unit_ok,
        "sudoers_configured": sudoers_ok,
        "setup_hint": None if ready else f"sudo bash {_SETUP_SCRIPT_HINT}",
    }


def dockerd_socket_path(uuid: str) -> str:
    """The deterministic -H socket path for a drive's secondary dockerd —
    pure string construction, no liveness check. app_images.py uses THIS
    (not docker_host_for) for actual docker-CLI calls: if the drive isn't
    plugged in the docker CLI just fails with a plain connection-refused
    error against a nonexistent socket, which is the CORRECT behavior (the
    image genuinely isn't reachable) rather than a silent, wrong fallback to
    the local daemon."""
    return f"unix:///run/sandos-usb-docker-{uuid}.sock"


def _start_dockerd(uuid: str, mountpoint: str) -> bool:
    r = subprocess.run(["sudo", "-n", "/usr/local/bin/sandos-usb-dockerd",
                        "start", uuid, mountpoint],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def _stop_dockerd(uuid: str) -> None:
    subprocess.run(["sudo", "-n", "/usr/local/bin/sandos-usb-dockerd", "stop", uuid],
                   capture_output=True, text=True, timeout=30)


def docker_host_for(uuid: str) -> str | None:
    """The -H socket for this drive's secondary dockerd, ONLY if app-hosting
    is enabled AND the drive is currently mounted (the daemon should be up)
    — None otherwise. This is the LIVENESS-CHECKED variant, for UI status
    (e.g. 'this drive isn't connected right now') — not for making the actual
    docker call, see dockerd_socket_path()."""
    state = _load_state()
    if not state.get(uuid, {}).get("app_hosting"):
        return None
    if not mountpoint_for(uuid):
        return None
    return dockerd_socket_path(uuid)


def set_app_hosting(uuid: str, enabled: bool) -> dict:
    """Toggle whether this assigned drive runs a secondary dockerd (so an
    app's IMAGE can be relocated onto it, not just its data). Requires the
    drive to already be assigned (see assign()) and currently mounted."""
    state = _load_state()
    if uuid not in state:
        raise ValueError("assign this drive first (Fleet > USB Storage)")
    mountpoint = mountpoint_for(uuid)
    if enabled:
        if not mountpoint:
            raise RuntimeError("drive isn't mounted right now")
        setup = dockerd_setup_status()
        if not setup["ready"]:
            raise RuntimeError(
                "one-time setup needed on this Server Manager node before app hosting can "
                f"work (once, ever — not per drive): run `{setup['setup_hint']}` as root, "
                "then try again.")
        part = next((p for p in usb_partitions() if p["uuid"] == uuid), None)
        if part and part["fstype"] in _NON_POSIX_FSTYPES:
            raise RuntimeError(
                f"this drive is formatted {part['fstype']} — Docker's image storage needs "
                "real Unix permissions (chown/hardlinks), which vfat/exFAT/NTFS can't "
                "provide. Reformat it as ext4 to use it for app hosting (plain file "
                "storage on this drive is unaffected either way)")
        if not _start_dockerd(uuid, mountpoint):
            raise RuntimeError(
                "couldn't start the per-drive Docker daemon — the one-time setup looked "
                "done but starting it still failed; check `journalctl -u "
                f"sandos-usb-dockerd@{uuid}.service` on the Server Manager host")
        from . import pending_imports   # deferred: avoids a circular import
        pending_imports.scan_drive(uuid, mountpoint)
    else:
        _stop_dockerd(uuid)
    state[uuid]["app_hosting"] = enabled
    _save_state(state)
    return {"uuid": uuid, "app_hosting": enabled}


def list_devices() -> list[dict]:
    """Current USB partitions annotated with known assignments."""
    state = _load_state()
    out = []
    for part in usb_partitions():
        known = state.get(part["uuid"], {})
        marker = _read_marker(part["mountpoint"]) if part["mountpoint"] else None
        out.append({**part, "assign": (marker or known).get("assign", ""),
                    "app_hosting": bool(known.get("app_hosting"))})
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
    # Merge, not overwrite — re-assigning (e.g. a label change) must not lose
    # an existing app_hosting flag out from under a drive that's already
    # hosting a relocated app image.
    state[uuid] = {**state.get(uuid, {}), "assign": target, "label": part["label"]}
    _save_state(state)
    return {"uuid": uuid, "mountpoint": mountpoint, "assign": target,
            "nas_path": nas_path,
            "nas_visible": nas_path is not None}


def forget(uuid: str) -> dict:
    """Unregister a drive from this OS: delete its marker file(s) and the
    server-side state entry. The drive's DATA is untouched."""
    state = _load_state()
    if state.get(uuid, {}).get("app_hosting"):
        _stop_dockerd(uuid)   # never leave an orphaned per-drive daemon running
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
    if known.get("app_hosting"):
        _stop_dockerd(uuid)   # the drive is about to be wiped — nothing left to host
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
    if known.get("app_hosting"):
        # Stop the daemon (its data-root is about to disappear from under it)
        # but keep the app_hosting FLAG — re-inserting the drive resumes it
        # via the poller, same as NAS grafting already does for assignment.
        _stop_dockerd(uuid)
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


def _dockerd_active(uuid: str) -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet",
                        f"sandos-usb-dockerd@{uuid}.service"], timeout=10)
    return r.returncode == 0


def _ensure_dockerd_running(uuid: str, mountpoint: str, info: dict) -> None:
    """Resume an app-hosting drive's daemon on re-insertion — same 'known
    assignment survives unplug/replug' idea the NAS grafting already gets,
    just for the secondary dockerd instead of a bind mount. Also scans the
    drive for portable app manifests (deferred import: pending_imports.py
    imports app_images.py, which imports THIS module — importing it lazily
    here, not at module load time, avoids a circular import)."""
    if not info.get("app_hosting"):
        return
    if not _dockerd_active(uuid):
        _start_dockerd(uuid, mountpoint)
    from . import pending_imports
    pending_imports.scan_drive(uuid, mountpoint)


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
                        _ensure_dockerd_running(part["uuid"], mp, info)
                elif part["mountpoint"] and part["uuid"] in state:
                    info = state[part["uuid"]]
                    target = _nas_target(info["assign"], info["label"])
                    if not os.path.ismount(target):
                        _graft(part["mountpoint"], info["assign"], info["label"])
                    _ensure_dockerd_running(part["uuid"], part["mountpoint"], info)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_POLL_S)


def start_poller() -> None:
    threading.Thread(target=_poll_loop, daemon=True, name="usb-poll").start()
