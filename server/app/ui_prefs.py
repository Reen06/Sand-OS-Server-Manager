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
        # Kit ignores QT_SCALE_FACTOR, so an app built on it needs the same
        # number passed its own way (see the isaac-launch wrapper). Harmless
        # for images that do not use it.
        env["SM_UI_SCALE"] = str(p["scale"])
    return env


def choices() -> dict:
    return {"scale": list(_SCALE_CHOICES)}


def apply_live(app_id: str, user: str, container: str) -> dict:
    """Re-scale the running desktop WITHOUT recreating the container.

    QT_SCALE_FACTOR is read per-process at startup, so restarting just the
    shell picks up a new value while everything else in the session keeps
    running -- verified with Isaac: kit survives, only the panel and its
    widgets are replaced.

    Restarting the whole SESSION would not work: the app is a child of
    plasma_session, so it would die with it and that is no cheaper than
    recreating the container.

    The shell must inherit the session's DISPLAY/DBUS/XDG environment or Qt
    cannot load its xcb platform plugin and the shell never comes back --
    taking the panel with it. Read from a live session process rather than
    assumed.
    """
    scale = (get(app_id, user) or {}).get("scale")
    if not scale:
        return {"applied": False, "reason": "no scale set"}
    # Isaac (Kit) reads its scale at startup and ignores QT_SCALE_FACTOR, so
    # the app itself has to be relaunched for the two to match. Done inside the
    # same session, so the container and everything else keep running -- this
    # costs Isaac's startup, not a container recreate.
    script = (
        'p=$(pgrep -x plasma_session | head -1); [ -n "$p" ] || exit 3; '
        'eval $(tr "\\0" "\\n" < /proc/$p/environ '
        '| grep -E "^(DISPLAY|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR|XAUTHORITY)=" '
        '| sed "s/^/export /"); '
        f'QT_SCALE_FACTOR={scale} nohup plasmashell --replace >/tmp/sm-rescale.log 2>&1 & '
        'sleep 7; pgrep -x plasmashell >/dev/null || exit 1; '
        # Relaunch a Kit app at the new scale if one is running. Guarded on it
        # already running, so this never STARTS an app that was closed.
        'if pgrep -x kit >/dev/null && [ -x /usr/local/bin/isaac-launch ]; then '
        '  pkill -x kit; sleep 3; '
        f'  SM_UI_SCALE={scale} nohup /usr/local/bin/isaac-launch >/tmp/sm-isaac.log 2>&1 & '
        'fi; true'
    )
    from . import docker_backend
    res = docker_backend._docker(
        ["exec", "-u", "1000", container, "sh", "-c", script], timeout=40)
    ok = res.returncode == 0
    return {"applied": ok, "scale": scale,
            "reason": "" if ok else "the desktop shell did not come back"}
