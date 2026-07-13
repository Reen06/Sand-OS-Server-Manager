"""Per-user cloud file picker — backs the "Save/Open to Server" dialog used by
apps like Ray Optics (a save-into-your-profile flow, mirroring how FreeCAD
persists into the same per-user NAS home over NFS, but exposed here as a
pick-a-folder HTTP API for apps that have no OS-level file dialog to hook).

Roots:
  - "home"          — the user's private NAS folder (same bytes FreeCAD/
                       Filebrowser/Nextcloud already read/write for this user).
  - "shared:<name>" — one of nas.py's Fleet NAS shared folders, if this user is
                       a member (or the folder has no member list ⇒ everyone).
                       Saving here is how a scene "auto appears" to everyone
                       already sharing that folder — no separate grant model.

All paths are resolved with a realpath prefix check against the root's base
directory to block ".."/symlink escape before any read/write/listdir call.
"""
from __future__ import annotations

import os
import re

from . import config, nas


def _safe_user(user: str) -> str:
    """MUST match docker_backend._safe: the NFS home subpath is users/{safe},
    and the app containers (FreeCAD/Filebrowser) mount the lowercased form.
    A case-preserving version here split 'Braeden' into users/Braeden (picker)
    vs users/braeden (apps) — files no longer followed the user across apps."""
    return re.sub(r"[^a-z0-9]+", "-", (user or "").lower()).strip("-") or "user"


def _home_dir(user: str) -> str:
    path = os.path.join(config.NAS_ROOT, config.NAS_USERS_SUBPATH, _safe_user(user))
    os.makedirs(path, exist_ok=True)
    return path


def _shared_dir(name: str) -> str:
    return os.path.join(config.NAS_ROOT, config.NAS_SHARED_SUBPATH, name)


def list_roots(user: str) -> list[dict]:
    """Roots this user may browse: their home + any shared folder they're in."""
    roots = [{"id": "home", "label": "My Files"}]
    for folder in nas.list_shared():
        if not folder.get("exists"):
            continue
        members = folder.get("members") or []
        if members and user not in members:
            continue
        roots.append({"id": f"shared:{folder['name']}", "label": f"{folder['name']} (Shared)"})
    from . import usb_storage  # lazy: avoid import cycle
    roots.extend(
        {"id": r["id"], "label": r["label"]} for r in usb_storage.roots_for(user))
    return roots


def _root_base(root_id: str, user: str) -> str:
    roots = {r["id"] for r in list_roots(user)}
    if root_id not in roots:
        raise ValueError("no access to that folder")
    if root_id == "home":
        return _home_dir(user)
    if root_id.startswith("usb:"):
        from . import usb_storage
        for r in usb_storage.roots_for(user):
            if r["id"] == root_id:
                return r["path"]
        raise ValueError("USB drive no longer available")
    return _shared_dir(root_id[len("shared:"):])


def _resolve(root_id: str, user: str, rel_path: str) -> str:
    base = _root_base(root_id, user)
    parts = [p for p in (rel_path or "").strip("/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("invalid path")
    target = os.path.realpath(os.path.join(base, *parts)) if parts else os.path.realpath(base)
    base_real = os.path.realpath(base)
    if target != base_real and not target.startswith(base_real + os.sep):
        raise ValueError("invalid path")
    return target


def list_dir(root_id: str, user: str, rel_path: str) -> list[dict]:
    target = _resolve(root_id, user, rel_path)
    if not os.path.isdir(target):
        raise FileNotFoundError("not a directory")
    entries = []
    for name in sorted(os.listdir(target)):
        if name.startswith("."):
            continue
        full = os.path.join(target, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        entries.append({
            "name": name,
            "is_dir": os.path.isdir(full),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        })
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return entries


def read_file(root_id: str, user: str, rel_path: str) -> bytes:
    target = _resolve(root_id, user, rel_path)
    if not os.path.isfile(target):
        raise FileNotFoundError("no such file")
    with open(target, "rb") as f:
        return f.read()


def write_file(root_id: str, user: str, rel_path: str, data: bytes) -> None:
    target = _resolve(root_id, user, rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        f.write(data)
    try:  # match the all_squash owner every app maps to (see nas.py)
        os.chown(target, config.NAS_UID, config.NAS_GID)
    except (PermissionError, OSError):
        pass


def make_dir(root_id: str, user: str, rel_path: str) -> None:
    target = _resolve(root_id, user, rel_path)
    os.makedirs(target, exist_ok=True)
    try:
        os.chown(target, config.NAS_UID, config.NAS_GID)
    except (PermissionError, OSError):
        pass


def exists(root_id: str, user: str, rel_path: str) -> dict:
    """Whether `rel_path` already exists — used by the picker's Save flow to
    warn before a silent overwrite."""
    target = _resolve(root_id, user, rel_path)
    if not os.path.exists(target):
        return {"exists": False, "is_dir": False}
    return {"exists": True, "is_dir": os.path.isdir(target)}


def _reject_root(rel_path: str) -> None:
    """Refuse an operation on the root folder itself (empty rel_path) — never
    let rename/delete touch a user's home or a shared folder's top level."""
    if not (rel_path or "").strip("/"):
        raise ValueError("cannot modify the root folder")


def rename(root_id: str, user: str, rel_path: str, new_name: str) -> None:
    """Rename a file or folder within its current parent directory (no moving
    across folders — that's a separate feature; this backs the picker's
    inline rename action)."""
    if not new_name or "/" in new_name or new_name in (".", ".."):
        raise ValueError("invalid name")
    target = _resolve(root_id, user, rel_path)
    _reject_root(rel_path)
    if not os.path.exists(target):
        raise FileNotFoundError("no such file")
    dest = os.path.join(os.path.dirname(target), new_name)
    base_real = os.path.realpath(_root_base(root_id, user))
    dest_real = os.path.realpath(dest)
    if dest_real != base_real and not dest_real.startswith(base_real + os.sep):
        raise ValueError("invalid path")
    if os.path.exists(dest_real):
        raise ValueError("a file or folder with that name already exists")
    os.rename(target, dest_real)


def delete(root_id: str, user: str, rel_path: str) -> None:
    target = _resolve(root_id, user, rel_path)
    _reject_root(rel_path)
    if os.path.isdir(target):
        import shutil
        shutil.rmtree(target)
    elif os.path.isfile(target):
        os.remove(target)
    else:
        raise FileNotFoundError("no such file")
