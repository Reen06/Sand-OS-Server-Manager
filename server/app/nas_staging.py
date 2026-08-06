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

# Where a node's write-backs land. One well-known folder, so a file that appeared
# from an app is always explicable — rather than results scattered through the
# home with nothing marking where they came from.
_DEPOSIT_DIR = "Saved from apps"


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


def _staging_root() -> str:
    """Where staged files live.

    On the mesh, not the NAS host's own tree. Staging is only useful if the node
    the files are FOR can read it, and since the mesh migration that means the
    cluster mount every node shares — the local tree is visible to the NAS host
    alone. It previously sat there and was reached over NFS, a route that never
    completed a single mount, which is why staging has never actually delivered
    a file to a node.
    """
    return os.path.join(config.nas_data_root(), _STAGING)


def _node_dir(node: str) -> str:
    return os.path.join(_staging_root(), _safe_component(node))


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
        os.path.join(config.nas_data_root(), config.NAS_USERS_SUBPATH, _safe_user(user)))
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
        os.path.join(config.nas_data_root(), config.NAS_USERS_SUBPATH, _safe_user(user)))
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


def clear_node(node: str) -> dict:
    """Remove a node's entire staging tree — every instance under it.

    For decommissioning: revoking the export stops the node reaching these
    files, but leaving them on disk still leaves someone else's documents lying
    in a directory named after a machine that no longer exists.
    """
    d = _node_dir(node)
    existed = os.path.isdir(d)
    shutil.rmtree(d, ignore_errors=True)
    return {"cleared": existed, "dir": d}


def list_staged(node: str | None = None) -> list[dict]:
    """What is currently exposed, and to whom — the answer to 'what can that
    box see right now', which should never require reading a filesystem by hand.
    """
    root = _staging_root()
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


def prune_orphans(known_nodes: list[str]) -> list[str]:
    """Delete staging trees belonging to nodes the fleet no longer knows about.

    This is how a decommissioned node's staged files actually get removed. The
    Hub cannot delete them at deregistration time: a node running its own
    uninstaller has no Hub session, so the Hub has no credential to call the NAS
    host with, and the call comes back 401. Here the NAS host is acting on its
    own data, so no credential is involved.

    Refuses to do anything on an empty roster. An empty list is what a failed
    Hub fetch, a half-written response or a Hub with its registry wiped all look
    like, and acting on it would delete every staged file on the NAS — including
    files a running app is mid-way through using. Deleting nothing when the
    answer is unclear is always recoverable; the reverse is not.
    """
    if not known_nodes:
        return []
    root = _staging_root()
    if not os.path.isdir(root):
        return []
    keep = {n.strip() for n in known_nodes if n and n.strip()}
    # Case-insensitively too: a node stages into its raw NODE_NAME while the Hub
    # generates the export path from a lowercased, sanitised one. Matching only
    # exactly would delete a live node's files whenever the two spellings differ.
    keep_lower = {n.lower() for n in keep}
    removed = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        if name in keep or name.lower() in keep_lower:
            continue
        shutil.rmtree(d, ignore_errors=True)
        removed.append(name)
    return removed


def deposit(user: str, filename: str, data: bytes, subdir: str = "") -> dict:
    """Write one file into a user's NAS home on behalf of a node.

    This is the write half of brokered access, and it is deliberately not the
    mirror of the read half. Reading is scoped — an app-only node sees just its
    staging directory — but producing a result should not require the node to
    see anything at all. A node with no read access whatsoever can still hand
    back a finished file, because the NAS host does the write itself; the node
    never holds a writable mount over the user's files.

    Lands under `Saved from apps/` rather than at the root of the home, and
    never overwrites: a colliding name gains a numeric suffix. A node that can
    silently replace an arbitrary path in someone's home is a node that can
    destroy their library one file at a time, which is exactly the power the
    read scoping was put in place to withhold.
    """
    home = os.path.realpath(
        os.path.join(config.nas_data_root(), config.NAS_USERS_SUBPATH, _safe_user(user)))
    if not os.path.isdir(home):
        raise ValueError(f"no NAS home for {user!r}")
    dest_dir = os.path.join(home, _DEPOSIT_DIR)
    if subdir:
        # One level, and only a name — not a path. A caller passing "a/b" or
        # ".." is not choosing a layout, it is trying to leave the folder.
        dest_dir = os.path.join(dest_dir, _safe_component(subdir))
    dest_dir = os.path.realpath(dest_dir)
    if dest_dir != home and not dest_dir.startswith(home + os.sep):
        raise ValueError("destination escapes the user's home")
    os.makedirs(dest_dir, exist_ok=True)

    name = _safe_component(os.path.basename(filename or "untitled"))
    stem, ext = os.path.splitext(name)
    final = os.path.join(dest_dir, name)
    n = 1
    while os.path.exists(final):
        final = os.path.join(dest_dir, f"{stem} ({n}){ext}")
        n += 1
    with open(final, "wb") as f:
        f.write(data)
    return {"ok": True, "name": os.path.basename(final),
            "path": os.path.relpath(final, home), "bytes": len(data)}
