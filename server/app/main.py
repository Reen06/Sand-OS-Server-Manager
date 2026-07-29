"""Sand-OS Server Manager — API + static apps screen.

MVP: single-node orchestration of streamed apps (FreeCAD). Per-user instances,
launch / stop / status, served with a minimal apps screen. Real identity (Hub
SSO) and the auth-gated TLS proxy come in the next phase — for now 'user' is a
cookie so per-user instances are demonstrable on the LAN.
"""
from __future__ import annotations
import getpass
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import app_images, app_storage, app_variants, busy, config, docker_backend, dockerhub_apps, files, glances_svc, hub_auth, metrics, nas, ollama_mgr, pending_imports, proxy, pwa, registry, snapshots, usb_storage

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# server/app/main.py -> repo root, for the fleet-wide auto-update feature (the
# Hub SSHes in and runs `git -C <repo_root> ...` directly — never hardcoded
# Hub-side, since a different node could have a different home dir).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_GIT_SHA_CACHE: tuple[float, str] = (0.0, "")
_GIT_SHA_TTL = 60.0


def _git_sha() -> str:
    """This node's current git HEAD — cached briefly since sm_info() is on a
    hot polling path and a subprocess call per poll would be needless
    overhead (mirrors registry._INSTALLED_CACHE's shape)."""
    global _GIT_SHA_CACHE
    ts, sha = _GIT_SHA_CACHE
    now = time.monotonic()
    if now - ts < _GIT_SHA_TTL:
        return sha
    try:
        r = subprocess.run(["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        sha = r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        sha = ""
    _GIT_SHA_CACHE = (now, sha)
    return sha


app = FastAPI(title="Sand-OS Server Manager")


import logging as _logging
_log = _logging.getLogger("sandos.main")


def _autostart_apps() -> None:
    """Launch the configured always-on apps (config.AUTOSTART_APPS) in the
    background so a reboot doesn't leave a dead AI stack. Runs after
    reconcile_from_docker so anything already running is skipped, not relaunched.
    Shared apps map to the _shared key inside registry.launch."""
    apps = config.AUTOSTART_APPS
    if not apps:
        return

    def _run() -> None:
        for app_id in apps:
            if app_id not in registry.APPS:
                _log.warning("autostart: unknown app %r — skipping", app_id)
                continue
            try:
                # Already running (adopted by reconcile)? Don't relaunch.
                if registry.status(app_id, "_shared") in ("idle", "active"):
                    _log.info("autostart: %s already running", app_id)
                    continue
                registry.launch(app_id, "_shared")
                _log.info("autostart: launched %s", app_id)
            except Exception as e:  # noqa: BLE001
                _log.warning("autostart: %s failed: %s", app_id, e)
            time.sleep(3)   # stagger so several heavy containers don't start at once

    threading.Thread(target=_run, daemon=True, name="sm-autostart").start()


# How often the NAS host re-pulls its export policy from the Hub. Fifteen
# minutes bounds the window in which a missed push (node down, tunnel flapping,
# Hub restarted mid-push) leaves a stale export live. Overridable so the loop
# can be exercised in seconds rather than by waiting out a real interval.
_NAS_RESYNC_INTERVAL = int(os.environ.get("SM_NAS_RESYNC_INTERVAL") or 900)

@app.on_event("startup")
def _startup() -> None:
    usb_storage.start_poller()   # auto-mount marked USB drives
    registry.reconcile_from_docker()
    glances_svc.start()   # local Glances REST server for the Fleet monitor panel
    _autostart_apps()            # bring the always-on AI stack up after a reboot
    _resync_nas_policy_if_host() # re-read export policy from the Hub after a reboot


def _resync_nas_policy_if_host() -> None:
    """Re-apply the NAS export policy at startup, if this node hosts the NAS.

    The NFS container is `--restart unless-stopped`, so after a reboot it comes
    back with whatever exports were baked into its `docker run` — i.e. whatever
    the policy was the last time run-nas.sh executed, NOT what the Hub says now.
    Trust changed while this box was down would silently not be in effect, which
    for a revocation is the wrong way to fail.

    Deliberately quiet and non-fatal: a node that does not host the NAS, or a
    Hub that is unreachable at boot, must not stop the Server Manager starting.
    """
    script = _REPO_ROOT / "containers" / "nfs-server" / "sync-nas-policy.sh"
    if not script.exists():
        return
    if not config.NAS_ENABLED or config.NAS_HOST != config.LAN_IP:
        return                      # not the NAS host — nothing to apply
    def _once() -> bool:
        try:
            r = subprocess.run(["bash", str(script)], capture_output=True,
                               text=True, timeout=120)
            if r.returncode == 0:
                return True
            print(f"[nas] policy resync skipped: "
                  f"{(r.stderr or r.stdout or '').strip()[:200]}")
        except Exception as e:      # noqa: BLE001
            print(f"[nas] policy resync failed: {e}")
        return False

    def _run() -> None:
        """Apply at boot, then keep re-applying on a slow timer.

        The Hub pushes on every trust change, so this is the backstop for the
        pushes that never arrive: this node down at the moment of the change, a
        tunnel that was flapping, a Hub restart mid-push. Without it a revocation
        can sit unapplied indefinitely and nothing anywhere reports a problem —
        the exports simply stay as they were, which for a revocation is the
        dangerous direction to fail in.

        Interval is deliberately long. The push path is what makes changes
        prompt; this only bounds how long a *missed* push can persist, and
        polling the Hub harder would cost every node a request for something
        that is almost always a no-op.
        """
        first = True
        while True:
            if not first:
                time.sleep(_NAS_RESYNC_INTERVAL)
            if _once() and first:
                print("[nas] export policy re-applied from the Hub")
            first = False

    # Off the startup path: the Hub may not be reachable the instant this boots,
    # and blocking here would delay every other service this node provides.
    threading.Thread(target=_run, daemon=True).start()


@app.on_event("shutdown")
def _shutdown() -> None:
    glances_svc.stop()


def _require_identity(request: Request) -> dict:
    """The authenticated identity {username, role, grants}. Hub-SSO mode: from the
    Hub (else 401 → login). Dev mode (no SM_HUB_URL): an anonymous full-access
    per-browser cookie user."""
    if hub_auth.enabled():
        ident = hub_auth.verify_identity(request.cookies.get(config.HUB_SESSION_COOKIE, ""))
        if not ident:
            raise HTTPException(401, detail={"error": "login required",
                                             "login_url": config.HUB_LOGIN_URL})
        return ident
    return {"username": request.cookies.get("sm_user") or "me", "role": "admin", "grants": []}


def _require_user(request: Request) -> str:
    return _require_identity(request)["username"]


def _app_allowed(identity: dict, app_id: str) -> bool:
    """Scoped (shared-person) accounts may only touch apps they were granted
    (`app.<id>` in grants). admin/viewer (and dev-mode) reach every app."""
    if identity.get("role") == "scoped":
        return f"app.{app_id}" in (identity.get("grants") or [])
    return True


def _require_app(request: Request, app_id: str) -> dict:
    ident = _require_identity(request)
    if not _app_allowed(ident, app_id):
        raise HTTPException(403, detail={"error": "you do not have access to this app"})
    return ident


def _require_admin(request: Request) -> dict:
    """Fleet/NAS administration is owner-only (dev-mode is admin)."""
    ident = _require_identity(request)
    if ident.get("role") != "admin":
        raise HTTPException(403, detail={"error": "admin only"})
    return ident


def _is_loopback(request: Request) -> bool:
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1")


def _require_admin_or_local(request: Request) -> None:
    """Busy mode's local control path: the owner's own machine (the Windows/
    WSL launcher GUI talking to localhost — WSL2's automatic port-forwarding
    makes this "just work") never needs a Hub login, since it's already
    physically the same machine. Anything else falls back to a real admin
    session, same as every other Fleet action."""
    if _is_loopback(request):
        return
    _require_admin(request)


# ── Fleet NAS: shared-folder management (admin-only) ──────────────────────────
@app.get("/api/nas/usb")
def usb_list(request: Request):
    _require_admin(request)
    return {"ok": True, "devices": usb_storage.list_devices()}


class _UsbAssignBody(BaseModel):
    uuid: str
    target: str   # 'shared' | 'user:<name>'


@app.post("/api/nas/usb/assign")
def usb_assign(request: Request, body: _UsbAssignBody):
    _require_admin(request)
    try:
        return {"ok": True, **usb_storage.assign(body.uuid, body.target)}
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/nas/usb/forget")
def usb_forget(request: Request, body: _UsbAssignBody):
    """Remove the drive's SandOS metadata (marker + server memory). Data kept."""
    _require_admin(request)
    return {"ok": True, **usb_storage.forget(body.uuid)}


class _UsbFormatBody(BaseModel):
    uuid: str
    fs: str = "vfat"
    confirm: bool = False


@app.post("/api/nas/usb/format")
def usb_format(request: Request, body: _UsbFormatBody):
    """FULL ERASE of the partition. Requires confirm=true."""
    _require_admin(request)
    if not body.confirm:
        return JSONResponse({"ok": False, "error": "formatting erases everything; confirm=true required"},
                            status_code=428)
    try:
        return {"ok": True, **usb_storage.format_drive(body.uuid, body.fs)}
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/nas/usb/eject")
def usb_eject(request: Request, body: _UsbAssignBody):
    _require_admin(request)
    try:
        return {"ok": True, **usb_storage.eject(body.uuid)}
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)


@app.get("/api/nas/usb/disks")
def usb_disks(request: Request):
    """Hotplug/USB DISKS, including totally blank ones with no partition
    table — the provisioning wizard's "detected but unformatted drive" cards.
    (GET /api/nas/usb only lists existing partitions, which a blank drive
    doesn't have.)"""
    _require_admin(request)
    return {"ok": True, "disks": usb_storage.usb_disks()}


class _UsbProvisionBody(BaseModel):
    disk: str
    mode: str            # "single" | "split"
    confirm: bool = False
    fstype: str = "exfat"
    label: str = "SANDOS"
    app_gib: int = 20
    app_label: str = "SANDOS-APPS"
    media_fstype: str = "exfat"
    media_label: str = "SANDOS"


@app.post("/api/nas/usb/provision")
def usb_provision(request: Request, body: _UsbProvisionBody):
    """Wipe + repartition an ENTIRE physical drive (not one partition — see
    /format for that) into one or two partitions, format them, and auto-
    assign the result. Requires confirm=true — this erases everything on
    the whole disk, every existing partition. Runs as a background job;
    poll /api/nas/usb/provision/status?disk=..."""
    _require_admin(request)
    if not body.confirm:
        return JSONResponse(
            {"ok": False, "error": "provisioning erases the ENTIRE drive; confirm=true required"},
            status_code=428)
    try:
        if body.mode == "single":
            return usb_storage.provision_drive(body.disk, "single",
                                               fstype=body.fstype, label=body.label)
        return usb_storage.provision_drive(
            body.disk, "split", app_gib=body.app_gib, app_label=body.app_label,
            media_fstype=body.media_fstype, media_label=body.media_label)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/nas/usb/provision/status")
def usb_provision_status(request: Request, disk: str):
    _require_admin(request)
    status = usb_storage.provision_status(disk)
    if status is None:
        return JSONResponse({"ok": False, "error": "no provision job for that disk"}, status_code=404)
    return {"ok": True, **status}


class _UsbAppHostingBody(BaseModel):
    uuid: str
    enabled: bool


@app.get("/api/nas/usb/app-hosting/setup-status")
def usb_app_hosting_setup_status(request: Request):
    """Live check of the one-time root setup (helper script, systemd unit,
    sudoers grant) — lets the Fleet page show exactly what's missing (and
    the one command to fix it) instead of a generic warning."""
    _require_admin(request)
    return {"ok": True, **usb_storage.dockerd_setup_status()}


@app.post("/api/nas/usb/app-hosting")
def usb_app_hosting(request: Request, body: _UsbAppHostingBody):
    """Toggle whether this assigned drive runs a secondary Docker daemon, so
    an app's IMAGE (not just its data) can be relocated onto it."""
    _require_admin(request)
    try:
        return {"ok": True, **usb_storage.set_app_hosting(body.uuid, body.enabled)}
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/nas/shared")
def nas_shared_list(request: Request):
    _require_admin(request)
    return {"enabled": config.NAS_ENABLED, "node": config.NODE_NAME,
            "folders": nas.list_shared(), "users": nas.list_users()}


@app.post("/api/nas/shared")
async def nas_shared_create(request: Request):
    _require_admin(request)
    body = await request.json()
    try:
        return nas.create_shared(body.get("name", ""), body.get("members") or [])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.patch("/api/nas/shared/{name}")
async def nas_shared_update(name: str, request: Request):
    _require_admin(request)
    body = await request.json()
    try:
        return nas.set_members(name, body.get("members") or [])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.delete("/api/nas/shared/{name}")
def nas_shared_delete(name: str, request: Request):
    _require_admin(request)
    delete_files = request.query_params.get("delete_files") in ("1", "true", "yes")
    try:
        return nas.delete_shared(name, delete_files)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ── Cloud file picker: save/open into a user's NAS home or an accessible shared
# folder. Apps with no OS-level file dialog (Ray Optics) use this instead —
# saving into a shared folder is how a scene "auto appears" to everyone already
# sharing it (see files.py / nas.py's shared-folder membership) ───────────────
@app.get("/api/files/roots")
def files_roots(request: Request):
    ident = _require_identity(request)
    return {"roots": files.list_roots(ident["username"])}


@app.get("/api/files/list")
def files_list(request: Request, root: str, path: str = ""):
    ident = _require_identity(request)
    try:
        return {"entries": files.list_dir(root, ident["username"], path)}
    except (ValueError, FileNotFoundError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/files/read")
async def files_read(request: Request, root: str, path: str):
    ident = _require_identity(request)
    try:
        data = files.read_file(root, ident["username"], path)
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return Response(content=data, media_type="application/octet-stream")


@app.put("/api/files/write")
async def files_write(request: Request, root: str, path: str):
    ident = _require_identity(request)
    body = await request.body()
    try:
        files.write_file(root, ident["username"], path, body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/api/files/mkdir")
async def files_mkdir(request: Request, root: str, path: str):
    ident = _require_identity(request)
    try:
        files.make_dir(root, ident["username"], path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.get("/api/files/exists")
def files_exists(request: Request, root: str, path: str):
    """Whether `path` already exists — the picker's Save flow calls this
    before writing so it can warn on an overwrite instead of silently
    replacing another file."""
    ident = _require_identity(request)
    try:
        return files.exists(root, ident["username"], path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


class RenameBody(BaseModel):
    new_name: str


@app.post("/api/files/rename")
async def files_rename(request: Request, root: str, path: str, body: RenameBody):
    ident = _require_identity(request)
    try:
        files.rename(root, ident["username"], path, body.new_name)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.delete("/api/files/delete")
def files_delete(request: Request, root: str, path: str):
    ident = _require_identity(request)
    try:
        files.delete(root, ident["username"], path)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.middleware("http")
async def ensure_user_cookie(request: Request, call_next):
    response = await call_next(request)
    if not request.cookies.get("sm_user"):
        response.set_cookie("sm_user", uuid.uuid4().hex[:12], max_age=60 * 60 * 24 * 365,
                            httponly=True, samesite="lax")
    return response


@app.get("/api/sm/info")
def sm_info():
    """Node identity + capabilities (UNAUTHENTICATED — no user data). The Hub
    probes this during LAN discovery / add-by-IP to find Server Manager nodes and
    learn what each can run (GPU nodes for streamed apps, the app catalogue, free
    slots), so it can offer per-app placement."""
    return {
        "sm": True,
        "version": config.SM_VERSION,
        # Fleet-wide auto-update: lets the Hub tell "this node is behind
        # origin/main" apart from a hand-bumped SM_VERSION string, and know
        # exactly where to `git pull` on this node without ever hardcoding
        # a path Hub-side (a different node can have a different home dir).
        "git_sha": _git_sha(),
        "repo_root": str(_REPO_ROOT),
        # Busy mode: the Hub's Fleet tab mirrors these for the greyed-out
        # card + override button, but this node's own local state (busy.py)
        # is the real source of truth, not the Hub.
        "busy": busy.is_busy(),
        "busy_override_allowed": busy.override_allowed(),
        # Lets a node's own local tooling (the Windows/WSL launcher's DNS-
        # hairpin fix) find the Hub's configured URL without re-asking the
        # owner something install.sh already collected. Same exposure level
        # as the rest of this unauthenticated endpoint — a public domain.
        "hub_url": config.HUB_URL,
        "node_name": config.NODE_NAME,
        "lan_ip": config.LAN_IP,
        "port": config.SM_PORT,
        "gpu": config.HAS_GPU,
        "slots_total": config.SLOT_COUNT,
        # Lets the Hub discover which node is the fleet's real shared NAS
        # (nas_host == this node's own lan_ip means it's self-hosting the
        # NFS export, not just pointing at one) and hand that address to
        # every future install.sh run as the default, instead of each new
        # node silently defaulting to itself and mounting nothing.
        "nas_enabled": config.NAS_ENABLED,
        "nas_host": config.NAS_HOST,
        "metrics": metrics.collect(),
        "apps": [
            {"id": a.id, "label": a.label, "kind": a.kind, "mode": a.mode,
             "gpu": a.gpu, "icon": a.icon, "color": a.color, "desc": a.desc,
             "image_installed": registry.image_installed(a),
             # Lets the Hub's proxy know this app's own manifest.json/static/*
             # (icons, favicons) are safe to serve WITHOUT the hub_session
             # cookie — browsers fetch a PWA's install assets unauthenticated
             # by design, so gating them 401s the icon fetch during "Add to
             # Home Screen" and the OS falls back to a generic/blank icon.
             "native_pwa": a.native_pwa,
             # Everything a peer-install flow (Hub-brokered) needs to know
             # about this node's copy of the app, so it can be offered as a
             # transfer source to a fresh node that doesn't have it yet.
             "image_tag": app_images._image_tag(a),
             "binds": [list(b) for b in a.binds],
             "source_ready": registry.source_tree_ready(a),
             # A node without the live checkout isn't broken if it's running
             # the self-contained packaged build instead (see
             # app_variants.active_image()) — only flag it red when there's
             # neither the checkout NOR a packaged build installed here.
             "packaged_image": a.packaged_image,   # static AppDef fact — same tag fleet-wide
             "dockerhub_repo": a.dockerhub_repo,    # static AppDef fact — where §11's publish flow pushes
             # §11's receiving side: this app was installed FROM Docker Hub
             # on this node (dockerhub_apps.py) rather than being built in.
             "hub_installed": dockerhub_apps.is_hub_app(a.id),
             "hub_repo": dockerhub_apps.hub_repo_of(a.id),
             "hub_generic": dockerhub_apps.is_generic(a.id),   # plain image, unvetted config
             "packaged_image_installed": (
                 bool(a.packaged_image) and
                 app_variants._docker_image_exists(a.packaged_image, app_images.active_docker_host(a.id))
             ),
             "manual_install_hint": registry.manual_install_hint(a),
             # Lets the Hub tell "real rebuild needed if uninstalled" apps
             # from "plain re-pull, cheap to uninstall" apps for the
             # uninstall risk-tiering feature, without string-sniffing
             # manual_install_hint's build_cmd.
             "has_build_context": bool(a.build_context)}
            for a in registry.APPS.values()
        ],
    }


class _SshAuthorizeBody(BaseModel):
    public_key: str


@app.post("/api/sm/ssh/authorize")
def sm_ssh_authorize(request: Request, body: _SshAuthorizeBody):
    """Let the Hub SSH into this node's own OS account — bootstraps off the
    SAME Hub-session trust already used for every other admin action (a
    Hub session cookie forwarded here IS already "the Hub can administer
    this node"), so there's no separate manual key-copying step. Idempotent
    (a re-authorize with the same key is a no-op) and additive only — never
    removes an existing authorized_keys entry.

    Requires this node to have its own sshd already running/enabled (a
    normal Linux default) — nothing here installs or configures sshd itself.
    """
    _require_admin(request)
    key = body.public_key.strip()
    if not key or "\n" in key or not key.split()[0].startswith("ssh-"):
        return JSONResponse({"ok": False, "error": "that doesn't look like a public key"},
                            status_code=400)
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    auth_file = ssh_dir / "authorized_keys"
    existing = auth_file.read_text() if auth_file.exists() else ""
    if key not in existing.splitlines():
        with open(auth_file, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(key + "\n")
        auth_file.chmod(0o600)
    return {"ok": True, "user": getpass.getuser(), "ssh_port": config.SSH_PORT}


@app.get("/api/apps/{app_id}/instance-name")
def sm_instance_name(app_id: str, request: Request):
    """The container/staging identity this node would use for this caller.

    Exists so the Hub can stage files under the SAME name that registry.stop()
    later clears. The Hub cannot derive it: the name depends on whether this
    app's AppDef is `mode="shared"` (no user suffix) and on this node's own
    identifier sanitiser. Guessing it produces a staging directory that nothing
    ever cleans up — files left behind after a job is exactly the standing
    access staging exists to remove.
    """
    ident = _require_identity(request)
    if app_id not in registry.APPS:
        raise HTTPException(404, "unknown app")
    user = ident.get("username", "")
    return {"app_id": app_id, "user": user,
            "instance": registry.instance_name(app_id, user),
            "node": config.NODE_NAME}


class _StageBody(BaseModel):
    node: str
    instance: str
    user: str
    paths: list[str] = []


@app.post("/api/sm/nas/stage")
def sm_nas_stage(body: _StageBody, request: Request):
    """Place specific files into one app instance's staging directory.

    Only meaningful on the NAS host. Paths are resolved inside the named user's
    own home and validated by realpath, so neither traversal nor a symlink
    planted in that home can reach anything else.
    """
    _require_identity(request)
    from . import nas_staging
    try:
        return nas_staging.stage(body.node, body.instance, body.user, body.paths)
    except ValueError as e:
        raise HTTPException(400, str(e))


class _InstanceBody(BaseModel):
    node: str
    instance: str
    user: str = ""


@app.post("/api/sm/nas/unstage")
def sm_nas_unstage(body: _InstanceBody, request: Request):
    """Collect anything the app produced, then clear the staging directory.

    Results go to a dated folder under the user's home rather than over their
    originals — an app writing a mangled file must not be able to destroy the
    input it was handed.
    """
    _require_identity(request)
    from . import nas_staging
    try:
        collected = nas_staging.collect(body.node, body.instance, body.user) \
            if body.user else {"collected": []}
        cleared = nas_staging.clear(body.node, body.instance)
        return {**cleared, **collected}
    except ValueError as e:
        raise HTTPException(400, str(e))


class _NodeBody(BaseModel):
    node: str


@app.post("/api/sm/nas/clear-node")
def sm_nas_clear_node(body: _NodeBody, request: Request):
    """Drop a decommissioned node's whole staging tree."""
    _require_identity(request)
    from . import nas_staging
    try:
        return nas_staging.clear_node(body.node)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/sm/nas/staged")
def sm_nas_staged(request: Request, node: str = ""):
    """What is exposed to which node right now.

    'What can that box currently see' should be answerable from the dashboard,
    not by reading a filesystem by hand.
    """
    _require_identity(request)
    from . import nas_staging
    return {"staged": nas_staging.list_staged(node or None)}


@app.post("/api/sm/nas/apply-policy")
def sm_apply_nas_policy(request: Request):
    """Re-apply this node's NFS export policy from the Hub's per-node trust
    settings. Only meaningful on the node that HOSTS the NAS.

    Called by the Hub right after a node's NAS trust changes on the Fleet page,
    so the dropdown takes effect immediately instead of waiting for someone to
    run a script by hand.

    Applies live via `exportfs -ra` — it does not restart the NFS server, which
    would drop every client's mount. Needs no sudo: the script drives Docker,
    and this service's user is already in the docker group (install.sh ensures
    that, since app launching needs it too).
    """
    script = _REPO_ROOT / "containers" / "nfs-server" / "sync-nas-policy.sh"
    if not script.exists():
        raise HTTPException(404, "sync-nas-policy.sh not present on this node")
    env = dict(os.environ)
    # Only pass SM_HUB_URL through when it is a private/mesh address. The Hub
    # serves its API to the mesh only and answers a deliberate 404 to anything
    # arriving from the public internet — so a node configured with the public
    # hostname (which is easy to end up with, and was the case on the live NAS
    # host) would fail every call. The script's own mesh default is correct in
    # that case, so let it win rather than forcing a URL we know cannot work.
    _hub = getattr(config, "HUB_URL", "") or ""
    _host = _hub.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    try:
        import ipaddress as _ip
        _private = _ip.ip_address(_host).is_private
    except ValueError:
        _private = False        # a hostname — assume public-facing
    if _hub and _private:
        env["HUB_URL"] = _hub
    try:
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                           timeout=120, env=env)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "NAS policy sync timed out")
    if r.returncode != 0:
        # The script deliberately refuses to apply an empty or unreachable
        # policy rather than widening/revoking access on a Hub hiccup — surface
        # that reason rather than a bare failure.
        raise HTTPException(502, (r.stderr or r.stdout or "sync failed").strip()[:400])
    return {"ok": True, "detail": (r.stdout or "").strip()[-400:]}


@app.post("/api/sm/restart")
def sm_restart(request: Request):
    """Restart THIS node's Server Manager systemd unit — not the app
    containers it manages, which keep running independently (they're plain
    `docker run` processes, not children of this one). Fleet page button
    (per-node "Restart Server Manager"), proxied via the Hub.

    Needs a narrowly-scoped NOPASSWD sudoers rule (the service runs as the
    unprivileged `control` user, same as every other app on this node) —
    add once, as root:
      /etc/sudoers.d/61-sandos-sm-restart:
        control ALL=(root) NOPASSWD: /usr/bin/systemctl restart sandos-server-manager
    Without it this 500s with a clear sudo/password error.

    Fires the actual restart on a delayed background thread so this response
    reaches the client BEFORE `systemctl restart` sends SIGTERM to this very
    process — an immediate synchronous call would just drop the connection.
    """
    _require_admin(request)

    def _do_restart() -> None:
        time.sleep(0.5)
        subprocess.run(["sudo", "-n", "systemctl", "restart", config.SM_SYSTEMD_UNIT], timeout=30)

    threading.Thread(target=_do_restart, daemon=True, name="sm-restart").start()
    return {"ok": True, "restarting": True}


class _BusyBody(BaseModel):
    enabled: bool


@app.post("/api/sm/busy")
def sm_set_busy(body: _BusyBody, request: Request):
    """Busy mode: stop every running app instance on this node right now to
    free up its resources (e.g. before playing a game), and refuse new
    launches until it's back to Available (enforced in registry.launch()).

    Local (loopback — the Windows/WSL launcher GUI, or curl on this box)
    callers may set either state. Remote (Hub-proxied) callers may ONLY ever
    request enabled=False — "override this node back to Available" — and
    only when this node's own owner has opted into that via
    /api/sm/busy/override-permission. A remote admin can never busy-lock a
    node they don't own, and can never grant themselves override consent."""
    if not _is_loopback(request):
        _require_admin(request)
        if body.enabled:
            return JSONResponse(
                {"ok": False, "error": "remote callers may only clear Busy, never set it"},
                status_code=403)
        if not busy.override_allowed():
            return JSONResponse(
                {"ok": False, "error": "this node hasn't allowed remote override"},
                status_code=403)

    result = {}
    if body.enabled:
        result = registry.stop_all()
    busy.set_busy(body.enabled)
    return {"ok": True, "busy": body.enabled, **result}


class _BusyOverrideBody(BaseModel):
    allowed: bool


@app.post("/api/sm/busy/override-permission")
def sm_set_busy_override_permission(body: _BusyOverrideBody, request: Request):
    """The owner's own consent switch — may a remote Hub admin force this
    node back to Available while it's Busy? Loopback-only, no remote path
    at all, ever: granting this permission is a decision only the machine's
    own owner can make for themselves."""
    if not _is_loopback(request):
        return JSONResponse(
            {"ok": False, "error": "this setting can only be changed from this machine itself"},
            status_code=403)
    busy.set_override_allowed(body.allowed)
    return {"ok": True, "override_allowed": body.allowed}


@app.get("/api/sm/processes")
def sm_processes(request: Request):
    """Running processes on this node (CPU/RAM). Authenticated — the Hub's Fleet
    page reaches this through /api/fleet/nodes/<id>/processes (admin-gated)."""
    _require_identity(request)
    return {"processes": metrics.top_processes()}


@app.get("/api/sm/monitor")
def sm_monitor(request: Request):
    """Rich live monitor (Glances): per-core CPU, memory, load, network + a full
    process list. Authenticated; the Hub's Fleet page proxies it (admin-gated).
    Falls back to the lightweight process list if Glances isn't ready yet."""
    _require_identity(request)
    snap = glances_svc.monitor()
    if snap is None:
        return {"ready": False, "processes": metrics.top_processes()}
    snap["ready"] = True
    return snap


@app.get("/api/sm/apps/stats")
def sm_apps_stats(request: Request):
    """Per-app instance breakdown with live CPU/RAM (docker stats), for the
    Hub's Fleet page. Not gated per-app (unlike /api/apps) — this is node
    administration/monitoring, visible to anyone who can see the Fleet page."""
    _require_identity(request)
    instances = registry.instances_summary()
    # Bucket by daemon (an app whose image lives on a USB drive is only
    # visible to THAT drive's secondary dockerd, not the default one).
    by_host: dict[str | None, list[str]] = {}
    for i in instances:
        if i["running"]:
            by_host.setdefault(app_images.active_docker_host(i["app_id"]), []).append(i["name"])
    live: dict[str, dict] = {}
    for host, names in by_host.items():
        live.update(docker_backend.stats(names, host=host))
    by_app: dict[str, list[dict]] = {}
    for i in instances:
        if not i["running"]:
            continue
        entry = {"user": None if i["user"] == registry._SHARED else i["user"],
                  **(live.get(i["name"]) or {})}
        by_app.setdefault(i["app_id"], []).append(entry)
    return {"apps": [
        {"id": a.id, "label": a.label, "instance_count": len(by_app.get(a.id, [])),
         "instances": by_app.get(a.id, [])}
        for a in registry.APPS.values()
    ]}


@app.get("/api/apps")
def list_apps(request: Request):
    ident = _require_identity(request)
    apps = registry.list_for_user(ident["username"])
    # Scoped accounts only see the apps their profiles grant.
    if ident.get("role") == "scoped":
        apps = [a for a in apps if _app_allowed(ident, a.get("id"))]
    return {"apps": apps}


@app.post("/api/apps/{app_id}/launch")
def launch(app_id: str, request: Request):
    user = _require_app(request, app_id)["username"]
    try:
        inst = registry.launch(app_id, user)
    except KeyError:
        return JSONResponse({"ok": False, "error": "unknown app"}, status_code=404)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "url": registry.url_for(inst), "status": registry.status(app_id, user)}


@app.post("/api/apps/{app_id}/stop")
def stop(app_id: str, request: Request):
    registry.stop(app_id, _require_app(request, app_id)["username"])
    return {"ok": True, "status": "stopped"}


@app.post("/api/apps/{app_id}/stop-all")
def stop_app_all(app_id: str, request: Request):
    """Admin-only: stop EVERY running instance of this one app on this node,
    regardless of which user owns it — see registry.stop_app()'s docstring
    for why the plain per-user stop above isn't enough for this. Used by the
    Hub Fleet page's toggle, not the per-user Apps page."""
    _require_admin(request)
    return registry.stop_app(app_id)


# ── Per-user app state: factory reset + snapshots (NAS .appdata) ───────────────
class _SnapBody(BaseModel):
    label: str = ""


class _RestoreBody(BaseModel):
    file: str


class _VariantInstallBody(BaseModel):
    variant_id: str


class _VariantSelectBody(BaseModel):
    variant_id: str


@app.get("/api/apps/{app_id}/source-check")
def app_source_check(app_id: str, request: Request):
    """This node's own half of the dev-source-vs-packaged-image staleness
    check (see App Definition Standard §8-10) — the Hub's fleet_source_check
    combines this node's answer with whichever node actually has the live
    dev-source checkout to tell "is this packaged copy behind". Read-only,
    on-demand only; nothing here ever triggers a rebuild."""
    _require_identity(request)
    app = registry.APPS.get(app_id)
    if app is None or not app.binds:
        return {"has_dev_source_concept": False}
    host = app_images.active_docker_host(app_id)
    return {
        "has_dev_source_concept": True,
        "dev_source_commit": registry.dev_source_commit(app),   # non-None only if THIS node has the real checkout
        "dev_source_remote_status": registry.dev_source_remote_status(app),  # vs GitHub, if a remote exists
        "image_commit": app_variants.image_source_commit(app, host=host),
        "image_installed": registry.image_installed(app),
    }


@app.post("/api/apps/{app_id}/packaged-build")
def app_packaged_build(app_id: str, request: Request):
    """Rebuild this app's self-contained packaged image FROM THIS NODE's own
    checkout — only succeeds on the dev machine itself (see
    app_variants.build_packaged()). The Hub's fleet_rebuild_and_deploy is
    what actually orchestrates finding the right node to call this on, then
    transferring the result to wherever it should run — this endpoint just
    does the build. Admin-only: it's a real build+label action, not a
    read-only check."""
    ident = _require_identity(request)
    if ident.get("role") != "admin":
        raise HTTPException(403, "admin only")
    app = registry.APPS.get(app_id)
    if app is None:
        raise HTTPException(404, "unknown app")
    try:
        return app_variants.build_packaged(app)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/apps/{app_id}/packaged-build/status")
def app_packaged_build_status(app_id: str, request: Request):
    _require_identity(request)
    status = app_variants.packaged_build_status(app_id)
    if status is None:
        raise HTTPException(404, "no packaged build in progress or completed on this node")
    return status


@app.post("/api/apps/{app_id}/publish")
def app_publish(app_id: str, request: Request):
    """Publish this app to Docker Hub (App Definition Standard §11) — rebuilds
    fresh (see app_variants.build_packaged()'s dev-machine-only guard) then
    pushes :latest + :<short-commit> to app.dockerhub_repo, carrying a
    sandos.appdef label so a DIFFERENT Server Manager install can auto-wire
    it. Admin-only, and requires `docker login` already set up on this
    node's daemon — this never touches or stores a registry credential."""
    ident = _require_identity(request)
    if ident.get("role") != "admin":
        raise HTTPException(403, "admin only")
    app = registry.APPS.get(app_id)
    if app is None:
        raise HTTPException(404, "unknown app")
    try:
        return app_variants.publish_to_dockerhub(app)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/apps/{app_id}/publish/status")
def app_publish_status(app_id: str, request: Request):
    _require_identity(request)
    status = app_variants.publish_status(app_id)
    if status is None:
        raise HTTPException(404, "no publish in progress or completed on this node")
    return status


# ── Install apps FROM Docker Hub (App Definition Standard §11, user side) ────
# NOTE: these two literal paths are registered BEFORE /api/apps/{app_id}/status
# below on purpose — FastAPI matches in registration order, so the literal
# "hub-install" segment wins over the {app_id} pattern only because it comes
# first. Don't move them below the parameterized routes.

class _HubInstallBody(BaseModel):
    repo: str
    env: dict[str, str] = {}
    internal_port: int | None = None   # generic installs: explicit HTTP port


@app.post("/api/apps/hub-install")
def hub_install(body: _HubInstallBody, request: Request):
    """Pull an app image from Docker Hub and register it as a live app on
    THIS node. Sand-OS-published images bring their full sandos.appdef
    manifest; anything else gets a generic install from the image's own
    EXPOSE/VOLUME metadata (sane defaults, no SSO/GPU/per-user).
    Admin-only: installs software."""
    _require_admin(request)
    try:
        return dockerhub_apps.install(body.repo, body.env, body.internal_port)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/apps/hub-install/status")
def hub_install_status(repo: str, request: Request):
    _require_identity(request)
    status = dockerhub_apps.job_status(repo)
    if status is None:
        raise HTTPException(404, "no install job for that repo on this node")
    return status


# ── Per-node app library: which SHIPPED apps are enabled here ────────────────
# A fresh install starts empty (registry._seed_enabled()); these endpoints
# are how built-in apps get added to (or removed from) a node's library.
# Same registration-order note as hub-install above: the literal "catalog"
# segment must register before /api/apps/{app_id}/status.

@app.get("/api/apps/catalog")
def apps_catalog(request: Request):
    _require_identity(request)
    return {"catalog": registry.catalog_summary()}


@app.post("/api/apps/catalog/{app_id}/enable")
def catalog_enable(app_id: str, request: Request):
    _require_admin(request)
    try:
        return registry.enable_app(app_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/apps/catalog/{app_id}/disable")
def catalog_disable(app_id: str, request: Request):
    _require_admin(request)
    try:
        return registry.disable_app(app_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/apps/{app_id}/hub-uninstall")
def hub_uninstall(app_id: str, request: Request):
    _require_admin(request)
    try:
        return dockerhub_apps.uninstall(app_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/apps/{app_id}/hub-check")
def hub_check(app_id: str, request: Request):
    """Is a newer image available on Docker Hub for this hub-installed app?
    Digest comparison via the registry API — read-only, nothing pulled."""
    _require_identity(request)
    try:
        return dockerhub_apps.check_update(app_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/apps/{app_id}/hub-update")
def hub_update(app_id: str, request: Request):
    """Re-pull + re-register from the same repo:tag (keeps configured env).
    Poll /api/apps/hub-install/status?repo=… — same job machinery."""
    _require_admin(request)
    try:
        return dockerhub_apps.update(app_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/apps/{app_id}/variants")
def app_variants_list(app_id: str, request: Request, dev: bool = False):
    """Catalog + installed/active state for this app's installable versions.
    dev=true also lists 'dev' channel entries (e.g. the weekly build)."""
    _require_app(request, app_id)
    app = registry.APPS.get(app_id)
    if app is None:
        return JSONResponse({"ok": False, "error": "unknown app"}, status_code=404)
    return {"ok": True, **app_variants.list_variants(app, show_dev=dev)}


@app.post("/api/apps/{app_id}/variants/install")
def app_variants_install(app_id: str, request: Request, body: _VariantInstallBody):
    """Kick off a build/pull for one variant (background; poll via the list
    endpoint's `installing` field for progress)."""
    _require_admin(request)
    app = registry.APPS.get(app_id)
    if app is None:
        return JSONResponse({"ok": False, "error": "unknown app"}, status_code=404)
    try:
        return {"ok": True, **app_variants.install(app, body.variant_id)}
    except (KeyError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/apps/{app_id}/variants/select")
def app_variants_select(app_id: str, request: Request, body: _VariantSelectBody):
    """Switch which installed version future launches use. Takes effect on
    the NEXT launch — stop + start (or Restart) a running instance to apply."""
    _require_admin(request)
    app = registry.APPS.get(app_id)
    if app is None:
        return JSONResponse({"ok": False, "error": "unknown app"}, status_code=404)
    try:
        return {"ok": True, **app_variants.select(app, body.variant_id)}
    except (KeyError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/apps/{app_id}/variants/uninstall")
def app_variants_uninstall(app_id: str, request: Request, body: _VariantSelectBody):
    """Remove an installed version's image to free disk. Refused if it's the
    active version or an instance is currently using it."""
    _require_admin(request)
    app = registry.APPS.get(app_id)
    if app is None:
        return JSONResponse({"ok": False, "error": "unknown app"}, status_code=404)
    try:
        return {"ok": True, **app_variants.uninstall(app, body.variant_id)}
    except (KeyError, ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


class _StorageMoveBody(BaseModel):
    mount_name: str
    target_mode: str          # local | nfs | usb
    usb_uuid: str | None = None


class _StorageReclaimBody(BaseModel):
    mount_name: str
    old_volume: str


@app.get("/api/apps/{app_id}/storage")
def app_storage_list(app_id: str, request: Request):
    """Where this app's data volumes currently live + what they could move to
    (local / fleet NAS / an assigned USB drive) — backs the dashboard's
    'Storage location' section in the app-manage modal."""
    ident = _require_admin(request)
    user = registry._eff(app_id, ident["username"])
    try:
        return {"ok": True, **app_storage.list_locations(app_id, user)}
    except KeyError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)


@app.post("/api/apps/{app_id}/storage/move")
def app_storage_move(app_id: str, request: Request, body: _StorageMoveBody):
    """Start moving one Mount's data to a new location in the background.
    Returns a job_id immediately; poll GET /storage/move/status/{job_id}."""
    ident = _require_admin(request)
    user = registry._eff(app_id, ident["username"])
    try:
        job_id = app_storage.start_move(
            app_id, user, body.mount_name, body.target_mode, body.usb_uuid)
        return {"ok": True, "job_id": job_id}
    except (KeyError, ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/apps/{app_id}/storage/move/status/{job_id}")
def app_storage_move_status(app_id: str, job_id: str, request: Request):
    """Poll the status of a background storage-move job."""
    _require_admin(request)
    job = app_storage.move_status(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    return {"ok": True, **job}


@app.post("/api/apps/{app_id}/storage/reclaim")
def app_storage_reclaim(app_id: str, request: Request, body: _StorageReclaimBody):
    """Free a moved-away-from volume once its replacement is confirmed good."""
    ident = _require_admin(request)
    user = registry._eff(app_id, ident["username"])
    try:
        return {"ok": True, **app_storage.delete_old(
            app_id, user, body.mount_name, body.old_volume)}
    except (KeyError, ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ── Image location — move/mirror the app's IMAGE (not its data) to USB ────────
class _ImageMoveBody(BaseModel):
    usb_uuid: str


@app.get("/api/apps/{app_id}/image-location")
def app_image_location(app_id: str, request: Request):
    """Where this app's IMAGE currently lives (local disk / a USB drive) and
    what app-hosting-enabled drives it could move/mirror to — backs the
    Manage modal's 'Image location' section."""
    _require_admin(request)
    try:
        return {"ok": True, **app_images.list_image_options(app_id)}
    except KeyError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)


@app.post("/api/apps/{app_id}/image-location/move")
def app_image_move(app_id: str, request: Request, body: _ImageMoveBody):
    """Start moving the image to USB in the background. Returns job_id immediately."""
    _require_admin(request)
    try:
        job_id = app_images.start_move_to_usb(app_id, body.usb_uuid, keep_local=False)
        return {"ok": True, "job_id": job_id}
    except (KeyError, ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/apps/{app_id}/image-location/mirror")
def app_image_mirror(app_id: str, request: Request, body: _ImageMoveBody):
    """Start mirroring the image to USB in the background. Returns job_id immediately."""
    _require_admin(request)
    try:
        job_id = app_images.start_move_to_usb(app_id, body.usb_uuid, keep_local=True)
        return {"ok": True, "job_id": job_id}
    except (KeyError, ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/apps/{app_id}/image-location/move-to-local")
def app_image_move_to_local(app_id: str, request: Request):
    """Copy the image back to local disk AND remove the USB copy — frees the
    drive. Returns job_id immediately."""
    _require_admin(request)
    try:
        job_id = app_images.start_move_to_local(app_id, delete_usb_copy=True)
        return {"ok": True, "job_id": job_id}
    except (KeyError, ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/apps/{app_id}/image-location/clone-to-local")
def app_image_clone_to_local(app_id: str, request: Request):
    """Copy the image back to local disk but LEAVE the USB copy in place —
    for when the drive should stay portable to another node. Returns
    job_id immediately."""
    _require_admin(request)
    try:
        job_id = app_images.start_move_to_local(app_id, delete_usb_copy=False)
        return {"ok": True, "job_id": job_id}
    except (KeyError, ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/apps/{app_id}/image-location/move/status/{job_id}")
def app_image_move_status(app_id: str, job_id: str, request: Request):
    """Poll the status of a background image-move job."""
    _require_admin(request)
    job = app_images.img_job_status(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    return {"ok": True, **job}


@app.post("/api/apps/{app_id}/image-location/remove-usb-copy")
def app_image_remove_usb_copy(app_id: str, request: Request):
    """Explicit follow-up to move-to-local: delete the now-leftover USB copy.
    Refuses if that drive is still the ACTIVE copy (can't happen by accident)."""
    _require_admin(request)
    try:
        return {"ok": True, **app_images.remove_usb_copy(app_id)}
    except (KeyError, ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/apps/{app_id}/uninstall")
def app_uninstall(app_id: str, request: Request):
    """Delete this app's currently-active image. Refused while any
    container — running or stopped — still references it. Never touches a
    `binds` app's host source-tree directory; image and source are
    separate concerns, so rebuilding after this still needs that tree."""
    _require_admin(request)
    try:
        return app_images.uninstall_app(app_id)
    except KeyError as e:
        return JSONResponse({"ok": False, "error": f"unknown app {e}"}, status_code=404)
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ── Same-mesh app portability — pending imports from a plugged-in drive ────────
@app.get("/api/apps/pending-imports")
def apps_pending_imports(request: Request):
    """Apps detected on an app-hosting-enabled USB drive that this node has
    never seen before. NEVER auto-registered — surfaced here for an explicit
    admin click (see /import) per the no-silent-execution-from-removable-
    media decision."""
    _require_admin(request)
    return {"ok": True, "pending": pending_imports.list_pending()}


@app.post("/api/apps/pending-imports/{app_id}/import")
def apps_import_pending(app_id: str, request: Request):
    _require_admin(request)
    try:
        return {"ok": True, **pending_imports.import_app(app_id)}
    except (KeyError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/apps/pending-imports/{app_id}/dismiss")
def apps_dismiss_pending(app_id: str, request: Request):
    _require_admin(request)
    return {"ok": True, **pending_imports.dismiss(app_id)}


@app.post("/api/apps/{app_id}/reset")
def app_reset(app_id: str, request: Request):
    """Factory defaults: stop + wipe the app's per-user settings. Files kept."""
    user = _require_app(request, app_id)["username"]
    if not snapshots.has_appdata(app_id):
        return JSONResponse({"ok": False, "error": "app keeps no per-user settings"}, status_code=400)
    return {"ok": True, **snapshots.reset(app_id, user)}


@app.get("/api/apps/{app_id}/snapshots")
def app_snapshots(app_id: str, request: Request):
    user = _require_app(request, app_id)["username"]
    return {"ok": True, "snapshots": snapshots.list_snapshots(app_id, user),
            "supported": snapshots.has_appdata(app_id)}


@app.post("/api/apps/{app_id}/snapshot")
def app_snapshot(app_id: str, request: Request, body: _SnapBody):
    """Save this app's current per-user settings to the user's NAS folder
    (users/<u>/snapshots/) — restorable on any node in the fleet."""
    user = _require_app(request, app_id)["username"]
    try:
        return {"ok": True, **snapshots.snapshot(app_id, user, body.label)}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/apps/{app_id}/restore")
def app_restore(app_id: str, request: Request, body: _RestoreBody):
    """Stop the app and load a saved snapshot; next launch uses those settings."""
    user = _require_app(request, app_id)["username"]
    try:
        return {"ok": True, **snapshots.restore(app_id, user, body.file)}
    except (FileNotFoundError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)


@app.get("/api/apps/{app_id}/status")
def status(app_id: str, request: Request):
    return {"status": registry.status(app_id, _require_app(request, app_id)["username"])}


# ── Ollama model management + LLM proxy ──────────────────────────────────────
# These routes are what the Hub LLM router calls — they proxy to the local
# Ollama container (resolved by the SM slot registry, not a hardcoded port).

class _PullBody(BaseModel):
    model: str


class _DeleteBody(BaseModel):
    model: str


class _InternetBody(BaseModel):
    enabled: bool


class _OllamaExportBody(BaseModel):
    model: str


class _OllamaImportBody(BaseModel):
    model: str


@app.get("/api/apps/ollama/models")
def ollama_models(request: Request):
    """Installed models — used by the Hub LLM router to build its model inventory."""
    _require_identity(request)
    return {"ok": True, "models": ollama_mgr.list_models(),
            "ollama_running": ollama_mgr.ollama_running()}


@app.get("/api/apps/ollama/loaded")
def ollama_loaded(request: Request):
    """Models currently resident in memory (Ollama's /api/ps), plus the
    keep-alive setting. This is what the dashboard's green "loaded" dot and
    unload controls read — Open WebUI can't show them itself because it talks
    to us as a plain OpenAI endpoint (ENABLE_OLLAMA_API=false), and the
    OpenAI API has no concept of a loaded model."""
    _require_identity(request)
    return {"ok": True,
            "running": ollama_mgr.ollama_running(),
            "loaded": ollama_mgr.running_models(),
            "keep_alive": ollama_mgr.get_keep_alive(),
            "model_config": ollama_mgr.model_config()}


class _OllamaUnloadBody(BaseModel):
    model: str


@app.post("/api/apps/ollama/unload")
def ollama_unload(body: _OllamaUnloadBody, request: Request):
    """Evict one model from memory now."""
    _require_admin(request)
    ok, msg = ollama_mgr.unload_model(body.model)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


class _OllamaKeepAliveBody(BaseModel):
    keep_alive: str


@app.post("/api/apps/ollama/keep-alive")
def ollama_keep_alive(body: _OllamaKeepAliveBody, request: Request):
    """Set how long a model stays resident after its last request. Ollama
    reads this at process start, so it takes effect on the next launch —
    the response says so rather than implying it applied instantly."""
    _require_admin(request)
    ok, msg = ollama_mgr.set_keep_alive(body.keep_alive)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg,
            "applies": "next Ollama start" if ollama_mgr.ollama_running() else "now"}


class _OllamaModelCfgBody(BaseModel):
    model: str
    num_ctx: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    num_gpu: int | None = None


@app.post("/api/apps/ollama/model-config")
def ollama_model_config(body: _OllamaModelCfgBody, request: Request):
    """Set per-model inference parameters ON THIS NODE. Per-node by design:
    the same model on a GPU box and a small server wants different limits.
    Saved and baked onto the model so it applies to every request, including
    through the OpenAI-compatible endpoint Open WebUI uses."""
    _require_admin(request)
    ok, msg = ollama_mgr.set_model_config(
        body.model,
        {"num_ctx": body.num_ctx, "temperature": body.temperature,
         "top_p": body.top_p, "num_gpu": body.num_gpu})
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg, "config": ollama_mgr.model_config(body.model)}


@app.get("/api/apps/ollama/llm-status")
def ollama_llm_status(request: Request):
    """Full LLM-node snapshot (running models, load score) for the Hub router poller.

    NOT /api/apps/ollama/status: the generic /api/apps/{app_id}/status route is
    registered earlier and would shadow it — FastAPI matches in definition order."""
    _require_identity(request)
    return {"ok": True, **ollama_mgr.node_llm_status()}


@app.post("/api/apps/ollama/models/pull")
def ollama_pull(request: Request, body: _PullBody):
    """Start pulling a model in the background. Returns job_id to poll."""
    _require_admin(request)
    try:
        job_id = ollama_mgr.start_pull(body.model)
        return {"ok": True, "job_id": job_id}
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/apps/ollama/models/pull/status/{job_id}")
def ollama_pull_status(job_id: str, request: Request):
    _require_identity(request)
    job = ollama_mgr.pull_job_status(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    return {"ok": True, **job}


@app.get("/api/apps/ollama/models/pull/active")
def ollama_pull_active(request: Request):
    """The latest pull job on this node, if any — lets a client that just
    (re)opened the model manager discover an in-progress (or just-finished)
    download without already knowing its job_id. {"active": False} if this SM
    process has never run a pull (nothing to resume — not the same as "none
    running"; a genuinely-idle-but-previously-used node still returns the last
    job here, with done=True)."""
    _require_identity(request)
    job = ollama_mgr.latest_pull_job()
    if job is None:
        return {"ok": True, "active": False}
    return {"ok": True, "active": True, **job}


@app.delete("/api/apps/ollama/models/{model_name:path}")
def ollama_delete(model_name: str, request: Request):
    _require_admin(request)
    try:
        return ollama_mgr.delete_model(model_name)
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/apps/ollama/internet")
def ollama_get_internet(request: Request):
    _require_identity(request)
    return {"ok": True, "internet_enabled": ollama_mgr.get_internet_access()}


@app.post("/api/apps/ollama/internet")
def ollama_set_internet(request: Request, body: _InternetBody):
    _require_admin(request)
    return ollama_mgr.set_internet_access(body.enabled)


@app.post("/api/apps/ollama/models/export")
def ollama_export(request: Request, body: _OllamaExportBody):
    """Export a model to NAS staging for transfer to another node."""
    _require_admin(request)
    try:
        job_id = ollama_mgr.start_export(body.model)
        return {"ok": True, "job_id": job_id}
    except (KeyError, ValueError, FileNotFoundError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/apps/ollama/models/import")
def ollama_import(request: Request, body: _OllamaImportBody):
    """Import a model from NAS staging (placed there by export on another node)."""
    _require_admin(request)
    try:
        job_id = ollama_mgr.start_import(body.model)
        return {"ok": True, "job_id": job_id}
    except (KeyError, ValueError, FileNotFoundError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/apps/ollama/models/transfer/status/{job_id}")
def ollama_transfer_status(job_id: str, request: Request):
    _require_identity(request)
    job = ollama_mgr.transfer_job_status(job_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    return {"ok": True, **job}


@app.get("/api/apps/ollama/v1/models")
async def ollama_v1_models(request: Request):
    """OpenAI-compatible model listing — what the Hub LLM router exposes upstream."""
    _require_identity(request)
    return await ollama_mgr.fetch_models_openai()


@app.post("/api/apps/ollama/v1/{path:path}")
async def ollama_v1_proxy(path: str, request: Request):
    """Streaming proxy to Ollama's OpenAI-compatible API (/v1/chat/completions etc).
    The Hub LLM router calls this; the SM resolves the actual container port."""
    _require_identity(request)
    body = await request.json()
    try:
        gen = ollama_mgr.stream_to_ollama(f"/v1/{path}", body)
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    # Preserve the content-type the caller expects (JSON or text/event-stream).
    ct = request.headers.get("accept", "application/json")
    if "event-stream" not in ct:
        ct = "application/json"
    return StreamingResponse(gen, media_type=ct)


# ── Session-gated reverse proxy to the user's instance (the secure viewer) ─────
def _ws_identity(ws: WebSocket) -> dict | None:
    if hub_auth.enabled():
        return hub_auth.verify_identity(ws.cookies.get(config.HUB_SESSION_COOKIE, ""))
    return {"username": ws.cookies.get("sm_user") or "me", "role": "admin", "grants": []}


@app.websocket("/stream/{app_id}/{path:path}")
async def stream_ws(app_id: str, path: str, websocket: WebSocket):
    ident = _ws_identity(websocket)
    if not ident or not _app_allowed(ident, app_id):
        await websocket.close(code=1008)  # unauthenticated or not granted this app
        return
    await proxy.ws(app_id, path, websocket, ident["username"])


# ── Per-app PWA assets (UNAUTHENTICATED) ──────────────────────────────────────
# Registered BEFORE the catch-all /stream route so these exact paths match first.
# Served without auth on purpose: Chrome fetches a page's manifest/icons without
# credentials, and these carry only the already-public id/label/icon/color. They
# make "Open in window" install the app as its OWN scoped PWA (its own icon).
@app.get("/stream/{app_id}/sm-app.webmanifest")
def sm_app_manifest(app_id: str):
    app_def = registry.APPS.get(app_id)
    if not app_def:
        return JSONResponse({"error": "unknown app"}, status_code=404)
    return JSONResponse(pwa.manifest(app_def, config.EXTERNAL_BASE),
                        media_type="application/manifest+json")


@app.get("/stream/{app_id}/sm-icon.svg")
def sm_app_icon(app_id: str):
    app_def = registry.APPS.get(app_id)
    if not app_def:
        return Response("not found", status_code=404)
    return Response(pwa.icon_svg(app_def), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/stream/{app_id}/sm-icon-180.png")
def sm_app_icon_png(app_id: str):
    """PNG rasterization for apple-touch-icon — iOS Safari's "Add to Home
    Screen" doesn't accept SVG there (see pwa.icon_png_180)."""
    app_def = registry.APPS.get(app_id)
    if not app_def:
        return Response("not found", status_code=404)
    png = pwa.icon_png_180(app_def)
    if png is None:
        return Response("icon rendering unavailable", status_code=404)
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


def _is_native_pwa_public_asset(app_id: str, path: str) -> bool:
    """manifest.json + static/* for a native_pwa app (Open WebUI) — PUBLIC on
    purpose, same reasoning as _PWA_ASSETS above: browsers fetch a PWA's own
    manifest/icons WITHOUT cookies while installing "Add to Home Screen", so
    gating them 401s the icon fetch and the OS falls back to a generic/blank
    icon instead of the app's real branding."""
    app = registry.APPS.get(app_id)
    return bool(app and app.native_pwa
                and (path == "manifest.json" or path.startswith("static/")))


# Includes WebDAV/CalDAV verbs — Nextcloud's Files/Photos/sync/calendar use them
# (PROPFIND/REPORT/etc.); without these the proxy 405s and Photos "can't fetch files".
@app.api_route("/stream/{app_id}/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
                        "PROPFIND", "PROPPATCH", "MKCOL", "COPY", "MOVE",
                        "LOCK", "UNLOCK", "REPORT", "SEARCH", "MKCALENDAR"])
async def stream_http(app_id: str, path: str, request: Request):
    if _is_native_pwa_public_asset(app_id, path):
        user, role = "_pwa", None
    else:
        ident = _require_app(request, app_id)
        user, role = ident["username"], ident.get("role")
    return await proxy.http(app_id, path, request, user, role)


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
