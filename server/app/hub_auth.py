"""Hub SSO — validate the SandOS Hub's `hub_session` cookie.

When SM_HUB_URL is configured, the Server Manager identifies the user by asking
the Hub who owns the presented session (GET {HUB_URL}/api/auth/me with the
hub_session cookie forwarded). The Hub returns 200 + {username,...} for a valid
session, or 401. No shared secret or DB — the Hub stays the identity authority.
"""
from __future__ import annotations
import json
import ssl
import threading
import time
import urllib.error
import urllib.request

from . import config

_INSECURE_CTX = ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = ssl.CERT_NONE

# Short-TTL identity cache. Every proxied request (incl. each static asset on a
# Nextcloud page — dozens per page) is auth-checked here, and each miss is a
# blocking TLS round-trip to the Hub (~15-20ms). Without this, one page = dozens
# of Hub calls and navigations re-pay it every time. Caching by token collapses a
# page's request burst + repeat navigations into ~one Hub call per TTL. Trade-off:
# a revoked/logged-out session lingers on already-open app tabs for up to the TTL.
_POS_TTL = 30.0   # trust a valid identity this long without re-asking the Hub
_NEG_TTL = 3.0    # keep negatives brief so a fresh login isn't blocked
_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return bool(config.HUB_URL)


def _fetch_identity(token: str) -> dict | None:
    """Ask the Hub who owns this session (the authoritative, uncached path).

    Uses HUB_INTERNAL_URL (defaults to the public HUB_URL) so a trusted node can
    opt into a faster plain-HTTP LAN path while off-LAN/WireGuard nodes keep the
    secure HTTPS default — see config.HUB_INTERNAL_URL."""
    base = config.HUB_INTERNAL_URL
    req = urllib.request.Request(
        f"{base}/api/auth/me",
        headers={"Cookie": f"{config.HUB_SESSION_COOKIE}={token}"},
    )
    ctx = None
    if base.startswith("https") and not config.HUB_VERIFY_TLS:
        ctx = _INSECURE_CTX
    try:
        with urllib.request.urlopen(req, timeout=4, context=ctx) as r:
            data = json.loads(r.read().decode()) or {}
    except urllib.error.HTTPError:
        return None  # 401 Not authenticated
    except Exception:
        return None  # Hub unreachable, etc.
    if not data.get("username"):
        return None
    return {
        "username": data["username"],
        "role": data.get("role") or "viewer",
        "grants": data.get("grants") or [],
    }


def verify_identity(token: str) -> dict | None:
    """Return the Hub identity {username, role, grants} for a valid session, else None.

    `role` is admin | viewer | scoped; `grants` is the scoped account's resolved
    permission keys (e.g. "app.freecad"). Both come straight from the Hub's
    /api/auth/me (cached briefly) — the Hub remains the identity + permission
    authority."""
    if not config.HUB_URL or not token:
        return None
    now = time.monotonic()
    with _lock:
        hit = _cache.get(token)
        if hit and hit[0] > now:
            return hit[1]
    ident = _fetch_identity(token)
    with _lock:
        _cache[token] = (now + (_POS_TTL if ident else _NEG_TTL), ident)
        if len(_cache) > 512:   # opportunistic sweep of expired entries
            for k in [k for k, v in _cache.items() if v[0] <= now]:
                _cache.pop(k, None)
    return ident


def verify_session(token: str) -> str | None:
    """Back-compat: return just the Hub username for a valid session, else None."""
    ident = verify_identity(token)
    return ident["username"] if ident else None
