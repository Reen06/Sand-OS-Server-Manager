"""Per-user app snapshots + factory reset.

An app's per-user state is its NFS `.appdata/<mount-name>` dirs (e.g. FreeCAD's
~/.config and ~/.local/share — see the registry's named per-user mounts). Since
that state lives on the fleet NAS:

  snapshot  = tar the .appdata dirs into users/<u>/snapshots/<app>-<ts>.tar.gz
              — visible in Files/Nextcloud like any other file, and restorable
              on ANY node (the NAS is fleet-wide), which is what makes
              "install my configured FreeCAD on another server" work.
  restore   = stop the app, wipe .appdata dirs, untar a chosen snapshot.
  reset     = stop the app, wipe .appdata dirs (next launch = factory fresh).

User FILES (users/<u> outside .appdata) are never touched by any of these.
"""
from __future__ import annotations

import os
import re
import shutil
import tarfile
import time

from . import config, registry


def _safe(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def _user_root(user: str) -> str:
    return os.path.join(config.NAS_ROOT, config.NAS_USERS_SUBPATH, _safe(user))


def _appdata_dirs(app_id: str, user: str) -> list[tuple[str, str]]:
    """(mount-name, absolute .appdata path) for this app's named per-user mounts."""
    app = registry.APPS.get(app_id)
    if app is None:
        raise KeyError(app_id)
    out = []
    for m in app.mounts:
        if m.scope == "per-user" and getattr(m, "storage", "local") == "nfs" and m.name != "home":
            out.append((m.name, os.path.join(_user_root(user), ".appdata", _safe(m.name))))
    return out


def _snap_dir(user: str) -> str:
    path = os.path.join(_user_root(user), "snapshots")
    os.makedirs(path, exist_ok=True)
    return path


def has_appdata(app_id: str) -> bool:
    app = registry.APPS.get(app_id)
    return bool(app) and any(
        m.scope == "per-user" and getattr(m, "storage", "local") == "nfs" and m.name != "home"
        for m in app.mounts
    )


def list_snapshots(app_id: str, user: str) -> list[dict]:
    prefix = f"{_safe(app_id)}-"
    out = []
    root = _snap_dir(user)
    for name in sorted(os.listdir(root), reverse=True):
        if name.startswith(prefix) and name.endswith(".tar.gz"):
            st = os.stat(os.path.join(root, name))
            out.append({"file": name, "size": st.st_size, "mtime": int(st.st_mtime)})
    return out


def snapshot(app_id: str, user: str, label: str = "") -> dict:
    """Tar the app's .appdata dirs. The app keeps running (settings files are
    small and mostly quiescent); stop first for a guaranteed-consistent copy."""
    dirs = _appdata_dirs(app_id, user)
    if not dirs:
        raise ValueError(f"{app_id} has no per-user settings to snapshot")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"-{_safe(label)}" if label.strip() else ""
    file = f"{_safe(app_id)}-{stamp}{suffix}.tar.gz"
    path = os.path.join(_snap_dir(user), file)
    with tarfile.open(path, "w:gz") as tar:
        for name, dir_path in dirs:
            if os.path.isdir(dir_path):
                tar.add(dir_path, arcname=_safe(name))
    return {"file": file, "size": os.path.getsize(path)}


def _wipe(app_id: str, user: str) -> list[str]:
    wiped = []
    for _, dir_path in _appdata_dirs(app_id, user):
        if os.path.isdir(dir_path):
            for entry in os.listdir(dir_path):
                full = os.path.join(dir_path, entry)
                shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
            wiped.append(dir_path)
    return wiped


def reset(app_id: str, user: str) -> dict:
    """Factory defaults: stop the instance, wipe its .appdata. Files untouched."""
    registry.stop(app_id, user)
    return {"wiped": _wipe(app_id, user)}


def restore(app_id: str, user: str, file: str) -> dict:
    """Stop the app, replace its .appdata with a snapshot's content."""
    if "/" in file or ".." in file:
        raise ValueError("bad snapshot name")
    path = os.path.join(_snap_dir(user), file)
    if not os.path.isfile(path):
        raise FileNotFoundError(file)
    registry.stop(app_id, user)
    _wipe(app_id, user)
    by_name = {name: dir_path for name, dir_path in _appdata_dirs(app_id, user)}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            top = member.name.split("/", 1)[0]
            target_root = by_name.get(top) or by_name.get(top.replace("-", "_"))
            if target_root is None:
                # tolerate snapshots from renamed mounts: match by suffix
                match = [d for n, d in by_name.items() if _safe(n) == top]
                if not match:
                    continue
                target_root = match[0]
            rel = member.name.split("/", 1)[1] if "/" in member.name else ""
            if not rel:
                continue
            member.name = rel
            tar.extract(member, target_root, filter="data")
    return {"restored": file}
