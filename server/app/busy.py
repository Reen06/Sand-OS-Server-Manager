"""Busy/Available node mode — node-local state, NOT the shared NAS.

Busy means: every running app instance on this node was stopped to free up
its resources (the owner wants to do something else with the machine, e.g.
play a game), and new launches are refused until it's back to Available.

This has to keep working even if the Hub is briefly unreachable, so it's a
plain local JSON file next to the code (like server/sand.db already is),
NOT app_images.py's _STATE_FILE (that one deliberately lives on the shared
NAS — wrong for a per-node fact). The Hub's own sm_nodes.busy column is only
ever a reported MIRROR of this file for the Fleet tab's display, refreshed
every probe — this file is the real source of truth.

override_allowed is a separate concept: the owner's own consent for a
remote Hub admin to force this node back to Available. It's node-local for
the same reason busy is, but with an even stronger rule: it can only ever be
set by a loopback caller (this machine's own launcher GUI) — never over the
network, so a remote admin can never grant themselves this permission for
someone else's node.
"""
from __future__ import annotations
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_STATE_FILE = _REPO_ROOT / "server" / ".busy_state.json"


def _load() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f)


def is_busy() -> bool:
    return bool(_load().get("busy", False))


def set_busy(v: bool) -> None:
    state = _load()
    state["busy"] = bool(v)
    _save(state)


def override_allowed() -> bool:
    return bool(_load().get("override_allowed", False))


def set_override_allowed(v: bool) -> None:
    state = _load()
    state["override_allowed"] = bool(v)
    _save(state)
