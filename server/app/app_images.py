"""Per-app IMAGE location — move which Docker daemon actually stores an
app's image: the node's default local daemon, or a secondary one rooted on
an assigned, app-hosting-enabled USB drive (see usb_storage.py). This is
deliberately separate from app_storage.py, which relocates an app's DATA
(Mounts) — the two are independent and an app can have either, both, or
neither relocated.

Two distinct actions, not two branches of one flow (they have opposite
goals — see the Storage Fleet Plan KB doc):
  move_to_usb(..., keep_local=False)  "Move"    frees local disk — the local
                                                 image is removed once the USB
                                                 copy is verified present.
  move_to_usb(..., keep_local=True)   "Mirror"   keeps BOTH copies, for
                                                 redundancy — uses MORE disk.
  move_to_local(...)                             the reverse of either;
                                                 leaves the USB copy in place
                                                 by default (never delete
                                                 without an explicit,
                                                 separate follow-up — same
                                                 carefulness as app_storage).

A USB-hosted app also gets a portable manifest written next to it
(<mountpoint>/SandOS/app-hosting/apps/<app_id>/appdef.json) — everything
another Server Manager node needs to display + relaunch this app once the
drive is plugged in there. See pending_imports.py for that side of the
story. Everything SandOS puts on a drive lives under one visible "SandOS/"
folder (with its own README) — personal files elsewhere on the drive are
never touched.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess

from . import config, registry, usb_storage
from .models import AppDef

_STATE_FILE = os.path.join(config.NAS_ROOT, ".app-images-state.json")
MANIFEST_NAME = "appdef.json"
_MANIFEST_ROOT = os.path.join("SandOS", "app-hosting", "apps")


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


def location(app_id: str) -> dict:
    """{'mode': 'local'|'usb', 'usb_uuid': str|None, 'last_usb_uuid': str|None}
    — the DECLARED location, independent of whether that drive is reachable
    right now. `last_usb_uuid` is only set after a move-back-to-local, when a
    leftover copy may still be sitting on that drive (see remove_usb_copy)."""
    entry = _load_state().get(app_id)
    if not entry:
        return {"mode": "local", "usb_uuid": None, "last_usb_uuid": None}
    return {"mode": entry.get("mode", "local"), "usb_uuid": entry.get("usb_uuid"),
            "last_usb_uuid": entry.get("last_usb_uuid")}


def list_image_options(app_id: str) -> dict:
    """Current image location + size, and every USB drive it could move/
    mirror to (with free space) — backs the Manage modal's 'Image location'
    section. Any assigned+mounted drive with a POSIX filesystem qualifies,
    not just ones already app-hosting-enabled: picking one that isn't
    enabled yet turns it on as PART of the move/mirror action (the Fleet
    page has no separate "enable app hosting" control on purpose — that
    decision belongs here, in context, not as a standalone pre-step)."""
    app = registry.APPS.get(app_id)
    if app is None:
        raise KeyError(app_id)
    loc = location(app_id)
    host = active_docker_host(app_id)
    tag = _image_tag(app)
    size = _image_size(tag, host) if (host or loc["mode"] == "local") else None
    drives = [d for d in usb_storage.list_devices()
              if d.get("mountpoint") and d.get("assign")
              and d.get("fstype") not in usb_storage._NON_POSIX_FSTYPES]
    options = [{"mode": "local", "label": "This server (local disk)"}]
    for d in drives:
        options.append({
            "mode": "usb", "usb_uuid": d["uuid"],
            "label": f"USB: {d['label']}" + ("" if d.get("app_hosting") else " (will enable app hosting)"),
            "app_hosting": bool(d.get("app_hosting")),
            "free_bytes": usb_storage.free_bytes_for(d["uuid"]),
        })
    return {
        "app_id": app_id, "mode": loc["mode"], "usb_uuid": loc["usb_uuid"],
        "last_usb_uuid": loc["last_usb_uuid"], "size_bytes": size,
        "options": options,
    }


def active_docker_host(app_id: str) -> str | None:
    """The -H target every docker-CLI call for this app's IMAGE must use.
    None = the node's default daemon."""
    loc = location(app_id)
    if loc["mode"] == "usb" and loc["usb_uuid"]:
        return usb_storage.dockerd_socket_path(loc["usb_uuid"])
    return None


def _image_tag(app: AppDef) -> str:
    from . import app_variants
    return app_variants.active_image(app)


def _image_exists(tag: str, host: str | None) -> bool:
    args = ["docker"] + (["-H", host] if host else []) + ["image", "inspect", tag]
    return subprocess.run(args, capture_output=True, timeout=15).returncode == 0


def _image_size(tag: str, host: str | None) -> int | None:
    args = (["docker"] + (["-H", host] if host else [])
            + ["image", "inspect", tag, "--format", "{{.Size}}"])
    r = subprocess.run(args, capture_output=True, text=True, timeout=15)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _instance_running_anywhere(app_id: str) -> bool:
    """True if ANY user's instance of this app is running — an image can't
    be pulled out from under a live container, and unlike app_storage.py's
    per-(app,user) mount move, an image move affects every user of the app.
    Checks EVERY active daemon (default + any USB-hosting drive) — if the
    image is already on a USB drive and running there, the default daemon
    alone would wrongly report 'nothing running'."""
    from . import docker_backend, registry as _registry
    app = _registry.APPS.get(app_id)
    if not app:
        return False
    prefix = f"sm-{_safe(app_id)}"
    for host in docker_backend.all_docker_hosts():
        for name in docker_backend.list_sm_containers(host=host):
            if name == prefix or name.startswith(prefix + "-"):
                if docker_backend.running(name, host=host):
                    return True
    return False


def _safe(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def write_manifest(app: AppDef, mountpoint: str) -> None:
    """Everything a DIFFERENT Server Manager node needs to render this app's
    card and relaunch it, once this drive is plugged in there — the
    'auto-populate the dashboard' half of the portability story."""
    usb_storage.ensure_sandos_readme(mountpoint)
    path = os.path.join(mountpoint, _MANIFEST_ROOT, app.id, MANIFEST_NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    manifest = {
        "id": app.id, "label": app.label, "icon": app.icon, "color": app.color,
        "desc": app.desc, "kind": app.kind, "mode": app.mode,
        "internal_port": app.internal_port, "gpu": app.gpu,
        "mem_limit": app.mem_limit, "env": app.env,
        "proxy_subpath": app.proxy_subpath,
        "image_tag": _image_tag(app),
        "mounts": [dataclasses.asdict(m) for m in app.mounts],
    }
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)


def read_manifest(mountpoint: str, app_id: str) -> dict | None:
    path = os.path.join(mountpoint, _MANIFEST_ROOT, app_id, MANIFEST_NAME)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def list_manifests(mountpoint: str) -> list[dict]:
    """Every app manifest present on a drive — the USB poller's pending-
    imports scan uses this."""
    root = os.path.join(mountpoint, _MANIFEST_ROOT)
    out = []
    if not os.path.isdir(root):
        return out
    for app_id in os.listdir(root):
        m = read_manifest(mountpoint, app_id)
        if m:
            out.append(m)
    return out


def move_to_usb(app_id: str, usb_uuid: str, keep_local: bool) -> dict:
    app = registry.APPS.get(app_id)
    if app is None:
        raise KeyError(app_id)
    if _instance_running_anywhere(app_id):
        raise ValueError(f"stop every running instance of {app.label} before moving its image")

    mountpoint = usb_storage.mountpoint_for(usb_uuid)
    if not mountpoint:
        raise RuntimeError("that USB drive isn't plugged in right now")
    host = usb_storage.docker_host_for(usb_uuid)
    if not host:
        # First use of this drive for app-hosting — turn it on as PART of the
        # move/mirror action rather than requiring a separate Fleet-page
        # step. Raises the same clear setup/fstype errors either way.
        usb_storage.set_app_hosting(usb_uuid, True)
        host = usb_storage.docker_host_for(usb_uuid)

    tag = _image_tag(app)
    if not _image_exists(tag, None):
        raise RuntimeError(f"{app.label}'s image isn't installed locally — nothing to move")

    # Stream straight across — no intermediate tarball on local disk, so a
    # move never transiently needs double the local space.
    save = subprocess.Popen(["docker", "image", "save", tag], stdout=subprocess.PIPE)
    load = subprocess.run(["docker", "-H", host, "image", "load"],
                          stdin=save.stdout, capture_output=True, text=True, timeout=1800)
    save.stdout.close()
    save.wait(timeout=1800)
    if save.returncode != 0 or load.returncode != 0:
        raise RuntimeError(load.stderr.strip() or "image transfer failed")
    if not _image_exists(tag, host):
        raise RuntimeError("image transfer reported success but the image isn't on the drive")

    write_manifest(app, mountpoint)

    state = _load_state()
    state[app_id] = {"mode": "usb", "usb_uuid": usb_uuid}
    _save_state(state)

    if not keep_local:
        subprocess.run(["docker", "rmi", tag], capture_output=True, text=True, timeout=60)

    return {"ok": True, "mode": "usb", "usb_uuid": usb_uuid, "kept_local": keep_local,
            "size_bytes": _image_size(tag, host)}


def move_to_local(app_id: str) -> dict:
    app = registry.APPS.get(app_id)
    if app is None:
        raise KeyError(app_id)
    if _instance_running_anywhere(app_id):
        raise ValueError(f"stop every running instance of {app.label} before moving its image")

    loc = location(app_id)
    if loc["mode"] != "usb":
        raise ValueError(f"{app.label}'s image is already local")
    host = usb_storage.docker_host_for(loc["usb_uuid"])
    if not host:
        raise RuntimeError("that USB drive isn't plugged in right now")

    tag = _image_tag(app)
    save = subprocess.Popen(["docker", "-H", host, "image", "save", tag], stdout=subprocess.PIPE)
    load = subprocess.run(["docker", "image", "load"],
                          stdin=save.stdout, capture_output=True, text=True, timeout=1800)
    save.stdout.close()
    save.wait(timeout=1800)
    if save.returncode != 0 or load.returncode != 0:
        raise RuntimeError(load.stderr.strip() or "image transfer failed")
    if not _image_exists(tag, None):
        raise RuntimeError("image transfer reported success but the image isn't local")

    state = _load_state()
    # Keep the drive's uuid around (as "local" now, not "usb") so a later
    # remove_usb_copy() knows which drive's leftover copy to clean up —
    # popping the entry entirely would lose that. USB copy is deliberately
    # left in place otherwise — freeing it is a separate, explicit step,
    # same "never delete without a follow-up" rule as app_storage.py's
    # move()/delete_old().
    state[app_id] = {"mode": "local", "last_usb_uuid": loc["usb_uuid"]}
    _save_state(state)
    return {"ok": True, "mode": "local", "size_bytes": _image_size(tag, None)}


def remove_usb_copy(app_id: str) -> dict:
    """Explicit follow-up to move_to_local(): actually delete the leftover
    image off the drive once the local copy is confirmed good. Refuses if
    the app is currently configured to run FROM that drive (i.e. it's the
    active copy, not a leftover) — can't accidentally delete a live copy."""
    loc = location(app_id)
    if loc["mode"] == "usb":
        raise ValueError("that's the ACTIVE copy — move the image back to local first")
    app = registry.APPS.get(app_id)
    if app is None:
        raise KeyError(app_id)
    usb_uuid = _load_state().get(app_id, {}).get("last_usb_uuid")
    if not usb_uuid:
        raise ValueError("no known USB copy to remove")
    host = usb_storage.docker_host_for(usb_uuid)
    if not host:
        raise RuntimeError("that USB drive isn't plugged in right now")
    tag = _image_tag(app)
    r = subprocess.run(["docker", "-H", host, "rmi", tag],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    state = _load_state()
    state.pop(app_id, None)
    _save_state(state)
    return {"ok": True, "removed": tag, "usb_uuid": usb_uuid}