"""Per-instance file staging for app-only nodes.

An app-only node mounts nothing but its own staging directory (see
containers/nfs-server/run-nas.sh). This module is what puts files there and,
more importantly, takes them away again.

The point is exposure minimisation, not prevention. An app that processes a file
receives that file, and a root administrator on the node running it can read
that file while the job is live. What staging changes is HOW MUCH is exposed
(one job's files, not the library) and FOR HOW LONG (while the job runs, not
permanently). Anything stronger has to run on a trusted host — do not describe
this as isolation it is not.

Only ever runs on the node that hosts the NAS: it manipulates the real export
tree directly.
"""
from __future__ import annotations

import os
import shutil
import time

from . import config
from .files import _safe_user

# A staging directory belongs to one node and one app instance, so a second app
# on the same node cannot see the first one's files.
_STAGING = "staging"


def _safe_component(name: str) -> str:
    """One path segment, with no way out of it.

    Rejects rather than sanitises anything with a separator in it: a caller
    passing "../.." is not a naming style to be cleaned up, it is a traversal
    attempt, and silently rewriting it hides that.
    """
    n = (name or "").strip()
    if not n or n in (".", "..") or "/" in n or "\\" in n or "\x00" in n:
        raise ValueError(f"unsafe path component: {name!r}")
    return n


def _node_dir(node: str) -> str:
    return os.path.join(config.NAS_ROOT, _STAGING, _safe_component(node))


def instance_dir(node: str, instance: str) -> str:
    return os.path.join(_node_dir(node), _safe_component(instance))


def _resolve_source(user: str, rel: str) -> str:
    """Resolve a caller-supplied path inside the user's own NAS home.

    realpath-then-verify rather than string checks: a symlink inside the user's
    home pointing at /etc or at another user's tree would pass any amount of
    "does it start with the right prefix" inspection of the raw string, and the
    files being staged are exactly the kind a user could have been tricked into
    creating.
    """
    home = os.path.realpath(
        os.path.join(config.NAS_ROOT, config.NAS_USERS_SUBPATH, _safe_user(user)))
    target = os.path.realpath(os.path.join(home, (rel or "").lstrip("/")))
    if target != home and not target.startswith(home + os.sep):
        raise ValueError(f"path escapes the user's home: {rel!r}")
    if not os.path.isfile(target):
        raise ValueError(f"not a file: {rel!r}")
    return target


def stage(node: str, instance: str, user: str, paths: list[str]) -> dict:
    """Copy the named files into this instance's staging directory.

    Copies rather than links or bind-mounts: a hardlink would let the node write
    through to the original, and the whole point is that what lands there is a
    working copy the node may have, not the library it may not.
    """
    dest = instance_dir(node, instance)
    os.makedirs(dest, exist_ok=True)
    staged, failed = [], []
    for rel in paths or []:
        try:
            src = _resolve_source(user, rel)
            name = _safe_component(os.path.basename(src))
            shutil.copy2(src, os.path.join(dest, name))
            staged.append({"source": rel, "name": name,
                           "bytes": os.path.getsize(src)})
        except (ValueError, OSError) as e:
            failed.append({"source": rel, "error": str(e)})
    # Record who this belongs to so write-back knows where results go, and so a
    # leftover directory can be explained rather than just found.
    try:
        with open(os.path.join(dest, ".sandos-staging.json"), "w") as f:
            import json
            json.dump({"node": node, "instance": instance, "user": user,
                       "staged_at": int(time.time()),
                       "files": [s["name"] for s in staged]}, f, indent=2)
    except OSError:
        pass
    return {"dir": dest, "staged": staged, "failed": failed}


def collect(node: str, instance: str, user: str) -> dict:
    """Copy anything the app produced back into the user's home, then clear.

    Results land in a dated subfolder rather than over the originals: an app
    writing a mangled file must not be able to destroy the input it was given,
    and there is no way to tell from here whether an overwrite was intended.
    """
    src_dir = instance_dir(node, instance)
    if not os.path.isdir(src_dir):
        return {"collected": [], "cleared": False}
    home = os.path.realpath(
        os.path.join(config.NAS_ROOT, config.NAS_USERS_SUBPATH, _safe_user(user)))
    out = os.path.join(home, "app-results",
                       f"{_safe_component(instance)}-{time.strftime('%Y%m%d-%H%M%S')}")
    collected = []
    try:
        os.makedirs(out, exist_ok=True)
        for name in sorted(os.listdir(src_dir)):
            if name == ".sandos-staging.json":
                continue
            s = os.path.join(src_dir, name)
            if os.path.isfile(s):
                shutil.copy2(s, os.path.join(out, name))
                collected.append(name)
    except OSError:
        pass
    if not collected:
        # Nothing produced — don't leave an empty dated folder behind.
        try:
            os.rmdir(out)
        except OSError:
            pass
    return {"collected": collected, "out": out if collected else None}


def clear(node: str, instance: str) -> dict:
    """Remove this instance's staging directory. Idempotent."""
    d = instance_dir(node, instance)
    existed = os.path.isdir(d)
    shutil.rmtree(d, ignore_errors=True)
    return {"cleared": existed, "dir": d}


def list_staged(node: str | None = None) -> list[dict]:
    """What is currently exposed, and to whom — the answer to 'what can that
    box see right now', which should never require reading a filesystem by hand.
    """
    root = os.path.join(config.NAS_ROOT, _STAGING)
    out: list[dict] = []
    if not os.path.isdir(root):
        return out
    for n in sorted(os.listdir(root)):
        if node and n != node:
            continue
        ndir = os.path.join(root, n)
        if not os.path.isdir(ndir):
            continue
        for inst in sorted(os.listdir(ndir)):
            idir = os.path.join(ndir, inst)
            if not os.path.isdir(idir):
                continue
            files = [f for f in sorted(os.listdir(idir))
                     if f != ".sandos-staging.json"]
            meta = {}
            try:
                import json
                with open(os.path.join(idir, ".sandos-staging.json")) as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                pass
            out.append({"node": n, "instance": inst, "files": files,
                        "user": meta.get("user"), "staged_at": meta.get("staged_at")})
    return out
