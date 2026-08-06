"""A node-local hold on auto-update, so work in progress survives it.

Auto-update reaches into a node and runs `git reset --hard origin/main`. That is
correct for a fleet meant to converge on one commit, and destructive for anyone
editing on the machine at the time: uncommitted work is discarded with no
warning, no copy, and nothing said afterwards. It happened three times in one
session while a node was being worked on directly.

The hold is the answer to "I am editing here, leave me alone". Take it before
editing, release it after committing and pushing — at which point the node
updates to that commit like any other.

Design notes, each of which is load-bearing:

  * The flag lives on the NODE, not in the Hub's database. A machine being
    edited is a fact about that machine, and a hold that depends on the Hub
    being reachable is no hold at all when the network is what you are editing.

  * It sits at `<repo_root>/.sandos-hold`, which survives `git reset --hard`
    (that removes tracked changes, never untracked files — verified, not
    assumed). Anywhere under the repo is guaranteed writable by whoever owns
    the checkout, which is exactly who needs to take the hold, and needs no
    root.

  * It is checked twice: the Hub skips held nodes when choosing what to update,
    and the update command re-checks on the node itself before resetting. Only
    the second is race-free — the Hub's view can be minutes stale — and the
    first exists so a held node reads as deliberately held rather than
    mysteriously behind.

  * A hold is never taken implicitly. A machine that quietly stops accepting
    updates is the failure this project has already been bitten by; a hold has
    to be a decision someone made, with a reason attached and a visible age.
"""
from __future__ import annotations

import json
import os
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOLD_FILE = os.path.join(_REPO_ROOT, ".sandos-hold")


def status() -> dict:
    """Whether this node is holding auto-update, and why.

    A hold file that cannot be parsed still counts as held. The point is to stop
    an update from destroying someone's work, and a corrupt file is not evidence
    that nobody is working — failing open here would discard exactly the changes
    the hold exists to protect.
    """
    if not os.path.exists(HOLD_FILE):
        return {"held": False}
    out = {"held": True, "reason": "", "since": None, "held_by": ""}
    try:
        with open(HOLD_FILE) as f:
            d = json.load(f)
        out["reason"] = str(d.get("reason") or "")
        out["since"] = d.get("since")
        out["held_by"] = str(d.get("held_by") or "")
    except Exception:  # noqa: BLE001
        out["reason"] = "(hold file unreadable — treating as held)"
    if out.get("since"):
        try:
            out["age_seconds"] = max(0, int(time.time() - float(out["since"])))
        except (TypeError, ValueError):
            pass
    return out


def take(reason: str = "", held_by: str = "") -> dict:
    """Start holding. Re-taking an existing hold refreshes the reason but keeps
    the original timestamp, so a long-running hold cannot hide its own age by
    being re-taken."""
    prev = status()
    since = prev.get("since") if prev.get("held") else None
    payload = {
        "reason": (reason or "").strip(),
        "held_by": (held_by or "").strip(),
        "since": since if since is not None else time.time(),
    }
    tmp = HOLD_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, HOLD_FILE)      # atomic: never a half-written hold
    return status()


def release() -> dict:
    """Stop holding. Idempotent — releasing an unheld node is not an error, so a
    cleanup path can run unconditionally without having to check first."""
    try:
        os.unlink(HOLD_FILE)
    except FileNotFoundError:
        pass
    return status()
