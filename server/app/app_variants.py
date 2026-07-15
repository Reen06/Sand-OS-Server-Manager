"""App version manager — install/uninstall/switch between an app's declared
`AppVariant`s (see models.py), and free disk space by removing ones you're
not using. Generic: any AppDef that declares `variants` gets this for free;
apps with none (the vast majority) are untouched.

Design:
  - "installed"  = the variant's image_tag exists locally (`docker image
    inspect`). Never implied by the catalog — the catalog just lists what
    COULD be installed.
  - "active"     = the variant docker_backend.spawn() will actually launch.
    Persisted in a small local JSON state file (per SM node — versions are a
    node-local disk/software choice, not a fleet-wide or per-user setting).
  - install()    = docker build (from build_context) or docker pull, run in a
    background thread so the endpoint returns immediately; progress is
    polled via status(). A resolver (e.g. "freecad-weekly") computes
    build_args dynamically right before building, for channels that roll
    forward (no fixed URL to hardcode).
  - uninstall()  = docker rmi the tag — refused if it's the active variant
    (switch first) or if a running container is using that image (stop it
    first). Per-user app SETTINGS (NFS .appdata) are untouched; this only
    affects which image bytes sit on this node's disk.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request

from . import config
from .models import AppDef, AppVariant

_STATE_FILE = os.path.join(config.NAS_ROOT, ".app-variants-state.json")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_lock = threading.Lock()
# app_id -> {"variant_id": str, "log": [str], "started_at": float, "done": bool, "error": str|None}
_jobs: dict[str, dict] = {}


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


def _variant(app: AppDef, variant_id: str) -> AppVariant:
    for v in app.variants:
        if v.id == variant_id:
            return v
    raise KeyError(f"no such variant {variant_id!r} for {app.id}")


def _host_args(host: str | None) -> list[str]:
    return ["-H", host] if host else []


def _docker_image_exists(tag: str, host: str | None = None) -> bool:
    r = subprocess.run(["docker", *_host_args(host), "image", "inspect", tag],
                       capture_output=True, timeout=10)
    return r.returncode == 0


def _docker_image_size(tag: str, host: str | None = None) -> int | None:
    r = subprocess.run(["docker", *_host_args(host), "image", "inspect", tag, "--format", "{{.Size}}"],
                       capture_output=True, text=True, timeout=10)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _image_in_use(tag: str, host: str | None = None) -> bool:
    """True if any container (running or stopped) still references this image."""
    r = subprocess.run(["docker", *_host_args(host), "ps", "-a", "--filter", f"ancestor={tag}", "-q"],
                       capture_output=True, text=True, timeout=10)
    return bool(r.stdout.strip())


def _host_for(app: AppDef) -> str | None:
    from . import app_images
    return app_images.active_docker_host(app.id)


# ── dynamic resolvers: fill in build_args right before an install ──────────────

def _resolve_freecad_weekly(variant: AppVariant) -> dict[str, str]:
    """Latest FreeCAD weekly dev build's Linux x86_64 AppImage asset URL."""
    req = urllib.request.Request(
        "https://api.github.com/repos/FreeCAD/FreeCAD/releases",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "SandOS-SM"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        releases = json.loads(resp.read())
    weekly = next((r for r in releases if r.get("tag_name", "").startswith("weekly-")), None)
    if weekly is None:
        raise RuntimeError("no weekly release found on FreeCAD/FreeCAD")
    asset = next(
        (a for a in weekly["assets"]
         if re.search(r"Linux-x86_64\.AppImage$", a["name"])),
        None)
    if asset is None:
        raise RuntimeError(f"no Linux x86_64 AppImage asset on {weekly['tag_name']}")
    return {"FREECAD_APPIMAGE_URL": asset["browser_download_url"],
            "_resolved_tag": weekly["tag_name"]}


_RESOLVERS = {"freecad-weekly": _resolve_freecad_weekly}


# ── catalog / status ─────────────────────────────────────────────────────────

def list_variants(app: AppDef, show_dev: bool = False) -> dict:
    if not app.variants:
        return {"supported": False, "variants": []}
    state = _load_state()
    active_id = state.get(app.id, {}).get("variant_id") or _default_active(app)
    job = _jobs.get(app.id)
    host = _host_for(app)

    out = []
    for v in app.variants:
        if v.channel == "dev" and not show_dev:
            continue
        installed = _docker_image_exists(v.image_tag, host)
        out.append({
            "id": v.id, "label": v.label, "channel": v.channel,
            "installed": installed,
            "active": v.id == active_id,
            "size": _docker_image_size(v.image_tag, host) if installed else None,
        })
    return {
        "supported": True,
        "variants": out,
        "installing": (
            {"variant_id": job["variant_id"], "done": job["done"],
             "error": job["error"], "log_tail": job["log"][-15:]}
            if job else None
        ),
    }


def _default_active(app: AppDef) -> str:
    stable = next((v for v in app.variants if v.channel == "stable"), app.variants[0])
    return stable.id


def active_image(app: AppDef) -> str:
    """What docker_backend.spawn() should actually run. Falls back to
    `app.image` when variants are unused/undeclared, or the selected variant
    somehow isn't installed (never launch a tag that isn't there). Checks the
    app's ACTUAL daemon (local, or a USB drive if the image was relocated) —
    checking the wrong one would wrongly report "not installed"."""
    if not app.variants:
        return app.image
    state = _load_state()
    variant_id = state.get(app.id, {}).get("variant_id") or _default_active(app)
    try:
        v = _variant(app, variant_id)
    except KeyError:
        return app.image
    return v.image_tag if _docker_image_exists(v.image_tag, _host_for(app)) else app.image


# ── actions ──────────────────────────────────────────────────────────────────

def select(app: AppDef, variant_id: str) -> dict:
    v = _variant(app, variant_id)
    if not _docker_image_exists(v.image_tag, _host_for(app)):
        raise ValueError(f"{v.label} isn't installed yet — install it first")
    state = _load_state()
    state[app.id] = {"variant_id": variant_id}
    _save_state(state)
    return {"active": variant_id}


def uninstall(app: AppDef, variant_id: str) -> dict:
    v = _variant(app, variant_id)
    host = _host_for(app)
    state = _load_state()
    if state.get(app.id, {}).get("variant_id", _default_active(app)) == variant_id:
        raise ValueError("can't uninstall the active version — switch to another first")
    if not _docker_image_exists(v.image_tag, host):
        return {"removed": False, "reason": "not installed"}
    if _image_in_use(v.image_tag, host):
        raise ValueError("an instance is using this version — stop it first")
    r = subprocess.run(["docker", *_host_args(host), "rmi", v.image_tag],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return {"removed": True}


def install_status(app_id: str) -> dict | None:
    job = _jobs.get(app_id)
    if not job:
        return None
    return {"variant_id": job["variant_id"], "done": job["done"],
            "error": job["error"], "log_tail": job["log"][-30:]}


def install(app: AppDef, variant_id: str) -> dict:
    """Kick off install in a background thread; returns immediately."""
    v = _variant(app, variant_id)
    with _lock:
        existing = _jobs.get(app.id)
        if existing and not existing["done"]:
            raise ValueError(f"{app.id} already has an install in progress")
        job = {"variant_id": variant_id, "log": [], "started_at": time.time(),
               "done": False, "error": None}
        _jobs[app.id] = job

    threading.Thread(target=_run_install, args=(app, v, job), daemon=True,
                     name=f"install-{app.id}-{variant_id}").start()
    return {"ok": True, "status": "installing"}


def _run_install(app: AppDef, v: AppVariant, job: dict) -> None:
    # Note: builds/pulls always target the app's CURRENT daemon (local, or a
    # USB drive if the image already lives there) — installing a new variant
    # of an already-relocated app keeps it on that same drive.
    host = _host_for(app)
    try:
        build_args = dict(v.build_args)
        if v.resolver:
            resolved = _RESOLVERS[v.resolver](v)
            job["log"].append(f"resolved: {resolved.get('_resolved_tag', '')}".strip())
            build_args.update({k: val for k, val in resolved.items() if not k.startswith("_")})

        env = None
        if v.kind == "pull":
            cmd = ["docker", *_host_args(host), "pull", v.source or v.image_tag]
        else:
            context = os.path.join(_REPO_ROOT, app.build_context)
            cmd = ["docker", *_host_args(host), "build", "-t", v.image_tag]
            for k, val in build_args.items():
                cmd += ["--build-arg", f"{k}={val}"]
            cmd.append(context)
            if host:
                # `docker build` is aliased to `docker buildx build`, and
                # buildx's default builder targets its OWN docker CONTEXT —
                # it silently ignores -H, so a "build directly on the USB
                # drive" ends up on local disk instead (confirmed live: an
                # OpenFOAM GUI image built with -H <usb-socket> landed on the
                # default daemon regardless). DOCKER_BUILDKIT=0 forces the
                # legacy builder, which does respect -H correctly. Only
                # needed for `build`, not `pull` — plain pulls always target
                # -H correctly regardless of buildx.
                env = {**os.environ, "DOCKER_BUILDKIT": "0"}

        job["log"].append("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env)
        for line in proc.stdout:
            job["log"].append(line.rstrip())
            job["log"][:] = job["log"][-500:]  # bounded
        proc.wait(timeout=1800)

        if v.kind == "pull" and v.source and v.source != v.image_tag:
            subprocess.run(["docker", *_host_args(host), "tag", v.source, v.image_tag], timeout=15)

        if proc.returncode != 0:
            job["error"] = f"exit code {proc.returncode}"
        elif not _docker_image_exists(v.image_tag, host):
            job["error"] = "build finished but the image tag wasn't produced"
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
    finally:
        job["done"] = True
