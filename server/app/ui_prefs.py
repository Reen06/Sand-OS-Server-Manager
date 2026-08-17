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
import subprocess
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
# app_scale is separate from scale on purpose. A Kit viewport wants a much
# larger UI than the desktop around it -- Isaac's own panels are dense and
# render small, while the KDE shell at the same factor becomes oversized. One
# number could not satisfy both, so there are two.
_ALLOWED = {"scale", "app_scale"}
_SCALE_CHOICES = ("1", "1.25", "1.5", "1.75", "2", "2.5", "3")
# Goes higher than the desktop: Isaac is the one that needs turning way up.
_APP_SCALE_CHOICES = ("1", "1.25", "1.5", "1.75", "2", "2.5", "3", "3.5", "4")
# GDK_SCALE (GTK full-UI scaling) accepts whole numbers only, so an app scaled
# that way gets a whole-number list instead — offering 1.5 there would show a
# control that silently rounds to 1.
_APP_SCALE_INTEGER_CHOICES = ("1", "2", "3")


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
        if k in ("scale", "app_scale"):
            v = str(v)
            # _APP_SCALE_CHOICES is a superset of the integer list, so one
            # check covers both kinds of second slider.
            allowed = _APP_SCALE_CHOICES if k == "app_scale" else _SCALE_CHOICES
            if v not in allowed:
                raise ValueError(f"{k} must be one of {', '.join(allowed)}")
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
        # GTK/wxWidgets (KiCad) ignores QT_SCALE_FACTOR entirely, and splits
        # what Qt does in one number across two variables:
        #   GDK_DPI_SCALE  fractional, scales TEXT only      <- the fine slider
        #   GDK_SCALE      integer only, scales the WHOLE UI <- app_scale below
        # So the fine slider drives text/DPI here, and the second (full-UI)
        # slider drives GDK_SCALE. They MULTIPLY, which is the point of having
        # both: full 2 + fine 1.25 gives 2x widgets and icons with text a
        # further 1.25x on top, so text size is tunable independently of how
        # big the chrome is.
        if str(p["scale"]) != "1":
            env["GDK_DPI_SCALE"] = str(p["scale"])
    # Kit ignores QT_SCALE_FACTOR and needs its own number (see the
    # isaac-launch wrapper). Falls back to the desktop scale when no separate
    # app scale is set, so the single-slider behaviour still holds.
    app_scale = p.get("app_scale") or p.get("scale")
    if app_scale:
        env["SM_UI_SCALE"] = str(app_scale)
    # The full-UI slider, for GTK. Deliberately reads app_scale ONLY (no
    # fallback to scale): the fine slider already drives GDK_DPI_SCALE, and
    # since the two multiply, falling back would apply the same number twice
    # the moment someone touched only the fine one. Integer-only because
    # GDK_SCALE cannot express 1.5 — which is why choices() offers this app a
    # different, whole-number list rather than the fractional one.
    _as = p.get("app_scale")
    if _as:
        try:
            _f = float(_as)
        except ValueError:
            _f = 1.0
        if _f >= 2 and _f == int(_f):
            env["GDK_SCALE"] = str(int(_f))
    return env


# image id -> does it want its own app scale. Labels cannot change without a
# rebuild, which produces a new id, so this never needs invalidating.
_APP_SCALE_LABEL = "sandos.ui.app_scale"
_label_cache: dict[str, str] = {}


def _image_app_scale_mode(app_id: str) -> str:
    """Does this app's IMAGE ask for a second scale, and of which KIND?

    Returns "" (none), "fractional" (Kit-style, any step) or "integer"
    (GTK/GDK_SCALE, whole numbers only). The kind matters because the two are
    not interchangeable: Kit takes any number, GDK_SCALE silently rounds, so
    offering fractional steps to a GTK app would show a control that appears
    to move and does nothing between whole numbers.

    Read from the image rather than decided here, because it is a fact about
    what is inside the container: a Qt app is scaled entirely by
    QT_SCALE_FACTOR and a second slider would move and change nothing, while
    an Omniverse Kit app ignores that variable and needs its own. The node
    cannot know that about an arbitrary image — and a copy of the answer kept
    node-side is a copy that drifts from the image it describes.

    Absent label means no. A default of "yes" would put a dead control on
    every app that has not been rebuilt, which is the failure this is fixing.
    """
    if not app_id:
        return ""
    try:
        from . import app_images, registry
        app = registry.CATALOG.get(app_id) or registry.APPS.get(app_id)
        if app is None:
            return ""
        tag = app_images._image_tag(app)
        host = app_images.active_docker_host(app_id)
        args = ["docker"] + (["-H", host] if host else []) + [
            "image", "inspect", tag,
            "--format", "{{.Id}}|{{index .Config.Labels \"" + _APP_SCALE_LABEL + "\"}}"]
        r = subprocess.run(args, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return ""
        img_id, _, raw = (r.stdout or "").strip().partition("|")
        if img_id in _label_cache:
            return _label_cache[img_id]
        raw = raw.strip().lower()
        mode = "integer" if raw == "integer" else (
            "fractional" if raw in ("1", "true", "yes") else "")
        _label_cache[img_id] = mode
        return mode
    except Exception:  # noqa: BLE001
        return ""


def choices(app_id: str = "") -> dict:
    """Which scales this app can actually be offered.

    A second scale is only real for an app whose own toolkit ignores
    QT_SCALE_FACTOR. Offering it everywhere put a slider on apps where moving
    it changed nothing; the presence of the key is what tells the UI whether
    the control means anything, so the UI needs no list of app names.
    """
    out = {"scale": list(_SCALE_CHOICES)}
    mode = _image_app_scale_mode(app_id)
    if mode == "integer":
        out["app_scale"] = list(_APP_SCALE_INTEGER_CHOICES)
    elif mode:
        out["app_scale"] = list(_APP_SCALE_CHOICES)
    return out


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
    prefs = get(app_id, user) or {}
    scale = prefs.get("scale")
    app_scale = prefs.get("app_scale") or scale
    if not scale and not app_scale:
        return {"applied": False, "reason": "no scale set"}
    scale = scale or "1"
    # Isaac (Kit) reads its scale at startup and ignores QT_SCALE_FACTOR, so
    # the app itself has to be relaunched for the two to match. Done inside the
    # same session, so the container and everything else keep running -- this
    # costs Isaac's startup, not a container recreate.
    # Write the current value where BOTH launchers read it. Container env is
    # fixed at creation, so without this a scale change could never reach a
    # Kit process started later -- which is exactly what happened with Lab's
    # train.py: it launches Kit directly and inherited a value set hours ago.
    script = (
        # Rewrite the Kit experience files, which every Kit launch reads --
        # train.py, isaac-sim.sh, AppLauncher, all of them. Passing a CLI flag
        # instead reached train.py's own parser and killed the run with
        # "unrecognized arguments", so the launcher is the wrong layer.
        # Takes effect the next time Kit starts; a run already going is
        # untouched.
        f'for f in /isaac-sim/apps/*.kit /workspace/isaaclab/apps/*.kit '
        f'/workspace/isaaclab/apps/*/*.kit; do '
        f'  [ -f "$f" ] || continue; '
        f'  if grep -q "^dpiScaleOverride" "$f"; then '
        f'    sed -i "s|^dpiScaleOverride = .*|dpiScaleOverride = {app_scale}|" "$f"; '
        f'  elif grep -q "^\\[settings.app.window\\]" "$f"; then '
        f'    sed -i "s|^\\[settings.app.window\\]|[settings.app.window]\\ndpiScaleOverride = {app_scale}|" "$f"; '
        f'  else printf "\\n[settings.app.window]\\ndpiScaleOverride = {app_scale}\\n" >> "$f"; fi; '
        f'done 2>/dev/null || true; '
        'p=$(pgrep -x plasma_session | head -1); [ -n "$p" ] || exit 3; '
        'eval $(tr "\\0" "\\n" < /proc/$p/environ '
        '| grep -E "^(DISPLAY|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR|XAUTHORITY)=" '
        '| sed "s/^/export /"); '
        # Xft.dpi first: it is what scales font rendering for anything that
        # does not read QT_SCALE_FACTOR, and it applies to windows opened
        # AFTER this without restarting anything.
        f'echo "Xft.dpi: $(awk "BEGIN{{print int(96*{scale})}}")" | xrdb -merge 2>/dev/null; '
        f'QT_SCALE_FACTOR={scale} nohup plasmashell --replace >/tmp/sm-rescale.log 2>&1 & '
        'sleep 7; pgrep -x plasmashell >/dev/null || exit 1; '
        # kwin too, or title bars and window buttons stay at the old size
        # while the panel changes -- which is exactly what it looked like.
        # --replace hands over without destroying windows, so a running app
        # keeps its session.
        f'QT_SCALE_FACTOR={scale} nohup kwin_x11 --replace >/tmp/sm-kwin.log 2>&1 & '
        'sleep 6; pgrep -x kwin_x11 >/dev/null || echo "kwin did not return"; '
        # Relaunch a Kit app at the new scale if one is running. Guarded on it
        # already running, so this never STARTS an app that was closed.
        'if pgrep -x kit >/dev/null && [ -x /usr/local/bin/isaac-launch ]; then '
        # Wait for it to actually exit. A fixed sleep was not enough -- Kit
        # takes several seconds to shut down, so the replacement started while
        # the old process still held the session and the new scale never
        # applied. Escalate to KILL if it will not go, then confirm.
        '  pkill -x kit; '
        '  for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -x kit >/dev/null || break; sleep 1; done; '
        '  pgrep -x kit >/dev/null && { pkill -9 -x kit; sleep 2; }; '
        f'  SM_UI_SCALE={app_scale} nohup /usr/local/bin/isaac-launch >/tmp/sm-isaac.log 2>&1 & '
        'fi; true'
    )
    from . import docker_backend
    # Run as whoever owns the KDE session, not a hardcoded uid. Isaac's session
    # runs as the desktop user (1000); FreeCAD's base image runs it as ROOT
    # with HOME=/home/ubuntu. Assuming 1000 meant every apply silently failed
    # on FreeCAD -- the slider reported success and nothing on screen changed.
    probe = docker_backend._docker(
        ["exec", container, "sh", "-c",
         "id -u $(ps -eo user,comm | awk '/plasma_session|startplasma/{print $1; exit}') 2>/dev/null || echo 0"],
        timeout=15)
    uid = (probe.stdout or "").strip() or "0"
    if not uid.isdigit():
        uid = "0"
    res = docker_backend._docker(
        ["exec", "-u", uid, container, "sh", "-c", script], timeout=40)
    ok = res.returncode == 0
    return {"applied": ok, "scale": scale, "app_scale": app_scale,
            "reason": "" if ok else "the desktop shell did not come back"}
