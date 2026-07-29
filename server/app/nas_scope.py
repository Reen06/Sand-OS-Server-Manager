"""What this node is allowed to see of the fleet NAS.

A trusted node mounts the whole export tree: `users/`, `shared/`, everything.
An app-only node's export is `fsid=0` on its OWN staging directory, so from its
side that directory *is* the root of the filesystem — `:/users/<name>` does not
resolve at all, and a container asking for one fails to start with a bare
"no such file or directory".

That is the fact this module exists to communicate. Mount resolution has to know
which kind of node it is running on before it can name a path that exists here.

The answer comes from the Hub, which is the authority on trust levels — the node
does not get to decide how much of the NAS it may see.
"""
from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.request

from . import config

# Trust rarely changes, and every app launch asks. Long enough to keep launches
# cheap, short enough that a revocation lands without a restart; the export
# itself is revoked immediately on the NAS host regardless, so this TTL only
# affects which PATH we ask for, never what we are permitted to read.
_TTL = 300
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "staging": None, "known": False}


def _fetch() -> tuple[bool, str | None]:
    """(known, staging_name). `known` is False when the Hub could not be asked."""
    hub = (config.HUB_URL or "").rstrip("/")
    if not hub:
        return False, None
    ctx = ssl._create_unverified_context() if hub.startswith("https") else None
    try:
        with urllib.request.urlopen(f"{hub}/api/fleet/nas-policy",
                                    timeout=10, context=ctx) as r:
            policy = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return False, None
    me = (config.LAN_IP or "").strip()
    for entry in policy.get("app_only") or []:
        if (entry.get("addr") or "").strip() == me:
            return True, entry.get("staging") or config.NODE_NAME
    return True, None          # answered, and we are not app-only


def staging_name(refresh: bool = False) -> str | None:
    """This node's staging directory name, or None if it mounts the whole tree.

    Returns None when the Hub cannot be reached, which deliberately means "carry
    on as a trusted node". The two ways to be wrong are not equal: guessing
    trusted on an app-only node makes mounts fail loudly at launch, while
    guessing app-only on a trusted node would silently hand an app an empty
    staging directory in place of the user's real files. A visible failure is
    the recoverable one.
    """
    now = time.time()
    with _lock:
        if not refresh and _cache["known"] and now - _cache["at"] < _TTL:
            return _cache["staging"]
    known, staging = _fetch()
    with _lock:
        if known:
            _cache.update({"at": now, "staging": staging, "known": True})
        return staging if known else _cache["staging"]


def is_app_only() -> bool:
    return staging_name() is not None
