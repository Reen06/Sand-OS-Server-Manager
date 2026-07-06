"""Fleet NAS — shared-folder manager.

A shared folder is a directory under ``{NAS_ROOT}/shared/<name>`` on the NAS host,
surfaced in Nextcloud as its own External Storage mount whose *applicable users*
decide who sees it. Members empty ⇒ everyone; naming specific users restricts it.

The SM runs on the NAS host (a control-owned tree), so it creates/removes the
directories directly and drives Nextcloud through ``occ`` in the shared instance's
container. Apps (FreeCAD, Filebrowser) still reach the same bytes over NFS — this
only governs per-user *visibility* inside Nextcloud, which is where the request
("pick which users see them") lives.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from . import config

# Folder names become both a directory and a Nextcloud mount point, so keep them
# to a safe, human set (no separators, no dotfiles, no traversal).
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


def _shared_root() -> str:
    return os.path.join(config.NAS_ROOT, config.NAS_SHARED_SUBPATH)


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_OK.match(name) or name in (".", ".."):
        raise ValueError("Use letters, numbers, spaces, dashes or underscores (max 64).")
    return name


def _occ(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run an occ command in the Nextcloud container as www-data."""
    return subprocess.run(
        ["docker", "exec", "-u", "www-data", config.NC_CONTAINER,
         "php", "/var/www/html/occ", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _mounts() -> list[dict]:
    try:
        r = _occ("files_external:list", "--all", "--output=json")
        return json.loads(r.stdout or "[]")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []


def _shared_mounts() -> dict[str, dict]:
    """Map folder name → its Nextcloud mount, for per-folder shared mounts only
    (datadir under /<shared>/<name>). Skips the per-user "My Files" and any legacy
    blanket "/<shared>" mount."""
    base = f"/{config.NAS_SHARED_SUBPATH}"
    out: dict[str, dict] = {}
    for m in _mounts():
        datadir = (m.get("configuration") or {}).get("datadir", "")
        if datadir.startswith(base + "/"):
            name = datadir[len(base) + 1:].strip("/")
            if name and "/" not in name:
                out[name] = m
    return out


def _extract_id(stdout: str) -> int | None:
    m = re.search(r"\b(\d+)\b", stdout or "")
    return int(m.group(1)) if m else None


def _set_members(mount_id: int, members: list[str], current: list[str] | None = None) -> None:
    """Reconcile applicable users: add the new, remove the departed. An empty
    member set leaves the mount global (visible to everyone)."""
    members = sorted({u for u in (members or []) if u})
    current = current or []
    for user in current:
        if user not in members:
            _occ("files_external:applicable", str(mount_id), "--remove-user", user)
    for user in members:
        if user not in current:
            _occ("files_external:applicable", str(mount_id), "--add-user", user)


# ── public API ────────────────────────────────────────────────────────────────
def list_users() -> list[str]:
    try:
        r = _occ("user:list", "--output=json")
        return sorted(json.loads(r.stdout or "{}").keys())
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []


def list_shared() -> list[dict]:
    root = _shared_root()
    mounts = _shared_mounts()
    folders = []
    for name, m in mounts.items():
        path = os.path.join(root, name)
        folders.append({
            "name": name,
            "members": m.get("applicable_users") or [],   # [] ⇒ everyone
            "everyone": not (m.get("applicable_users")),
            "mount_id": m.get("mount_id"),
            "exists": os.path.isdir(path),
        })
    return sorted(folders, key=lambda f: f["name"].lower())


def create_shared(name: str, members: list[str]) -> dict:
    name = _safe_name(name)
    if name in _shared_mounts():
        raise ValueError("A shared folder with that name already exists.")
    path = os.path.join(_shared_root(), name)
    os.makedirs(path, exist_ok=True)
    try:                                    # match the all_squash owner every app maps to
        os.chown(path, config.NAS_UID, config.NAS_GID)
    except (PermissionError, OSError):
        pass
    r = _occ("files_external:create", f"/{name}", "local", "null::null",
             "-c", f"datadir=/{config.NAS_SHARED_SUBPATH}/{name}")
    mount_id = _extract_id(r.stdout)
    if mount_id is None:
        raise ValueError(f"Nextcloud rejected the mount: {(r.stderr or r.stdout).strip()[:200]}")
    _occ("files_external:option", str(mount_id), "filesystem_check_changes", "1")
    _set_members(mount_id, members)
    return {"name": name, "members": sorted(set(members or [])), "mount_id": mount_id}


def set_members(name: str, members: list[str]) -> dict:
    name = _safe_name(name)
    m = _shared_mounts().get(name)
    if not m:
        raise ValueError("No such shared folder.")
    _set_members(m["mount_id"], members, current=m.get("applicable_users") or [])
    return {"name": name, "members": sorted(set(members or []))}


def delete_shared(name: str, delete_files: bool = False) -> dict:
    name = _safe_name(name)
    m = _shared_mounts().get(name)
    if m:
        _occ("files_external:delete", "-y", str(m["mount_id"]))
    if delete_files:
        path = os.path.join(_shared_root(), name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    return {"name": name, "deleted_files": delete_files}
