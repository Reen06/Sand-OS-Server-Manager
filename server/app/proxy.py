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
import logging
import os

import httpx
import websockets
from fastapi import Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

from . import config, docker_backend, registry

log = logging.getLogger("sm.proxy")
if not log.handlers:
    _h = logging.FileHandler(os.path.join(os.path.dirname(__file__), "..", "proxy.log"))
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

_HOP = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "content-length",
        "content-encoding", "authorization"}
# Never let the browser cache the streamed client — stale copies (with old paths)
# broke the proxy. Drop conditional-request headers so the instance always sends
# fresh 200s, strip its caching headers, and force no-store.
_NO_FORWARD_REQ = {"if-none-match", "if-modified-since", "if-range"}
_STRIP_RESP = {"etag", "last-modified", "cache-control", "expires", "age"}


def _auth() -> str:
    raw = f"{config.INSTANCE_USER}:{config.INSTANCE_PASSWD}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _instance_port(app_id: str, user: str) -> int | None:
    inst = registry.get_instance(app_id, user)
    if inst and docker_backend.running(inst.name):
        return inst.web_port
    return None


def _upstream_path(app_id: str, path: str) -> str:
    """Path to request from the instance, per the app's subpath mode:
      - forward: prepend the external prefix ({EXTERNAL_BASE}/stream/{app}) that
        Caddy+the SM route stripped — the app knows its baseURL and strips it
        (Filebrowser).
      - root (default for streamed / Nextcloud): serve at container root. Streamed
        Selkies clients use path-relative URLs; Nextcloud rewrites its own links
        via OVERWRITEWEBROOT."""
    app = registry.APPS.get(app_id)
    if app and not app.streamed and app.proxy_subpath == "forward":
        return f"{config.EXTERNAL_BASE}/stream/{app_id}/{path}".lstrip("/")
    return path


def _fwd_headers(app, request: Request, user: str) -> dict:
    """Build upstream request headers: drop hop/conditional headers, inject the
    Selkies basic-auth for streamed apps only, and set a TRUSTED SSO header
    (stripping any client-supplied copy) for apps that use header SSO."""
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in _HOP and k.lower() not in _NO_FORWARD_REQ}
    streamed = app.streamed if app else True
    if streamed:
        fwd["Authorization"] = _auth()          # instance basic-auth (Selkies only)
        fwd.pop("cookie", None)                  # streamed apps use injected auth,
                                                 # not cookies — don't leak the Hub's
    # Web apps (Nextcloud) keep the browser Cookie header — their session lives in
    # it; stripping it caused an auth→login redirect loop.
    if app and app.sso_header:
        fwd.pop(app.sso_header.lower(), None)    # never trust a client-sent copy
        fwd[app.sso_header] = user               # inject the authenticated identity
    fwd["X-Forwarded-Proto"] = "https"           # we terminate TLS at the Hub
    host = request.headers.get("host", "")
    fwd["X-Forwarded-Host"] = host
    # Web apps generate absolute URLs from the Host (Nextcloud redirects); forward
    # the real browser Host (Caddy passes it through via header_up) so those URLs
    # land back on the same origin. Streamed apps are localhost-only — Host is moot.
    if app and not app.streamed and host:
        fwd["Host"] = host
    return fwd


async def http(app_id: str, path: str, request: Request, user: str) -> Response:
    port = _instance_port(app_id, user)
    if port is None:
        log.warning("HTTP %s /%s user=%s → no running instance", request.method, path, user)
        return Response("app not running", status_code=502)
    target = f"http://127.0.0.1:{port}/{_upstream_path(app_id, path)}"
    fwd = _fwd_headers(registry.APPS.get(app_id), request, user)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            r = await client.request(request.method, target,
                                     params=dict(request.query_params),
                                     headers=fwd, content=await request.body())
    except Exception as e:  # noqa: BLE001
        log.exception("HTTP %s /%s → upstream error", request.method, path)
        return Response(f"upstream error: {e}", status_code=502)
    log.info("HTTP %s /%s user=%s → %s", request.method, path, user, r.status_code)
    app = registry.APPS.get(app_id)
    streamed = app.streamed if app else True
    out = {k: v for k, v in r.headers.items()
           if k.lower() not in _HOP and k.lower() != "set-cookie"
           and (not streamed or k.lower() not in _STRIP_RESP)}
    # user_saml under OVERWRITEWEBROOT mis-generates its own route URLs: the app
    # segment `apps/user_saml` comes out as the corrupt `index.php_saml`, so the
    # first (unauthenticated) load 302s into a 404 even though the SSO session was
    # just established. Nextcloud serves the correct `apps/user_saml/...` path, so
    # repair the redirect target it emits. Scoped to the exact corrupt token, which
    # never appears in a legitimate URL.
    for k in list(out):
        if k.lower() == "location" and "index.php_saml" in out[k]:
            out[k] = out[k].replace("index.php_saml", "apps/user_saml")
    # Streamed (Selkies) clients must never be cached — a stale copy with old
    # paths broke the proxy. Web apps keep their own caching headers untouched.
    if streamed:
        out["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp = Response(content=r.content, status_code=r.status_code, headers=out,
                    media_type=r.headers.get("content-type"))
    # Forward EACH Set-Cookie separately — Nextcloud sets several session cookies
    # and a plain dict keeps only the last, which breaks the session (redirect loop).
    for cookie in r.headers.get_list("set-cookie"):
        resp.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
    # ONE-TIME on a streamed app's entry page: wipe any stale service worker +
    # caches the browser latched onto (they served an old client that broke the
    # proxy). Cookie-gated so it fires exactly once — no loop. Skipped for web
    # apps, where Clear-Site-Data would wipe their legitimate storage/logins.
    if streamed and path in ("", "index.html") and "sm_swcleared" not in request.cookies:
        resp.headers["Clear-Site-Data"] = '"cache", "storage", "executionContexts"'
        resp.set_cookie("sm_swcleared", "1", max_age=31536000, path="/", samesite="lax")
    return resp


async def ws(app_id: str, path: str, client_ws: WebSocket, user: str) -> None:
    port = _instance_port(app_id, user)
    if port is None:
        log.warning("WS /%s user=%s → no running instance", path, user)
        await client_ws.close(code=1011)
        return
    qs = client_ws.url.query
    target = f"ws://127.0.0.1:{port}/{_upstream_path(app_id, path)}" + (f"?{qs}" if qs else "")
    subprotocols = client_ws.scope.get("subprotocols") or []
    await client_ws.accept(subprotocol=subprotocols[0] if subprotocols else None)
    log.info("WS open /%s user=%s → %s", path, user, target)

    # Upstream headers: Selkies basic-auth for streamed apps; trusted SSO header
    # for header-SSO apps (same rules as HTTP).
    app = registry.APPS.get(app_id)
    streamed = app.streamed if app else True
    hdrs = []
    if streamed:
        hdrs.append(("Authorization", _auth()))
    else:
        cookie = client_ws.headers.get("cookie")   # web-app session lives here
        if cookie:
            hdrs.append(("Cookie", cookie))
    if app and app.sso_header:
        hdrs.append((app.sso_header, user))
    # `websockets` renamed extra_headers → additional_headers across versions.
    conn_kw = {"subprotocols": subprotocols or None, "max_size": None}
    try:
        upstream = await websockets.connect(target, additional_headers=hdrs, **conn_kw)
    except TypeError:
        upstream = await websockets.connect(target, extra_headers=hdrs, **conn_kw)
    except Exception:
        log.exception("WS /%s → upstream connect failed", path)
        await client_ws.close(code=1011)
        return
    log.info("WS /%s → upstream connected", path)

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

    # As soon as EITHER side ends (usually the client closing the viewer), tear
    # down the other + the upstream — otherwise the instance keeps the old
    # session and reconnecting with the same peer id hangs.
    c_task = asyncio.create_task(c2u())
    u_task = asyncio.create_task(u2c())
    try:
        _, pending = await asyncio.wait({c_task, u_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        log.info("WS /%s closing (freeing instance session)", path)
        await upstream.close()
        try:
            await client_ws.close()
        except Exception:  # noqa: BLE001
            pass
