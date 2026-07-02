"""Hub SSO — validate the SandOS Hub's `hub_session` cookie.

When SM_HUB_URL is configured, the Server Manager identifies the user by asking
the Hub who owns the presented session (GET {HUB_URL}/api/auth/me with the
hub_session cookie forwarded). The Hub returns 200 + {username,...} for a valid
session, or 401. No shared secret or DB — the Hub stays the identity authority.
"""
from __future__ import annotations
import json
import ssl
import urllib.error
import urllib.request

from . import config

_INSECURE_CTX = ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = ssl.CERT_NONE


def enabled() -> bool:
    return bool(config.HUB_URL)


def verify_identity(token: str) -> dict | None:
    """Return the Hub identity {username, role, grants} for a valid session, else None.

    `role` is admin | viewer | scoped; `grants` is the scoped account's resolved
    permission keys (e.g. "app.freecad"). Both come straight from the Hub's
    /api/auth/me — the Hub remains the identity + permission authority."""
    if not config.HUB_URL or not token:
        return None
    req = urllib.request.Request(
        f"{config.HUB_URL}/api/auth/me",
        headers={"Cookie": f"{config.HUB_SESSION_COOKIE}={token}"},
    )
    ctx = None
    if config.HUB_URL.startswith("https") and not config.HUB_VERIFY_TLS:
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


def verify_session(token: str) -> str | None:
    """Back-compat: return just the Hub username for a valid session, else None."""
    ident = verify_identity(token)
    return ident["username"] if ident else None
