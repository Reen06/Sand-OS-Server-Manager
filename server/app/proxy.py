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
import json
import logging
import os
import re
from pathlib import Path

import httpx
import websockets
from fastapi import Request, WebSocket
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect

from . import app_images, config, docker_backend, pwa, registry

log = logging.getLogger("sm.proxy")
if not log.handlers:
    _h = logging.FileHandler(os.path.join(os.path.dirname(__file__), "..", "proxy.log"))
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# One pooled client, reused across requests. Building an httpx.AsyncClient per
# request rebuilds an SSL context each time and forgoes upstream keep-alive —
# needless per-asset overhead. Lazily created inside the running event loop.
_client: "httpx.AsyncClient | None" = None


def _get_client() -> "httpx.AsyncClient":
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            # A flat 30s timeout applied to EVERY request through this proxy used to
            # cap read time too — fine for ordinary page/asset loads, but a long LLM
            # generation (Open WebUI chat, proxied through here) can easily run past
            # that and get cut off mid-stream. `read` here is a per-chunk IDLE gap,
            # not a total-duration cap (it resets on every byte received) — mirrors
            # the Hub's own _SM_TIMEOUT_STREAM in api/llm.py and ollama_mgr.py's
            # STREAM_IDLE_TIMEOUT. A real generation runs as long as it needs to;
            # this only cuts off genuine dead air. connect/write/pool stay tight
            # since those really should be fast regardless of what's requested.
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=32, max_connections=128),
        )
    return _client

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
    # The published port itself is reachable at 127.0.0.1:<port> regardless of
    # which daemon started the container (port publishing is a host-level
    # thing) — but the RUNNING check must query the app's actual daemon, or a
    # USB-hosted app always reads as "not running" against the default one.
    inst = registry.get_instance(app_id, user)
    if inst and docker_backend.running(inst.name, host=app_images.active_docker_host(app_id)):
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


def _fwd_headers(app, request: Request, user: str, role: str | None = None,
                 keep_client_auth: bool = False) -> dict:
    """Build upstream request headers: drop hop/conditional headers, inject the
    Selkies basic-auth for streamed apps only, and set a TRUSTED SSO header
    (stripping any client-supplied copy) for apps that use header SSO.

    keep_client_auth passes the CLIENT's Authorization through instead of
    dropping it. Off by default and rightly so — for every other app the
    client's credentials are none of the app's business, and the proxy supplies
    whatever auth the app needs. A container registry inverts that: Docker's
    credentials are the ONLY thing that identifies the caller, and stripping
    them makes the registry issue an anonymous token that then fails, which
    reads as a rejected password rather than a discarded header.
    """
    drop = _HOP - {"authorization"} if keep_client_auth else _HOP
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in drop and k.lower() not in _NO_FORWARD_REQ}
    if app and app.sso_header:
        # Unconditionally: a client must never be able to assert this header,
        # including on the paths where we deliberately assert nothing ourselves.
        fwd.pop(app.sso_header.lower(), None)
    streamed = app.streamed if app else True
    if streamed:
        fwd["Authorization"] = _auth()          # instance basic-auth (Selkies only)
        fwd.pop("cookie", None)                  # streamed apps use injected auth,
                                                 # not cookies — don't leak the Hub's
    # Web apps (Nextcloud) keep the browser Cookie header — their session lives in
    # it; stripping it caused an auth→login redirect loop.
    if app and app.sso_header and not keep_client_auth:
        # Not on a path the app authenticates itself. Asserting an identity there
        # is actively harmful: Forgejo sees a reverse-proxy user alongside
        # Docker's credentials, cannot reconcile the two, and rejects the request
        # with "authGroup.Verify" — an error about the CONFLICT that reads like a
        # bad password. The header is still stripped from the client's copy
        # below in every case, so this never trusts one that was sent to us.
        fwd.pop(app.sso_header.lower(), None)    # never trust a client-sent copy
        # Some apps refuse particular names outright (Forgejo reserves "admin"),
        # which would leave that person unable to sign in at all rather than
        # merely inconvenienced. The map is applied here, at the one place the
        # identity crosses into the app, so nothing downstream has to know.
        fwd[app.sso_header] = app.sso_user_map.get(user, user)
    if app and app.sso_role_header:
        fwd.pop(app.sso_role_header.lower(), None)
        # A Hub ADMIN gets promoted to the app's own admin role too (Open
        # WebUI: "admin" instead of the AppDef's default "user") — otherwise
        # every SSO-provisioned account was permanently stuck at the AppDef's
        # one static sso_role_value with no way to ever reach that app's own
        # admin settings, Hub-admin or not. Everyone else still gets the
        # AppDef's configured default.
        fwd[app.sso_role_header] = "admin" if role == "admin" else app.sso_role_value
    fwd["X-Forwarded-Proto"] = "https"           # we terminate TLS at the Hub
    # Force gzip-only: the proxy buffers and decompresses the full response for
    # HTML injection. httpx handles gzip/deflate natively; Brotli requires an
    # optional package that isn't installed, so forwarding "br" causes httpx to
    # pass raw compressed bytes through and the browser renders binary garbage.
    fwd["accept-encoding"] = "gzip, deflate"
    host = request.headers.get("host", "")
    fwd["X-Forwarded-Host"] = host
    # Web apps generate absolute URLs from the Host (Nextcloud redirects); forward
    # the real browser Host (Caddy passes it through via header_up) so those URLs
    # land back on the same origin. Streamed apps are localhost-only — Host is moot.
    if app and not app.streamed and host:
        fwd["Host"] = host
    return fwd


_MANIFEST_LINK_RE = re.compile(rb"<link[^>]+rel=[\"']?manifest[\"']?[^>]*>", re.IGNORECASE)

_TURN_URL_HOST_RE = re.compile(r"^(turns?:)[^:?/]+")

# Some SPA builds (Stirling PDF's React frontend, at least) bake a hard
# <base href="/"> into their index.html — fine deployed unproxied at a
# domain root, but under our /stream/{app}/ prefix it makes the BROWSER
# resolve every relative asset/API path against the site's real root
# instead of the app's subpath, so every request the page makes 404s
# against SM/Hub's own routes (surfaced as their literal {"detail":"Not
# Found"}) rather than reaching the proxied app at all. Rewrite it to the
# app's real external prefix. Harmless no-op for apps that don't emit this
# tag (Nextcloud rewrites its own via OVERWRITEWEBROOT; most simple web
# builds have no <base> tag at all).
_BASE_HREF_RE = re.compile(rb'<base\s+href=["\']/["\']\s*/?>', re.IGNORECASE)


def _rewrite_base_href(content: bytes, app_id: str) -> bytes:
    prefix = f"{config.EXTERNAL_BASE}/stream/{app_id}/".encode()
    return _BASE_HREF_RE.sub(b'<base href="' + prefix + b'">', content)


# ParaView's wslink launcher returns its session JSON with a literally
# hardcoded "ws://localhost/proxy?sessionId=...&path=ws" (baked into the
# image's launcher.json, meant for same-machine testing) — the app's own JS
# passes this straight into `new WebSocket(url)` with no rewriting of its
# own, and the native WebSocket constructor requires a fully-qualified URL
# (no relative-URL resolution against the page origin like fetch() gets).
# Confirmed live 2026-07-16: without this, the app gets a real session but
# then tries to open a WebSocket to the BROWSER's OWN "localhost", which
# obviously isn't the server — surfaces as "Server disconnected" right
# after the loading spinner appears. Caddy always terminates TLS in front
# in this whole system, so wss:// is always the right scheme.
_PARAVIEW_WS_RE = re.compile(rb"ws://localhost(/proxy\?[^\"]*)")


def _rewrite_paraview_session(content: bytes, request: Request) -> bytes:
    host = request.headers.get("host", "")
    if not host:
        return content
    return _PARAVIEW_WS_RE.sub(f"wss://{host}".encode() + rb"\1", content)


def _inject_extra_turn(content: bytes) -> bytes:
    """If SM_TURN_EXTRA_HOST is set, clone each iceServer entry in the /turn
    JSON response with the extra host swapped in. This lets VPN/mobile clients
    (who can't reach the LAN IP directly) use the WireGuard IP instead, while
    LAN clients still connect to the primary TURN first.

    The TURN port is published on 0.0.0.0 so it already binds on every host
    interface including the WireGuard one — no extra port-mapping needed."""
    extra = config.TURN_EXTRA_HOST
    if not extra:
        return content
    try:
        data = json.loads(content)
        servers = data.get("iceServers")
        if not isinstance(servers, list):
            return content
        extras = []
        for entry in servers:
            urls = entry.get("urls", [])
            if isinstance(urls, str):
                urls = [urls]
            new_urls = [_TURN_URL_HOST_RE.sub(rf"\g<1>{extra}", u) for u in urls]
            extras.append({**entry, "urls": new_urls})
        data["iceServers"] = servers + extras
        return json.dumps(data).encode()
    except Exception:  # noqa: BLE001
        return content


# Vendored theme.park CSS bundles for Filebrowser (github.com/themepark-dev/theme.park,
# MIT). Served straight from disk — no runtime call to any third-party CDN, so the
# "true privacy" no-external-calls requirement holds even with theming enabled.
_FB_THEMES_DIR = Path(__file__).parent / "static" / "fb_themes"
_FB_THEME_ASSET_RE = re.compile(r"^__sm_theme/([a-z0-9-]+)\.css$")
_FB_THEMES = [
    ("", "Default"), ("dark", "Dark"), ("dracula", "Dracula"), ("nord", "Nord"),
    ("aquamarine", "Aquamarine"), ("space-gray", "Space Gray"), ("organizr", "Organizr"),
    ("plex", "Plex"), ("hotline", "Hotline"), ("hotpink", "Hot Pink"),
    ("maroon", "Maroon"), ("overseerr", "Overseerr"),
]


def _fb_theme_asset(path: str) -> Response | None:
    """Serve a vendored theme CSS bundle directly, bypassing the Filebrowser
    container entirely — these files don't exist in its image."""
    m = _FB_THEME_ASSET_RE.match(path)
    if not m:
        return None
    f = _FB_THEMES_DIR / f"{m.group(1)}.css"
    if not f.is_file():
        return Response(status_code=404)
    return FileResponse(f, media_type="text/css",
                         headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _inject_fb_theme_picker(body: bytes) -> bytes:
    """Inject a small floating theme picker into Filebrowser's entry page. Choice
    persists client-side (localStorage) and swaps in one of the vendored CSS
    bundles above — no server-side state, no per-user plumbing needed."""
    if b"</body>" not in body.lower():
        return body
    try:
        opts = "".join(f'<option value="{v}">{l}</option>' for v, l in _FB_THEMES)
        snippet = f"""
<link id="sm-theme-css" rel="stylesheet">
<div id="sm-theme-picker" style="position:fixed;bottom:14px;right:14px;z-index:99999;font-family:system-ui,sans-serif;">
<select id="sm-theme-select" title="Theme" style="padding:6px 10px;border-radius:8px;border:1px solid rgba(128,128,128,.4);background:#1e1e1eee;color:#eee;font-size:13px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.4);">
{opts}
</select>
</div>
<script>(function(){{
  var KEY = "sm_fb_theme";
  var sel = document.getElementById("sm-theme-select");
  var link = document.getElementById("sm-theme-css");
  function apply(id) {{ link.href = id ? ("__sm_theme/" + id + ".css") : ""; }}
  var saved = localStorage.getItem(KEY) || "";
  sel.value = saved;
  apply(saved);
  sel.addEventListener("change", function() {{
    localStorage.setItem(KEY, sel.value);
    apply(sel.value);
  }});
}})();</script>
""".encode("utf-8")
        idx = body.lower().rfind(b"</body>")
        return body[:idx] + snippet + body[idx:]
    except Exception:  # noqa: BLE001
        return body


# Mobile touch layer for streamed (Selkies) apps: RDP/VNC-viewer style gestures
# (tap/drag/long-press-right-click, pinch = local view zoom, 2-finger scroll,
# 3-finger middle-drag) + a soft-keyboard button. Served from disk like the
# theme bundles; no change to the streamed app's image.
_TOUCH_JS = Path(__file__).parent / "static" / "sm_touch.js"


def _touch_asset(path: str) -> Response | None:
    if path != "__sm_touch.js":
        return None
    if not _TOUCH_JS.is_file():
        return Response(status_code=404)
    return FileResponse(_TOUCH_JS, media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})


def _inject_touch(body: bytes) -> bytes:
    """Reference the touch layer from a streamed app's entry page. The script
    itself no-ops on non-touch devices, so injecting unconditionally is safe."""
    if b"</body>" not in body.lower():
        return body
    snippet = b'\n<script src="__sm_touch.js" defer></script>\n'
    idx = body.lower().rfind(b"</body>")
    return body[:idx] + snippet + body[idx:]


def _inject_pwa(body: bytes, app) -> bytes:
    """Inject this app's PWA manifest/icon/theme into its entry HTML so the popped-out
    window installs as its OWN scoped app. Strips the app's own manifest link so ours
    governs the install. Best-effort: any failure returns the body untouched."""
    if not app or b"</head>" not in body.lower():
        return body
    try:
        body = _MANIFEST_LINK_RE.sub(b"", body)
        idx = body.lower().rfind(b"</head>")
        if idx == -1:
            return body
        tags = pwa.head_tags(app, config.EXTERNAL_BASE).encode("utf-8")
        return body[:idx] + tags + body[idx:]
    except Exception:  # noqa: BLE001
        return body


def _inject_apple_touch_icon(body: bytes, href: str) -> bytes:
    """Add ONLY an apple-touch-icon link — for a native_pwa app whose own real
    manifest/icons are otherwise left completely untouched. iOS Safari's "Add
    to Home Screen" mostly ignores the JSON manifest and specifically wants
    this tag; without it Safari falls back to a low-res favicon or a page
    screenshot. Nothing existing is stripped, unlike _inject_pwa above."""
    if b"</head>" not in body.lower():
        return body
    try:
        idx = body.lower().rfind(b"</head>")
        tag = f'<link rel="apple-touch-icon" href="{href}">'.encode("utf-8")
        return body[:idx] + tag + body[idx:]
    except Exception:  # noqa: BLE001
        return body


async def http(app_id: str, path: str, request: Request, user: str, role: str | None = None) -> Response:
    if app_id == "filebrowser":
        asset = _fb_theme_asset(path)
        if asset is not None:
            return asset
    if app_id == "paraview" and not path:
        # The upstream image's Apache vhost only aliases /visualizer to the
        # real app — its actual DocumentRoot is just Apache's stock default
        # page, not something we control without patching the image (which
        # would defeat the point of using the ready-made upstream build at
        # all). Same "small proxy-side nudge" shape as the Nextcloud
        # user_saml fix below rather than a custom Dockerfile.
        return Response(status_code=302, headers={"Location": "visualizer/"})
    touch = _touch_asset(path)
    if touch is not None:
        return touch
    port = _instance_port(app_id, user)
    if port is None:
        log.warning("HTTP %s /%s user=%s → no running instance", request.method, path, user)
        return Response("app not running", status_code=502)
    target = f"http://127.0.0.1:{port}/{_upstream_path(app_id, path)}"
    # The registry authenticates the caller from Docker's own credentials, so
    # they must survive this hop — see _fwd_headers. The same predicate that let
    # the request past the identity gate decides this, so the two can never
    # disagree about which paths the app owns.
    from .main import _is_app_authenticated_path      # deferred: circular import
    # Computed once: it decides both what we send upstream (the client's own
    # credentials) and what we send back (an untouched body with its length).
    _appauth = _is_app_authenticated_path(app_id, path)
    fwd = _fwd_headers(registry.APPS.get(app_id), request, user, role,
                       keep_client_auth=_appauth)
    client = _get_client()
    req = client.build_request(request.method, target, params=dict(request.query_params),
                               headers=fwd, content=await request.body())
    try:
        # stream=True: don't read the body yet. Rewriting HTML (below) genuinely
        # needs the whole thing in memory first, but most traffic through here —
        # in particular a chat completion's token-by-token SSE stream — doesn't,
        # and reading it all here before forwarding ANY of it used to turn real-
        # time streaming into "wait for the whole response, then dump it all on
        # the browser at once" (confirmed live: Open WebUI chat responses arrived
        # in 2-3 big chunks instead of smoothly token-by-token). Content-Type is
        # available on `r` immediately, before the body is touched, so we can
        # still decide per-response which path to take.
        r = await client.send(req, stream=True)
    except Exception as e:  # noqa: BLE001
        log.exception("HTTP %s /%s → upstream error", request.method, path)
        return Response(f"upstream error: {e}", status_code=502)
    log.info("HTTP %s /%s user=%s → %s", request.method, path, user, r.status_code)
    app = registry.APPS.get(app_id)
    streamed = app.streamed if app else True
    # Registry responses go back byte-for-byte, headers included.
    #
    # Content-Length is hop-by-hop for our purposes everywhere else — we rewrite
    # HTML, so a length copied from upstream would be wrong. A registry body is
    # never rewritten, and Docker REQUIRES the length: without it the response
    # becomes chunked and `docker pull` fails with "missing or empty
    # Content-Length header". Push succeeds either way, which makes this look
    # like a broken pull rather than a stripped response header.
    _resp_drop = _HOP - {"content-length"} if _appauth else _HOP
    out = {k: v for k, v in r.headers.items()
           if k.lower() not in _resp_drop and k.lower() != "set-cookie"
           and (not streamed or k.lower() not in _STRIP_RESP)}
    # Never allow the proxy response to be cached. For streamed apps this was
    # already required (stale paths broke the proxy). For web apps, passing
    # through the upstream Cache-Control caused the browser to disk-cache a
    # brotli-encoded response as "plain" HTML (SM stripped Content-Encoding but
    # couldn't decompress without brotlicffi) — browser rendered binary garbage
    # and kept serving it from cache indefinitely, bypassing all fixes.
    if not _appauth:
        # Not for the registry: Docker relies on the upstream's own caching
        # semantics for layers, and forcing no-store re-downloads every blob.
        out["Cache-Control"] = "no-store, no-cache, must-revalidate"
    ct = (r.headers.get("content-type") or "").lower()

    # Everything below needs the full body in memory to rewrite it — genuinely
    # can't be done chunk-by-chunk. Keep this list exactly as narrow as the
    # rewrites that actually need it; anything else falls through to the real
    # streaming path further down.
    needs_rewrite = (
        "text/html" in ct
        or (streamed and path.rstrip("/") == "turn" and "json" in ct)
        or (app_id == "paraview" and path.rstrip("/") == "paraview")
    )

    if needs_rewrite:
        content = await r.aread()
        await r.aclose()
        # user_saml under OVERWRITEWEBROOT mis-generates its own route URLs: the app
        # segment `apps/user_saml` comes out as the corrupt `index.php_saml`, so the
        # first (unauthenticated) load 302s into a 404 even though the SSO session was
        # just established. Nextcloud serves the correct `apps/user_saml/...` path, so
        # repair the redirect target it emits. Scoped to the exact corrupt token, which
        # never appears in a legitimate URL.
        for k in list(out):
            if k.lower() == "location" and "index.php_saml" in out[k]:
                out[k] = out[k].replace("index.php_saml", "apps/user_saml")
        if "text/html" in ct:
            if not (app and app.own_subdomain):
                content = _rewrite_base_href(content, app_id)
            if app and app.native_pwa:
                if app.native_pwa_apple_icon:
                    content = _inject_apple_touch_icon(content, app.native_pwa_apple_icon)
            else:
                content = _inject_pwa(content, app)   # make the popped-out app its own PWA
            if app_id == "filebrowser":
                content = _inject_fb_theme_picker(content)
            if streamed:
                content = _inject_touch(content)  # mobile gestures for the stream
        if streamed and path.rstrip("/") == "turn" and "json" in ct:
            content = _inject_extra_turn(content)
        if app_id == "paraview" and path.rstrip("/") == "paraview":
            # The launcher mislabels its own JSON response as text/html (an old
            # Twisted-library quirk) — confirmed live, so this can't be gated on
            # content-type; gate on the exact launcher endpoint path instead.
            # The regex only matches the one literal string it's looking for, so
            # this is a harmless no-op on any response that isn't what we expect.
            content = _rewrite_paraview_session(content, request)
        resp = Response(content=content, status_code=r.status_code, headers=out,
                        media_type=r.headers.get("content-type"))
    else:
        # True streaming passthrough — SSE chat completions, plain JSON, binary
        # assets, everything that doesn't need rewriting. Bytes reach the
        # browser as they arrive from upstream instead of being held here until
        # the whole response is done.
        async def _relay():
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
            finally:
                await r.aclose()
        resp = StreamingResponse(_relay(), status_code=r.status_code, headers=out,
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


# Live client signalling sockets, keyed (app_id, user, selkies_peer_id).
#
# A streamed instance serves exactly ONE browser: Selkies' signalling assigns
# FIXED peer ids — 0/2 for the server's own video/audio pipelines, 1/3 for the
# browser's — and its hello_peer() refuses any uid already registered. So a
# second browser (another device, a forgotten tab, a left-open PWA window)
# permanently holds 1/3, and every later client is closed the instant it says
# HELLO, retrying forever. Browser-side that reads as "Registering with server,
# peer ID: 1" / "Server closed connection" on a loop, which the UI shows as
# flickering between "waiting for stream" and "registering" — with a perfectly
# healthy container, healthy signalling and correct SDP. Cost most of a day to
# find (2026-08-17); the only recovery was restarting the container by hand.
#
# Latest client wins: opening the app somewhere new takes over the session,
# which is what a single-user streamed desktop should do — reconnecting from
# another device should never deadlock your own app. Only the SERVER's
# pipelines talk to the signalling server directly (in-container, never through
# this proxy), so everything tracked here is by definition a browser.
_signalling_clients: dict[tuple[str, str, str], dict] = {}


def _hello_uid(text: str) -> str | None:
    """The peer id out of Selkies' 'HELLO <uid> [<b64 meta>]' handshake, or None
    if this isn't that handshake (any other ws traffic passes through untouched)."""
    toks = text.split(maxsplit=2)
    return toks[1] if len(toks) >= 2 and toks[0] == "HELLO" else None


async def _evict_previous(key: tuple[str, str, str]) -> None:
    """Close the client currently holding this peer id, so the id is free
    upstream before the new client's HELLO arrives.

    Closing its client socket is what unwinds it: the old handler's receive()
    returns a disconnect, which breaks its pump loop and closes its upstream in
    its own `finally`. The upstream is closed here too, because the peer id is
    only released when the SIGNALLING connection drops — waiting on the old
    handler to notice would race the new HELLO.
    """
    prev = _signalling_clients.pop(key, None)
    if not prev:
        return
    log.info("WS evicting previous client for %s (peer id %s)", key[0], key[2])
    for closer in (prev.get("client"), prev.get("upstream")):
        if closer is None:
            continue
        try:
            await closer.close()
        except Exception:  # noqa: BLE001
            pass


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
    # Take the client's first frame BEFORE opening upstream, so a Selkies HELLO
    # can be read and any previous holder of that peer id evicted while the id
    # is still free. Doing it after would race: the old client still holds the
    # id when the new HELLO lands, which is the deadlock this exists to prevent.
    # The frame is replayed upstream below, so nothing is swallowed.
    first_text: str | None = None
    key: tuple[str, str, str] | None = None
    if streamed and "signalling" in path:
        try:
            first = await client_ws.receive()
        except Exception:  # noqa: BLE001
            return
        if first["type"] == "websocket.disconnect":
            return
        first_text = first.get("text")
        uid = _hello_uid(first_text) if first_text else None
        if uid:
            key = (app_id, user, uid)
            await _evict_previous(key)

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

    if key is not None:
        _signalling_clients[key] = {"client": client_ws, "upstream": upstream}
    if first_text is not None:
        await upstream.send(first_text)

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
        # Only clear the registry if we are still the CURRENT holder — an
        # eviction already replaced the entry with the new client, and removing
        # it here would drop that live client's own bookkeeping instead.
        if key is not None and _signalling_clients.get(key, {}).get("client") is client_ws:
            _signalling_clients.pop(key, None)
        await upstream.close()
        try:
            await client_ws.close()
        except Exception:  # noqa: BLE001
            pass
