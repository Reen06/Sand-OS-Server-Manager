"""Same-mesh app portability — the 'plug the drive into another Server
Manager node' half of the story. A USB-hosted app's manifest
(<mountpoint>/sandos-apps/<app_id>/appdef.json>, written by
app_images.write_manifest) is everything a DIFFERENT node needs to render
and relaunch the app. But per the owner's explicit call: a drive's app is
NEVER auto-registered on a node that's never seen it before — it's staged
here as a pending import, surfaced in the Fleet page, and only added to the
live catalogue on an explicit admin click (import_app). No silent execution
of code from removable media.

True cross-mesh (an unrelated Hub, not just another node in this fleet) is
explicitly NOT handled here — see the Storage Fleet Plan KB doc.
"""
from __future__ import annotations

import threading

from . import app_images, registry
from .models import AppDef, Mount

_lock = threading.Lock()
# app_id -> {"manifest": dict, "usb_uuid": str, "mountpoint": str}
_pending: dict[str, dict] = {}


def scan_drive(uuid: str, mountpoint: str) -> None:
    """Called by usb_storage.py's poller for every mounted, app-hosting-
    enabled drive. Stages any manifest not already a live catalogue entry
    (or already staged) — never touches registry.APPS itself."""
    for manifest in app_images.list_manifests(mountpoint):
        app_id = manifest.get("id")
        if not app_id or app_id in registry.APPS:
            continue
        with _lock:
            _pending[app_id] = {"manifest": manifest, "usb_uuid": uuid, "mountpoint": mountpoint}


def list_pending() -> list[dict]:
    with _lock:
        return [
            {"app_id": app_id, "label": entry["manifest"].get("label", app_id),
             "icon": entry["manifest"].get("icon"), "color": entry["manifest"].get("color"),
             "desc": entry["manifest"].get("desc"), "usb_uuid": entry["usb_uuid"]}
            for app_id, entry in _pending.items()
        ]


def import_app(app_id: str) -> dict:
    """Admin-confirmed: register the manifest as a real, launchable AppDef.
    Refuses on an id collision instead of silently overwriting — an existing
    entry (even one added moments ago by someone else) always wins; resolve
    the clash manually (this is a deliberate scope cut, not an oversight)."""
    with _lock:
        entry = _pending.get(app_id)
    if entry is None:
        raise KeyError(f"no pending import for {app_id!r}")
    if app_id in registry.APPS:
        raise ValueError(
            f"{app_id!r} already exists in this node's catalogue — "
            "resolve the id collision manually before importing")

    m = entry["manifest"]
    app = AppDef(
        id=m["id"], label=m.get("label", m["id"]), icon=m.get("icon", "cpu"),
        color=m.get("color", "blue"), desc=m.get("desc", ""),
        image=m["image_tag"], kind=m.get("kind", "web"), mode=m.get("mode", "shared"),
        internal_port=m.get("internal_port", 8080), gpu=m.get("gpu", False),
        mem_limit=m.get("mem_limit", ""), env=m.get("env", {}),
        proxy_subpath=m.get("proxy_subpath", "forward"),
        mounts=[Mount(**mount) for mount in m.get("mounts", [])],
    )
    registry.APPS[app_id] = app

    # The image lives on the drive it was imported FROM — wire that up the
    # same way a manual move_to_usb() would, so spawn() finds it there.
    state = app_images._load_state()
    state[app_id] = {"mode": "usb", "usb_uuid": entry["usb_uuid"]}
    app_images._save_state(state)

    with _lock:
        _pending.pop(app_id, None)
    return {"ok": True, "app_id": app_id, "usb_uuid": entry["usb_uuid"]}


def dismiss(app_id: str) -> dict:
    """Forget a pending import without registering it (e.g. it's not
    actually wanted on this node) — purely in-memory, re-appears next poll
    if the drive is still there; nothing on the drive is touched."""
    with _lock:
        _pending.pop(app_id, None)
    return {"ok": True}
