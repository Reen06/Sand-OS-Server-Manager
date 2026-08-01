"""Who can see whose files.

The Hub owns identity, but an app launch must not depend on the Hub being
reachable — so the Hub pushes a small roster to the NAS and this module reads
it. Same reasoning as nas_shares: the answer lives next to the data it is about.

    {NAS}/.views/.users.json
    {"version": 1, "users": {"admin":   {"role": "admin",  "visibility": "guest"},
                             "braeden": {"role": "viewer", "visibility": "household"}}}

It sits under .views/ rather than at the NAS root for two reasons. The root is
root-owned and the Server Manager runs as an ordinary user, so it cannot write
there; and .views/ is already the directory that decides what each person's
Files app contains, which is exactly what this file governs.

Two visibility levels, and the difference is deliberately blunt:

  household — this person's folder appears in an admin's Files app, read-only.
              Intended for people in your house whose files you are expected to
              be able to reach.
  guest     — it does not appear. Intended for a friend you are hosting.

The rule that makes this honest: whichever level an account is on, the person
is TOLD, in their own Files app, by write_notice() below. Silent admin access
is the failure mode this whole feature exists to avoid — if you can see
someone's files and they don't know it, the setting is a lie.

What this does NOT claim: that a guest's files are hidden from the machine's
owner. They are not, and no software setting on hardware you control could make
them so. The notice wording says "does not appear in the admin's Files app",
which is exactly what is true, rather than "only you can see this", which is not.
"""
from __future__ import annotations

import json
import os
import tempfile

from . import config

ROSTER_NAME = ".users.json"
NOTICE_NAME = "Who can see your files.txt"

HOUSEHOLD = "household"
GUEST = "guest"


def _roster_dir(nas_root: str | None = None) -> str:
    return os.path.join(nas_root or config.nas_data_root(), config.NAS_VIEWS_SUBPATH)


def _roster_path(nas_root: str | None = None) -> str:
    return os.path.join(_roster_dir(nas_root), ROSTER_NAME)


def read_roster(nas_root: str | None = None) -> dict[str, dict]:
    try:
        with open(_roster_path(nas_root), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    users = data.get("users") if isinstance(data, dict) else None
    return users if isinstance(users, dict) else {}


def write_roster(users: dict[str, dict], nas_root: str | None = None) -> dict:
    """Replace the roster atomically (same-directory temp + rename, so a reader
    mid-write sees the old file or the new one, never a truncated one)."""
    clean: dict[str, dict] = {}
    for name, rec in (users or {}).items():
        name = (name or "").strip()
        if not name:
            continue
        rec = rec or {}
        vis = rec.get("visibility")
        clean[name] = {
            "role": rec.get("role") or "viewer",
            "visibility": vis if vis in (HOUSEHOLD, GUEST) else GUEST,
        }
    root = _roster_dir(nas_root)
    os.makedirs(root, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".users-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "users": clean}, fh, indent=2, sort_keys=True)
        try:
            os.chown(tmp, config.NAS_UID, config.NAS_GID)
        except (PermissionError, OSError):
            pass
        os.replace(tmp, _roster_path(nas_root))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"users": clean}


# ── queries ───────────────────────────────────────────────────────────────────
def visibility_of(user: str, nas_root: str | None = None) -> str:
    rec = read_roster(nas_root).get((user or "").strip()) or {}
    return rec.get("visibility") if rec.get("visibility") in (HOUSEHOLD, GUEST) else GUEST


def is_admin(user: str, nas_root: str | None = None) -> bool:
    rec = read_roster(nas_root).get((user or "").strip()) or {}
    return rec.get("role") == "admin"


def admins(nas_root: str | None = None) -> list[str]:
    return sorted(n for n, r in read_roster(nas_root).items()
                  if (r or {}).get("role") == "admin")


def household_users(exclude: str = "", nas_root: str | None = None) -> list[str]:
    """People whose folder an admin's Files app should show.

    Excludes the viewer themselves — an admin already has their own folder at
    the top level, and a second copy of it nested under "Household" would just
    be two names for one thing.
    """
    exclude = (exclude or "").strip().lower()
    return sorted(n for n, r in read_roster(nas_root).items()
                  if (r or {}).get("visibility") == HOUSEHOLD
                  and n.strip().lower() != exclude)


# ── the notice ────────────────────────────────────────────────────────────────
def notice_text(user: str, nas_root: str | None = None) -> str:
    """What this person is told about who can see their folder.

    Written for BOTH levels. A notice that only appears when someone IS being
    watched teaches people that its absence means something, and then a bug that
    fails to write it reads as a promise of privacy.
    """
    vis = visibility_of(user, nas_root)
    who = admins(nas_root)
    if vis == HOUSEHOLD:
        names = ", ".join(who) if who else "the administrator"
        body = (
            f"Your folder is shared with the administrator account ({names}).\n"
            f"\n"
            f"That means {names} can open this folder from their own Files app and\n"
            f"read what is in it. They cannot change or delete your files — their\n"
            f"copy is read-only.\n"
            f"\n"
            f"This is the \"Household\" setting on your account. If you would rather\n"
            f"your folder was not shown to them, ask the administrator to change\n"
            f"your account to \"Guest\".\n"
        )
    else:
        body = (
            "Your folder is not shared with anyone.\n"
            "\n"
            "It does not appear in the administrator's Files app, and nobody else\n"
            "sees it unless you put files in a shared folder.\n"
            "\n"
            "This is the \"Guest\" setting on your account.\n"
            "\n"
            "One honest caveat: this controls what the apps show. Whoever physically\n"
            "owns and administers the server hardware could still reach the files on\n"
            "disk, as is true of any computer you do not own yourself.\n"
        )
    return (
        "WHO CAN SEE YOUR FILES\n"
        "======================\n"
        "\n"
        + body
        + "\nShared folders are separate: anything you put in a folder named after\n"
          "two people (like \"admin + braeden\") is visible to everyone named in it.\n"
          "\nThis file is written automatically each time you open Files.\n"
    )


def ensure_view_children(view_dir: str, rel_paths: list[str]) -> None:
    """Pre-create the mount points a view needs, owned by us.

    Docker will happily create a missing bind target itself — as root, mode 755.
    That is fine until one has to be cleaned up: the Server Manager runs as an
    ordinary user, and removing an entry needs write permission on its PARENT,
    so a root-owned "Household" makes the per-person directories inside it
    permanently unprunable. Creating them first is the whole fix; Docker then
    finds them already there and leaves the ownership alone.
    """
    for rel in rel_paths:
        path = os.path.join(view_dir, rel)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            continue
        # chown each level we may have just made, not only the leaf.
        cur = path
        while os.path.commonpath([cur, view_dir]) == os.path.abspath(view_dir) \
                and os.path.abspath(cur) != os.path.abspath(view_dir):
            try:
                os.chown(cur, config.NAS_UID, config.NAS_GID)
            except (PermissionError, OSError):
                pass
            cur = os.path.dirname(cur)


def prune_stale_mountpoints(view_dir: str, keep: set[str]) -> list[str]:
    """Remove empty leftover directories from a Files-app view root.

    The view root holds nothing but mount points and the notice file, so Docker
    creating a directory there is always it preparing a bind target. When the
    mount later goes away — a share deleted, a housemate moved to Guest — the
    empty directory stays behind and the app shows a folder that opens onto
    nothing. Worse for "Household": an empty folder by that name reads as "you
    have access and they have no files", which is the opposite of the truth.

    Prunes bottom-up, because these leftovers nest: "Household" holds a
    per-person directory for each mount inside it, so the parent is never empty
    until its children go first.

    Every removal is an rmdir, which fails on a non-empty directory. That is the
    entire safety argument — no walk of ours can delete a file, so a mount that
    merely happens to be detached right now cannot lose data.
    """
    removed = []
    try:
        entries = os.listdir(view_dir)
    except OSError:
        return removed
    for name in entries:
        if name in keep or name == NOTICE_NAME:
            continue
        top = os.path.join(view_dir, name)
        if not os.path.isdir(top) or os.path.islink(top):
            continue
        for cur, dirs, files in os.walk(top, topdown=False, followlinks=False):
            if files:
                continue
            try:
                os.rmdir(cur)
            except OSError:
                pass
        if not os.path.exists(top):
            removed.append(name)
    return removed


def write_notice(view_dir: str, user: str, nas_root: str | None = None) -> bool:
    """Drop the notice into the root of this user's Files view.

    Rewritten on every launch rather than once, so that changing someone from
    Household to Guest (or back) reaches them without anybody remembering to go
    update a file. Best-effort: a file manager that opens without its notice is
    a much smaller problem than an app that refuses to start.
    """
    try:
        path = os.path.join(view_dir, NOTICE_NAME)
        text = notice_text(user, nas_root)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                if fh.read() == text:
                    return True      # unchanged: don't churn the mtime
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chown(path, config.NAS_UID, config.NAS_GID)
        except (PermissionError, OSError):
            pass
        return True
    except OSError:
        return False
