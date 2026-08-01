"""User-facing shared folders on the fleet NAS.

A *share* is a folder two or more people can both open in the Files app. It
lives at ``{NAS}/shares/<slug>`` and its membership is recorded in a small
index file next to it — ``{NAS}/shares/.shares.json``.

Why not reuse ``nas.py``'s shared folders? Two different things wear the same
word:

  - ``shared/<name>`` (nas.py) is where APPS keep fleet-wide data — media,
    ollama-transfer, open-webui-data. It is machinery, not somewhere a person
    files things, and showing it in a file manager would be noise. Its
    membership also lives in Nextcloud's External Storage "applicable users",
    so it only works while Nextcloud is running (it currently is not).
  - ``shares/<slug>`` (here) is people-facing and self-describing: the index
    is a plain JSON file ON the NAS, so any node can answer "which folders
    does this user see?" without another service being up.

The index is the source of truth for membership; the directory is the source
of truth for existence. A share missing either is reported but not mounted.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import tempfile

from . import config

# Slugs become a directory name, so keep them to a safe set. Labels are what a
# person actually reads ("admin + braeden") and may hold spaces and '+'.
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# A label also becomes a mount point inside the container, so it must not carry
# a path separator or lead with a dot.
_LABEL_BAD = re.compile(r"[/\\\x00]")

INDEX_NAME = ".shares.json"


def active_root() -> str:
    """The NAS tree to read/write shares in, from THIS node's point of view.

    A node with the mesh NAS mounted must use it: that is the fleet-wide tree
    every other node sees. Falling back to the local NAS_ROOT on such a node
    would write the index somewhere only this machine looks, and shares would
    silently differ per node.
    """
    return config.nas_data_root()


def shares_root(nas_root: str | None = None) -> str:
    return os.path.join(nas_root or active_root(), config.NAS_SHARES_SUBPATH)


def _index_path(nas_root: str | None = None) -> str:
    return os.path.join(shares_root(nas_root), INDEX_NAME)


def slug_for(members: list[str]) -> str:
    """Default slug for a share: its members, sorted, joined by '-'.

    Deterministic on purpose — asking twice for "the admin+braeden folder"
    lands on the same directory instead of quietly making a second one.
    """
    return "-".join(sorted(_safe_user(u) for u in members)) or "share"


def label_for(members: list[str]) -> str:
    """Default label: 'admin + braeden' — the form the user asked to see."""
    return " + ".join(sorted(members))


def _safe_user(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "-", (name or "").strip().lower())


def _check_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not _SLUG_OK.match(slug) or slug in (".", ".."):
        raise ValueError("Use lowercase letters, numbers, dots, dashes or underscores (max 64).")
    return slug


def _check_label(label: str) -> str:
    label = (label or "").strip()
    if not label or len(label) > 96 or _LABEL_BAD.search(label) or label.startswith("."):
        raise ValueError("A folder name can't be empty, start with '.', or contain a slash.")
    return label


# ── index i/o ─────────────────────────────────────────────────────────────────
def _read_index(nas_root: str | None = None) -> dict[str, dict]:
    try:
        with open(_index_path(nas_root), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("shares", {}) if isinstance(data, dict) else {}


def _write_index(shares: dict[str, dict], nas_root: str | None = None) -> None:
    """Replace the index atomically.

    The temp file is created in the same directory so the rename stays within
    one filesystem — on the mesh NAS a cross-filesystem rename would fall back
    to copy+unlink and lose atomicity, which is the one property this needs:
    a node reading the index mid-write must see the old file or the new one,
    never a truncated one.
    """
    root = shares_root(nas_root)
    os.makedirs(root, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".shares-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "shares": shares}, fh, indent=2, sort_keys=True)
        _chown(tmp)
        os.replace(tmp, _index_path(nas_root))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _chown(path: str) -> None:
    """Match the all_squash owner every app maps to, so a share created on one
    node is writable from another. Best-effort: on a node where the SM does not
    own the tree this is neither possible nor needed."""
    try:
        os.chown(path, config.NAS_UID, config.NAS_GID)
    except (PermissionError, OSError):
        pass


# ── public API ────────────────────────────────────────────────────────────────
def list_shares(nas_root: str | None = None) -> list[dict]:
    root = shares_root(nas_root)
    out = []
    for slug, rec in _read_index(nas_root).items():
        members = sorted(rec.get("members") or [])
        out.append({
            "slug": slug,
            "label": rec.get("label") or label_for(members),
            "members": members,
            "exists": os.path.isdir(os.path.join(root, slug)),
        })
    return sorted(out, key=lambda s: s["label"].lower())


def shares_for(user: str, nas_root: str | None = None) -> list[dict]:
    """The shares this user is a member of, and whose directory really exists.

    Both conditions matter: mounting a share whose directory is gone would have
    Docker create an empty one on the node, which looks like the share with
    everything deleted out of it.
    """
    user = (user or "").strip().lower()
    return [s for s in list_shares(nas_root)
            if s["exists"] and user in {m.lower() for m in s["members"]}]


def create_share(members: list[str], label: str = "", slug: str = "",
                 nas_root: str | None = None) -> dict:
    members = sorted({(u or "").strip() for u in (members or []) if (u or "").strip()})
    if len(members) < 2:
        raise ValueError("A shared folder needs at least two people.")
    slug = _check_slug(slug or slug_for(members))
    label = _check_label(label or label_for(members))

    shares = _read_index(nas_root)
    if slug in shares:
        raise ValueError(f"A shared folder for those people already exists ({shares[slug].get('label', slug)}).")
    if any((s.get("label") or "").lower() == label.lower() for s in shares.values()):
        raise ValueError("A shared folder with that name already exists.")

    path = os.path.join(shares_root(nas_root), slug)
    os.makedirs(path, exist_ok=True)
    _chown(path)

    shares[slug] = {"label": label, "members": members}
    _write_index(shares, nas_root)
    return {"slug": slug, "label": label, "members": members, "path": path}


def set_members(slug: str, members: list[str], nas_root: str | None = None) -> dict:
    slug = _check_slug(slug)
    shares = _read_index(nas_root)
    if slug not in shares:
        raise ValueError("No such shared folder.")
    members = sorted({(u or "").strip() for u in (members or []) if (u or "").strip()})
    if len(members) < 2:
        raise ValueError("A shared folder needs at least two people.")
    shares[slug]["members"] = members
    _write_index(shares, nas_root)
    return {"slug": slug, "label": shares[slug].get("label", slug), "members": members}


def rename_share(slug: str, label: str, nas_root: str | None = None) -> dict:
    slug = _check_slug(slug)
    label = _check_label(label)
    shares = _read_index(nas_root)
    if slug not in shares:
        raise ValueError("No such shared folder.")
    if any(k != slug and (s.get("label") or "").lower() == label.lower()
           for k, s in shares.items()):
        raise ValueError("A shared folder with that name already exists.")
    shares[slug]["label"] = label
    _write_index(shares, nas_root)
    return {"slug": slug, "label": label, "members": shares[slug].get("members", [])}


def delete_share(slug: str, delete_files: bool = False,
                 nas_root: str | None = None) -> dict:
    slug = _check_slug(slug)
    shares = _read_index(nas_root)
    rec = shares.pop(slug, None)
    _write_index(shares, nas_root)
    removed = False
    if delete_files:
        path = os.path.join(shares_root(nas_root), slug)
        # Only ever remove a directory directly under shares/ — never follow a
        # symlink out of the tree.
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
            removed = not os.path.exists(path)
    return {"slug": slug, "label": (rec or {}).get("label", slug),
            "existed": rec is not None, "deleted_files": removed}


def ensure_root(nas_root: str | None = None) -> bool:
    """Make sure shares/ exists. False if the NAS isn't writable from here."""
    try:
        os.makedirs(shares_root(nas_root), exist_ok=True)
        _chown(shares_root(nas_root))
        return True
    except OSError as e:
        return e.errno == errno.EEXIST
