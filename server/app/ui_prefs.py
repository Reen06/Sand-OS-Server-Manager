"""Per-user, per-app display preferences for streamed desktops.

Scale was a constant in the catalogue, so changing it meant an edit, a deploy
and a container recreate -- i.e. it needed whoever maintains the code. These
are settings a person should be able to change while looking at the thing
they are changing.

Kept on the NODE rather than the Hub because that is where the container is
built: launch() merges them into the app's env, so nothing has to be threaded
through the Hub's launch call or kept in sync across two databases. They are
applied at container CREATE, so a change takes effect on the app's next
start -- the UI says so rather than pretending otherwise.
"""
from __future__ import annotations

import json
import os
import threading

# Alongside the catalogue state, which is the node's existing convention for
# small per-node JSON that must survive a restart.
_PATH = os.environ.get("SM_UI_PREFS",
                       os.path.expanduser("~/.sandos-sm/ui-prefs.json"))
_lock = threading.Lock()

# Only settings we actually honour. An unknown key from a client is dropped
# rather than stored, so the file cannot fill with junk that looks meaningful.
#
# scale: QT_SCALE_FACTOR. Scales the whole Qt widget tree (toolbars, icons,
# panels), not just text. Note this does NOT reach an app with its own
# non-Qt toolkit -- Omniverse Kit ignores it entirely.
_ALLOWED = {"scale"}
_SCALE_CHOICES = ("1", "1.25", "1.5", "1.75", "2", "2.5", "3")


def _load() -> dict:
    try:
        with open(_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        # A corrupt file must not stop apps launching -- fall back to defaults.
        return {}


def get(app_id: str, user: str) -> dict:
    return (_load().get(f"{app_id}::{user}") or {})


def set_prefs(app_id: str, user: str, prefs: dict) -> dict:
    clean = {}
    for k, v in (prefs or {}).items():
        if k not in _ALLOWED:
            continue
        if k == "scale":
            v = str(v)
            if v not in _SCALE_CHOICES:
                raise ValueError(f"scale must be one of {', '.join(_SCALE_CHOICES)}")
        clean[k] = v
    with _lock:
        data = _load()
        cur = data.get(f"{app_id}::{user}") or {}
        cur.update(clean)
        data[f"{app_id}::{user}"] = cur
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _PATH)      # atomic: never leave a half-written file
        return cur


def env_for(app_id: str, user: str) -> dict:
    """Preferences as container env, for launch() to merge in."""
    p = get(app_id, user)
    env = {}
    if p.get("scale"):
        env["QT_SCALE_FACTOR"] = str(p["scale"])
    return env


def choices() -> dict:
    return {"scale": list(_SCALE_CHOICES)}
