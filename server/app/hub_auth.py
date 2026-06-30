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


def verify_session(token: str) -> str | None:
    """Return the Hub username for a valid hub_session token, else None."""
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
            return (json.loads(r.read().decode()) or {}).get("username")
    except urllib.error.HTTPError:
        return None  # 401 Not authenticated
    except Exception:
        return None  # Hub unreachable, etc.
