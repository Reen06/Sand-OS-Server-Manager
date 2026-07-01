"""Reverse-proxy a user's streamed instance under the Hub, session-gated.

The instance's web port binds to localhost (not the LAN), so the ONLY way in is
through this proxy — which validates the Hub session and injects the instance's
basic-auth (so the user never sees a login prompt). Handles both HTTP (the
Selkies web client assets) and the signalling WebSocket. The heavy WebRTC video
does NOT flow through here — it goes browser↔TURN directly.
"""
from __future__ import annotations
import asyncio
import base64

import httpx
import websockets
from fastapi import Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

from . import config, docker_backend, registry

_HOP = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "content-length",
        "content-encoding", "cookie", "authorization"}


def _auth() -> str:
    raw = f"{config.INSTANCE_USER}:{config.INSTANCE_PASSWD}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _instance_port(app_id: str, user: str) -> int | None:
    inst = registry.get_instance(app_id, user)
    if inst and docker_backend.running(inst.name):
        return inst.web_port
    return None


async def http(app_id: str, path: str, request: Request, user: str) -> Response:
    port = _instance_port(app_id, user)
    if port is None:
        return Response("app not running", status_code=502)
    target = f"http://127.0.0.1:{port}/{path}"
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    fwd["Authorization"] = _auth()
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            r = await client.request(request.method, target,
                                     params=dict(request.query_params),
                                     headers=fwd, content=await request.body())
    except Exception as e:  # noqa: BLE001
        return Response(f"upstream error: {e}", status_code=502)
    out = {k: v for k, v in r.headers.items() if k.lower() not in _HOP}
    return Response(content=r.content, status_code=r.status_code, headers=out,
                    media_type=r.headers.get("content-type"))


async def ws(app_id: str, path: str, client_ws: WebSocket, user: str) -> None:
    port = _instance_port(app_id, user)
    if port is None:
        await client_ws.close(code=1011)
        return
    qs = client_ws.url.query
    target = f"ws://127.0.0.1:{port}/{path}" + (f"?{qs}" if qs else "")
    subprotocols = client_ws.scope.get("subprotocols") or []
    await client_ws.accept(subprotocol=subprotocols[0] if subprotocols else None)

    # `websockets` renamed extra_headers → additional_headers across versions.
    conn_kw = {"subprotocols": subprotocols or None, "max_size": None}
    try:
        upstream = await websockets.connect(target, additional_headers=[("Authorization", _auth())], **conn_kw)
    except TypeError:
        upstream = await websockets.connect(target, extra_headers=[("Authorization", _auth())], **conn_kw)
    except Exception:
        await client_ws.close(code=1011)
        return

    async def c2u() -> None:
        try:
            while True:
                msg = await client_ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except (WebSocketDisconnect, Exception):  # noqa: BLE001
            pass

    async def u2c() -> None:
        try:
            async for m in upstream:
                if isinstance(m, (bytes, bytearray)):
                    await client_ws.send_bytes(m)
                else:
                    await client_ws.send_text(m)
        except Exception:  # noqa: BLE001
            pass

    try:
        await asyncio.gather(c2u(), u2c())
    finally:
        await upstream.close()
        try:
            await client_ws.close()
        except Exception:  # noqa: BLE001
            pass
