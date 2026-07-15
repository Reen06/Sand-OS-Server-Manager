"""Sand-OS Server Manager — API + static apps screen.

MVP: single-node orchestration of streamed apps (FreeCAD). Per-user instances,
launch / stop / status, served with a minimal apps screen. Real identity (Hub
SSO) and the auth-gated TLS proxy come in the next phase — for now 'user' is a
cookie so per-user instances are demonstrable on the LAN.
"""
from __future__ import annotations
import getpass
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import app_images, app_storage, app_variants, busy, config, docker_backend, files, glances_svc, hub_auth, metrics, nas, ollama_mgr, pending_imports, proxy, pwa, registry, snapshots, usb_storage

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


@app.on_event("startup")
def _startup() -> None:
    usb_storage.start_poller()   # auto-mount marked USB drives
    registry.reconcile_from_docker()
    glances_svc.start()   # local Glances REST server for the Fleet monitor panel


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
        "node_name": config.NODE_NAME,
        "lan_ip": config.LAN_IP,
        "port": config.SM_PORT,
        "gpu": config.HAS_GPU,
        "slots_total": config.SLOT_COUNT,
        "metrics": metrics.collect(),
        "apps": [
            {"id": a.id, "label": a.label, "kind": a.kind, "mode": a.mode,
             "gpu": a.gpu, "icon": a.icon, "color": a.color, "desc": a.desc,
             "image_installed": registry.image_installed(a),
             # Everything a peer-install flow (Hub-brokered) needs to know
             # about this node's copy of the app, so it can be offered as a
             # transfer source to a fresh node that doesn't have it yet.
             "image_tag": app_images._image_tag(a),
             "binds": [list(b) for b in a.binds],
             "source_ready": registry.source_tree_ready(a),
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
    return {"ok": True, "user": getpass.getuser()}


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


# ── Per-user app state: factory reset + snapshots (NAS .appdata) ───────────────
class _SnapBody(BaseModel):
    label: str = ""


class _RestoreBody(BaseModel):
    file: str


class _VariantInstallBody(BaseModel):
    variant_id: str


class _VariantSelectBody(BaseModel):
    variant_id: str


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


# Includes WebDAV/CalDAV verbs — Nextcloud's Files/Photos/sync/calendar use them
# (PROPFIND/REPORT/etc.); without these the proxy 405s and Photos "can't fetch files".
@app.api_route("/stream/{app_id}/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
                        "PROPFIND", "PROPPATCH", "MKCOL", "COPY", "MOVE",
                        "LOCK", "UNLOCK", "REPORT", "SEARCH", "MKCALENDAR"])
async def stream_http(app_id: str, path: str, request: Request):
    user = _require_app(request, app_id)["username"]
    return await proxy.http(app_id, path, request, user)


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
