"""Sand-OS Server Manager — API + static apps screen.

MVP: single-node orchestration of streamed apps (FreeCAD). Per-user instances,
launch / stop / status, served with a minimal apps screen. Real identity (Hub
SSO) and the auth-gated TLS proxy come in the next phase — for now 'user' is a
cookie so per-user instances are demonstrable on the LAN.
"""
from __future__ import annotations
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import registry

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Sand-OS Server Manager")


@app.on_event("startup")
def _startup() -> None:
    registry.reconcile_from_docker()


def _user(request: Request) -> str:
    return request.cookies.get("sm_user") or "me"


@app.middleware("http")
async def ensure_user_cookie(request: Request, call_next):
    response = await call_next(request)
    if not request.cookies.get("sm_user"):
        response.set_cookie("sm_user", uuid.uuid4().hex[:12], max_age=60 * 60 * 24 * 365,
                            httponly=True, samesite="lax")
    return response


@app.get("/api/apps")
def list_apps(request: Request):
    return {"apps": registry.list_for_user(_user(request))}


@app.post("/api/apps/{app_id}/launch")
def launch(app_id: str, request: Request):
    user = _user(request)
    try:
        inst = registry.launch(app_id, user)
    except KeyError:
        return JSONResponse({"ok": False, "error": "unknown app"}, status_code=404)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "url": registry.url_for(inst), "status": registry.status(app_id, user)}


@app.post("/api/apps/{app_id}/stop")
def stop(app_id: str, request: Request):
    registry.stop(app_id, _user(request))
    return {"ok": True, "status": "stopped"}


@app.get("/api/apps/{app_id}/status")
def status(app_id: str, request: Request):
    return {"status": registry.status(app_id, _user(request))}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
