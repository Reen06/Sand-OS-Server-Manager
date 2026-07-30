"""Apps that live on a removable drive, and travel with it.

A drive set up for app hosting carries a whole Docker root: images, layers and
container volumes, under `SandOS/app-hosting/`. Physically, the apps are on the
drive. But which apps those are was recorded only in a state file on the node
that set it up, so unplugging the drive and moving it left the new machine
holding gigabytes of images it had no idea existed.

This module writes that knowledge ONTO the drive, as a manifest beside the
images it describes. Any node that mounts the drive can then read it and offer
those apps — running them straight from the drive, with no copy, which is the
point of moving it.

The manifest records each app's image ARCHITECTURE, because a drive is a
physical object and the machine it lands in may not be able to execute what it
holds. An x86-64 image on an ARM board is not a failure to be discovered at
launch; it is a fact that can be known the moment the drive is seen, and said
plainly.

Deliberately advisory. Nothing here mutates another node's state or starts
anything; it describes what is present so the Hub can offer the choice.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

MANIFEST_NAME = ".sandos-apps.json"
APP_HOSTING_SUBPATH = os.path.join("SandOS", "app-hosting")


def manifest_path(mountpoint: str) -> str:
    return os.path.join(mountpoint, MANIFEST_NAME)


def _image_arch(tag: str, docker_host: str | None) -> dict:
    """Architecture of a local image, so portability can be judged before use."""
    cmd = ["docker"]
    if docker_host:
        cmd += ["-H", f"unix://{docker_host}"]
    cmd += ["image", "inspect", tag, "--format", "{{.Architecture}}/{{.Os}}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return {}
        arch, _, osname = (r.stdout or "").strip().partition("/")
        return {"arch": arch or "", "os": osname or ""}
    except Exception:  # noqa: BLE001
        return {}


def build(uuid: str, mountpoint: str) -> dict:
    """Describe every app hosted on this drive, from what is really on it."""
    from . import app_images, registry, usb_storage, config

    sock = usb_storage.dockerd_socket_path(uuid)
    apps = []
    for app_id, loc in (app_images._load_state() or {}).items():
        if loc.get("mode") != "usb" or loc.get("usb_uuid") != uuid:
            continue
        app = registry.CATALOG.get(app_id) or registry.APPS.get(app_id)
        if not app:
            continue
        tag = app_images._image_tag(app)
        entry = {"id": app_id, "label": app.label, "image": tag,
                 "kind": app.kind, "gpu": bool(app.gpu)}
        entry.update(_image_arch(tag, sock))
        apps.append(entry)
    return {
        "version": 1,
        "uuid": uuid,
        # Which machine wrote this, and when. Not for trust — for explaining a
        # drive found in a drawer six months from now.
        "written_by": config.NODE_NAME,
        "written_at": int(time.time()),
        "docker_root": APP_HOSTING_SUBPATH,
        "apps": sorted(apps, key=lambda a: a["id"]),
    }


def write(uuid: str, mountpoint: str) -> dict:
    """Write the manifest onto the drive. Best effort; never raises."""
    try:
        data = build(uuid, mountpoint)
        tmp = manifest_path(mountpoint) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, manifest_path(mountpoint))   # atomic: a torn manifest
        return data                                   # would describe nothing
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200], "apps": []}


def read(mountpoint: str) -> dict | None:
    """The manifest on a drive, or None if it carries none."""
    try:
        with open(manifest_path(mountpoint)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("apps") is not None else None
    except Exception:  # noqa: BLE001
        return None


def host_arch() -> str:
    """This machine's Docker architecture, in the same vocabulary as an image."""
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Arch}}"],
                           capture_output=True, text=True, timeout=15)
        a = (r.stdout or "").strip()
        if a:
            return a
    except Exception:  # noqa: BLE001
        pass
    import platform
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())


def runnable_here(entry: dict, this_arch: str | None = None) -> dict:
    """Whether this machine can execute the app, and why not if it cannot.

    Missing architecture is treated as runnable rather than blocked: an older
    manifest predates the field, and refusing to run an app because we failed to
    record something about it would be the tool getting in the way.
    """
    this_arch = this_arch or host_arch()
    img = (entry.get("arch") or "").strip()
    if not img:
        return {"runnable": True, "reason": ""}
    if img == this_arch:
        return {"runnable": True, "reason": ""}
    return {
        "runnable": False,
        "reason": (f"built for {img}, this machine is {this_arch} — "
                   f"move the drive to a {img} computer to use it"),
    }


def drives_with_apps() -> list[dict]:
    """Every mounted drive on this node carrying an app manifest.

    Includes drives this node did not set up: that is the whole point — a drive
    moved here from elsewhere should announce what it brought.
    """
    from . import usb_storage
    out = []
    arch = host_arch()
    try:
        parts = usb_storage.usb_partitions()
    except Exception:  # noqa: BLE001
        return out
    for p in parts:
        mnt = p.get("mountpoint")
        if not mnt or not os.path.ismount(mnt):
            continue
        man = read(mnt)
        if not man:
            continue
        apps = []
        for a in man.get("apps") or []:
            apps.append({**a, **runnable_here(a, arch)})
        out.append({
            "uuid": p.get("uuid"), "label": p.get("label"), "mountpoint": mnt,
            "written_by": man.get("written_by"), "written_at": man.get("written_at"),
            # True when the drive came from somewhere else — the case the Hub
            # should surface rather than treat as routine.
            "foreign": man.get("uuid") != p.get("uuid") or False,
            "apps": apps,
            "runnable_count": sum(1 for a in apps if a["runnable"]),
        })
    return out
