"""Sand-OS Server Manager — API + static apps screen.

MVP: single-node orchestration of streamed apps (FreeCAD). Per-user instances,
launch / stop / status, served with a minimal apps screen. Real identity (Hub
SSO) and the auth-gated TLS proxy come in the next phase — for now 'user' is a
cookie so per-user instances are demonstrable on the LAN.
"""
from __future__ import annotations
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, hub_auth, metrics, nas, proxy, registry

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Sand-OS Server Manager")


@app.on_event("startup")
def _startup() -> None:
    registry.reconcile_from_docker()


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


# ── Fleet NAS: shared-folder management (admin-only) ──────────────────────────
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
        "node_name": config.NODE_NAME,
        "lan_ip": config.LAN_IP,
        "port": config.SM_PORT,
        "gpu": config.HAS_GPU,
        "slots_total": config.SLOT_COUNT,
        "metrics": metrics.collect(),
        "apps": [
            {"id": a.id, "label": a.label, "kind": a.kind, "mode": a.mode,
             "gpu": a.gpu, "icon": a.icon, "color": a.color, "desc": a.desc}
            for a in registry.APPS.values()
        ],
    }


@app.get("/api/sm/processes")
def sm_processes(request: Request):
    """Running processes on this node (CPU/RAM). Authenticated — the Hub's Fleet
    page reaches this through /api/fleet/nodes/<id>/processes (admin-gated)."""
    _require_identity(request)
    return {"processes": metrics.top_processes()}


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


@app.get("/api/apps/{app_id}/status")
def status(app_id: str, request: Request):
    return {"status": registry.status(app_id, _require_app(request, app_id)["username"])}


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
