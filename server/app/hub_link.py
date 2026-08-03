"""Reverse link — dial the Hub and hold the socket, so nothing has to reach in.

The Hub normally drives this node by connecting to its :8170. That needs an
inbound port, which is precisely what is unavailable on a machine whose
firewall belongs to someone else: a work or university server where the rule
is "run what you like, but do not touch the firewall or networking". There the
tunnel comes up, SSH answers, and :8170 is rejected — the node installs
cleanly and never appears in the fleet.

So this dials OUT instead. It opens a WebSocket to the Hub, waits for request
frames, runs each one against this node's own HTTP API on loopback, and sends
the response back up the same socket. Nothing inbound is ever needed.

Deliberately narrow: it carries the HTTP control plane only. WireGuard still
carries NFS, and SSH-driven features (peer-install, auto-update) still use
SSH — both are already outbound from this node's side and never needed an
inbound port either.

Requests are executed by calling our own loopback API rather than dispatching
into the ASGI app directly. That keeps the node's real auth path intact: the
Hub forwards the caller's session cookie, and main.py validates it exactly as
it would for a direct request. A relayed request is therefore no more trusted
than the same request arriving over the LAN.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx

from . import config

log = logging.getLogger("sm.hub_link")

# Set by install/provisioning, not by hand. Absent = feature off, which is the
# right default: a node the Hub can already reach inbound needs none of this.
LINK_TOKEN = os.environ.get("SM_LINK_TOKEN", "")
LINK_HUB = os.environ.get("SM_LINK_HUB", "") or config.HUB_URL

_RECONNECT_MIN = 2.0
_RECONNECT_MAX = 60.0
# Long enough not to add chatter, short enough that a silently dropped socket
# (corporate middleboxes love to reap idle connections) is noticed and redialled
# rather than leaving the Hub thinking this node is reachable when it is not.
_PING_INTERVAL = 30.0

_task: asyncio.Task | None = None


def _ws_url() -> str:
    base = (LINK_HUB or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    node_id = config.LAN_IP
    return f"{base}/api/fleet/link?node_id={node_id}&token={LINK_TOKEN}"


async def _handle(frame: dict, client: httpx.AsyncClient) -> dict:
    """Run one relayed request against our own API and shape the reply."""
    req_id = frame.get("id")
    method = (frame.get("method") or "GET").upper()
    path = frame.get("path") or "/"
    cookies = frame.get("cookies") or {}
    body = frame.get("body")
    url = f"http://127.0.0.1:{config.SM_PORT}{path}"
    try:
        if method == "POST":
            r = await client.post(url, json=body, cookies=cookies, timeout=110.0)
        else:
            r = await client.get(url, cookies=cookies, timeout=110.0)
        try:
            parsed = r.json()
        except ValueError:
            parsed = r.text
        return {"t": "res", "id": req_id, "status": r.status_code, "body": parsed}
    except Exception as e:  # noqa: BLE001
        # Report as a reply rather than dropping it: the Hub is blocked on this
        # id, and an error it can surface beats waiting out the timeout.
        return {"t": "res", "id": req_id, "status": 502, "body": None,
                "error": f"local request failed: {e}"}


def _ssl_ctx():
    """None for ws://, else a context matching hub_auth's policy.

    The Hub terminates TLS with its own internal CA, which a node has no reason
    to have in its trust store, so SM_HUB_VERIFY_TLS is false on every node in
    practice. Same trade-off hub_auth.py already makes for identity checks --
    made the same way here rather than differently, so there is one policy to
    reason about. The link's own authentication does not rest on TLS: the node
    proves itself with a token the Hub generated and placed on it over SSH.
    """
    if not _ws_url().startswith("wss://"):
        return None
    import ssl
    if config.HUB_VERIFY_TLS:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _session() -> None:
    """One connection attempt, held until it drops."""
    import websockets

    url = _ws_url()
    async with websockets.connect(url, max_size=32 * 1024 * 1024,
                                  ssl=_ssl_ctx(),
                                  open_timeout=20, close_timeout=5) as ws:
        log.info("reverse link established to %s", LINK_HUB)
        send_lock = asyncio.Lock()

        async def _keepalive() -> None:
            while True:
                await asyncio.sleep(_PING_INTERVAL)
                async with send_lock:
                    await ws.send(json.dumps({"t": "ping"}))

        ka = asyncio.create_task(_keepalive())
        # One client for the whole session; the Hub can have several requests
        # in flight and each would otherwise pay connection setup.
        async with httpx.AsyncClient() as client:
            try:
                while True:
                    raw = await ws.recv()
                    try:
                        frame = json.loads(raw)
                    except ValueError:
                        continue
                    if frame.get("t") == "pong":
                        continue
                    if frame.get("t") != "req":
                        continue

                    async def _run(f: dict) -> None:
                        res = await _handle(f, client)
                        async with send_lock:
                            await ws.send(json.dumps(res))

                    # Concurrent: a slow request must not block the ones behind
                    # it, or one 90-second image pull stalls the whole link.
                    asyncio.create_task(_run(frame))
            finally:
                ka.cancel()


async def _loop() -> None:
    delay = _RECONNECT_MIN
    while True:
        try:
            await _session()
            delay = _RECONNECT_MIN      # a clean session resets the backoff
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("reverse link down (%s); retrying in %.0fs", e, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, _RECONNECT_MAX)


def start() -> None:
    """Begin dialling, if this node has been provisioned for it."""
    global _task
    if not LINK_TOKEN:
        return
    if not LINK_HUB:
        log.warning("SM_LINK_TOKEN is set but no Hub URL is known; reverse link disabled")
        return
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    log.info("reverse link enabled, dialling %s", LINK_HUB)


def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None


def status() -> dict:
    return {"enabled": bool(LINK_TOKEN),
            "hub": LINK_HUB if LINK_TOKEN else None,
            "running": bool(_task and not _task.done())}
