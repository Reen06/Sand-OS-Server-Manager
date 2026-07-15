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
import threading
import uuid as _uuid_mod

from . import config, registry, usb_storage
from .models import AppDef

# ── Background image-move jobs ────────────────────────────────────────────────
_img_jobs: dict[str, dict] = {}
_img_jobs_lock = threading.Lock()


def _new_img_job(app_id: str, action: str) -> tuple[str, dict]:
    job_id = _uuid_mod.uuid4().hex
    job: dict = {"job_id": job_id, "app_id": app_id, "action": action,
                 "done": False, "ok": False, "error": None,
                 "size_bytes": None, "mode": None,
                 "bytes_copied": 0, "total_bytes": None}
    with _img_jobs_lock:
        _img_jobs[job_id] = job
        done_ids = [k for k, v in _img_jobs.items() if v["done"]]
        for k in done_ids[:-200]:
            del _img_jobs[k]
    return job_id, job


def img_job_status(job_id: str) -> dict | None:
    return _img_jobs.get(job_id)

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


def set_initial_location(app_id: str, usb_uuid: str) -> None:
    """Register a BRAND-NEW app as already living on a USB drive from its very
    first build — for an app built directly against the drive's secondary
    dockerd (`docker -H <usb-socket> build/pull ...`) and never touching local
    disk at all. Without this, `location()` defaults to "local" the instant
    the app appears in the catalogue (no state entry yet), even though the
    image genuinely only exists on the drive. One-off: run manually right
    after the first successful build/pull, not exposed as an endpoint."""
    state = _load_state()
    state[app_id] = {"mode": "usb", "usb_uuid": usb_uuid}
    _save_state(state)


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
    """Real byte size of the image — the number that actually matters for
    both "how much space will this free up" and "how many bytes will a
    save|load transfer move."

    An earlier version of this function deliberately preferred `docker image
    ls`'s human "Size" column over `docker image inspect`'s `.Size` field,
    on the theory that `.Size` under-counts (only unique, non-shared layer
    bytes). That theory was wrong, discovered live: `ls`'s "Size" reported
    17GB for FreeCAD's image, but the ACTUAL save|load transfer only ever
    moved ~4.36GB — matching `docker image inspect --format '{{.Size}}'`
    almost exactly (differs by <0.001%). `ls`'s "Size" reflects the newer
    Docker CLI's inflated "disk usage" concept (shared-base layers counted
    as if unique), not real transferable/reclaimable bytes. `.Size` is the
    correct source for both a progress bar's total_bytes AND the "frees up
    X" figure shown before a move."""
    args = (["docker"] + (["-H", host] if host else [])
            + ["image", "inspect", tag, "--format", "{{.Size}}"])
    r = subprocess.run(args, capture_output=True, text=True, timeout=15)
    try:
        return int(r.stdout.strip())
    except (ValueError, AttributeError):
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


def _piped_transfer(src_cmd: list[str], dst_cmd: list[str], job: dict | None,
                    timeout: int = 1800) -> None:
    """Stream `docker image save` straight into `docker ... image load` with no
    intermediate tarball (so a move never transiently needs double the local
    disk) — but read/write it ourselves in chunks, rather than wiring the OS
    pipe directly between the two processes, so `job["bytes_copied"]` can be
    updated live as bytes actually cross the wire. This is what lets the UI
    show a real x/x GB + % instead of just an indeterminate spinner.

    `load`'s combined stdout/stderr is drained on a background thread WHILE we
    write — reading it only after the transfer loop (e.g. via communicate())
    would deadlock if `load` fills its output pipe's OS buffer before we get
    to it, since it'd then block on writing output, which blocks it reading
    more stdin, which blocks us writing more of `save`'s output."""
    save = subprocess.Popen(src_cmd, stdout=subprocess.PIPE)
    load = subprocess.Popen(dst_cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out_chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: out_chunks.extend(iter(lambda: load.stdout.read(4096), b"")),
                              daemon=True)
    reader.start()
    copied = 0
    try:
        while True:
            chunk = save.stdout.read(1024 * 1024)
            if not chunk:
                break
            load.stdin.write(chunk)
            copied += len(chunk)
            if job is not None:
                job["bytes_copied"] = copied
    finally:
        save.stdout.close()
        try:
            load.stdin.close()
        except BrokenPipeError:
            pass
    save.wait(timeout=timeout)
    load.wait(timeout=timeout)
    reader.join(timeout=5)
    if save.returncode != 0 or load.returncode != 0:
        raise RuntimeError(b"".join(out_chunks).decode(errors="replace").strip() or "image transfer failed")


def _prune_docker(host: str | None = None) -> None:
    """Best-effort reclaim of dangling image layers + stale build cache after
    an image is added/removed on a docker store. Never raises — a prune
    failure should never fail the move/mirror/clone that triggered it.

    Why this is needed at all: docker image ls's "Size" for a typical app is
    mostly SHARED base layers (Ubuntu, CUDA, Node…), so removing one app's
    tag only frees the bytes unique to it — any base layer that becomes
    orphaned once nothing local references it anymore just sits there as
    "reclaimable" (confirmed live: 6+ GB of it) until something prunes it.
    `docker image prune` only ever removes genuinely untagged/dangling
    images — never anything a live tag still points to."""
    for args in (["image", "prune", "-f"], ["builder", "prune", "-f"]):
        try:
            cmd = ["docker"] + (["-H", host] if host else []) + args
            subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception:  # noqa: BLE001
            pass


def move_to_usb(app_id: str, usb_uuid: str, keep_local: bool, job: dict | None = None) -> dict:
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

    if job is not None:
        job["total_bytes"] = _image_size(tag, None)

    _piped_transfer(["docker", "image", "save", tag],
                    ["docker", "-H", host, "image", "load"], job)
    if not _image_exists(tag, host):
        raise RuntimeError("image transfer reported success but the image isn't on the drive")

    write_manifest(app, mountpoint)

    state = _load_state()
    state[app_id] = {"mode": "usb", "usb_uuid": usb_uuid}
    _save_state(state)

    if not keep_local:
        subprocess.run(["docker", "rmi", tag], capture_output=True, text=True, timeout=60)

    _prune_docker(None)   # local — reclaim whatever the rmi above just orphaned
    _prune_docker(host)   # the USB store too, for completeness

    return {"ok": True, "mode": "usb", "usb_uuid": usb_uuid, "kept_local": keep_local,
            "size_bytes": _image_size(tag, host)}


def move_to_local(app_id: str, delete_usb_copy: bool = False, job: dict | None = None) -> dict:
    """Copy the image back to local disk. Mirrors move_to_usb's own Move vs
    Mirror split, just in the opposite direction:
      delete_usb_copy=True   "Move to local"   — frees the USB drive; the
                                                  USB copy is removed once
                                                  the local copy is verified.
      delete_usb_copy=False  "Clone to local"  — keeps BOTH copies (the USB
                                                  one stays the active one
                                                  from other nodes' point of
                                                  view, this is just a spare)."""
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
    if job is not None:
        job["total_bytes"] = _image_size(tag, host)

    _piped_transfer(["docker", "-H", host, "image", "save", tag],
                    ["docker", "image", "load"], job)
    if not _image_exists(tag, None):
        raise RuntimeError("image transfer reported success but the image isn't local")

    state = _load_state()
    if delete_usb_copy:
        r = subprocess.run(["docker", "-H", host, "rmi", tag],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "couldn't remove the USB copy")
        state[app_id] = {"mode": "local"}
    else:
        # Keep the drive's uuid around (as "local" now, not "usb") so a later
        # remove_usb_copy() knows which drive's leftover copy to clean up —
        # popping the entry entirely would lose that.
        state[app_id] = {"mode": "local", "last_usb_uuid": loc["usb_uuid"]}
    _save_state(state)

    _prune_docker(host)   # the USB store, if the copy there was just removed
    _prune_docker(None)   # local — the freshly-loaded image can orphan old layers too

    return {"ok": True, "mode": "local", "size_bytes": _image_size(tag, None)}


def start_move_to_usb(app_id: str, usb_uuid: str, keep_local: bool) -> str:
    """Validate eagerly, then copy the image in a background thread. Returns job_id."""
    app = registry.APPS.get(app_id)
    if app is None:
        raise KeyError(app_id)
    if _instance_running_anywhere(app_id):
        raise ValueError(f"stop every running instance of {app.label} before moving its image")
    if not usb_storage.mountpoint_for(usb_uuid):
        raise RuntimeError("that USB drive isn't plugged in right now")
    tag = _image_tag(app)
    if not _image_exists(tag, None):
        raise RuntimeError(f"{app.label}'s image isn't installed locally — nothing to move")

    action = "mirror" if keep_local else "move"
    job_id, job = _new_img_job(app_id, action)
    t = threading.Thread(target=_run_move_to_usb,
                         args=(job, app_id, usb_uuid, keep_local),
                         daemon=True, name=f"img-move-{job_id[:8]}")
    t.start()
    return job_id


def _run_move_to_usb(job: dict, app_id: str, usb_uuid: str, keep_local: bool) -> None:
    try:
        result = move_to_usb(app_id, usb_uuid, keep_local, job=job)
        job["mode"] = result.get("mode")
        job["size_bytes"] = result.get("size_bytes")
        job["ok"] = True
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
        job["ok"] = False
    finally:
        job["done"] = True


def start_move_to_local(app_id: str, delete_usb_copy: bool = False) -> str:
    """Validate eagerly, then copy back to local in a background thread. Returns job_id."""
    app = registry.APPS.get(app_id)
    if app is None:
        raise KeyError(app_id)
    if _instance_running_anywhere(app_id):
        raise ValueError(f"stop every running instance of {app.label} before moving its image")
    loc = location(app_id)
    if loc["mode"] != "usb":
        raise ValueError(f"{app.label}'s image is already local")
    if not usb_storage.docker_host_for(loc["usb_uuid"]):
        raise RuntimeError("that USB drive isn't plugged in right now")

    action = "move-to-local" if delete_usb_copy else "clone-to-local"
    job_id, job = _new_img_job(app_id, action)
    t = threading.Thread(target=_run_move_to_local, args=(job, app_id, delete_usb_copy),
                         daemon=True, name=f"img-local-{job_id[:8]}")
    t.start()
    return job_id


def _run_move_to_local(job: dict, app_id: str, delete_usb_copy: bool) -> None:
    try:
        result = move_to_local(app_id, delete_usb_copy, job=job)
        job["mode"] = result.get("mode")
        job["size_bytes"] = result.get("size_bytes")
        job["ok"] = True
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
        job["ok"] = False
    finally:
        job["done"] = True


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
    _prune_docker(host)
    return {"ok": True, "removed": tag, "usb_uuid": usb_uuid}


def _image_in_use_anywhere(app_id: str) -> bool:
    """Stricter than _instance_running_anywhere: True if ANY container —
    running OR merely stopped-but-present — on any active daemon still
    references this app's image tag. Mirrors app_variants._image_in_use's
    exact check (docker ps -a --filter ancestor=<tag>), generalized across
    every daemon this app's image could be on, the same way
    _instance_running_anywhere already generalizes the running-only check."""
    from . import docker_backend
    app = registry.APPS.get(app_id)
    if not app:
        return False
    tag = _image_tag(app)
    for host in docker_backend.all_docker_hosts():
        r = subprocess.run(["docker"] + (["-H", host] if host else [])
                           + ["ps", "-a", "--filter", f"ancestor={tag}", "-q"],
                           capture_output=True, text=True, timeout=10)
        if r.stdout.strip():
            return True
    return False


def uninstall_app(app_id: str) -> dict:
    """Delete this app's ACTIVE image — the intentional exception to
    move_to_usb()/remove_usb_copy()'s refusal to ever touch the active copy;
    uninstalling IS deleting the active copy, that's the whole point.

    Never touches a `binds` app's host source-tree directory (e.g. WebCAD's
    /home/control/webcadcam) — the image and the source tree are entirely
    separate concerns. Rebuilding after this still needs that tree to
    already exist; this function only ever runs `docker rmi`, nothing else."""
    app = registry.APPS.get(app_id)
    if app is None:
        raise KeyError(app_id)
    if _instance_running_anywhere(app_id):
        raise ValueError(f"stop every running instance of {app.label} before uninstalling its image")
    if _image_in_use_anywhere(app_id):
        raise ValueError(f"a stopped container still references {app.label}'s image — remove it first")

    host = active_docker_host(app_id)
    tag = _image_tag(app)
    if not _image_exists(tag, host):
        return {"ok": True, "removed": False, "reason": "not installed"}

    r = subprocess.run(["docker"] + (["-H", host] if host else []) + ["rmi", tag],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())

    _prune_docker(host)   # reclaim now-orphaned base layers, same as every other rmi path
    return {"ok": True, "removed": True}