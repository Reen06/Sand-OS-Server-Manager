#!/bin/bash
# FreeCAD launcher: starts FreeCAD maximised on the full KDE desktop.
#
# This was once a single-app kiosk that killed plasmashell and forced the
# window fullscreen. That is deliberately reversed: the panel, wallpaper and
# app menu stay, so a terminal or file manager is reachable without leaving
# the stream. FreeCAD is maximised rather than fullscreen, because fullscreen
# would cover the panel and defeat the point.
#
# FreeCAD is RELAUNCHED if it exits or crashes, so closing it never leaves a
# dead black screen. (True teardown of the instance is the Server Manager's
# job — on disconnect/idle — not "the user closed the app".)
#
# Used as the Exec of the KDE autostart entry (see freecad.desktop).
export DISPLAY="${DISPLAY:-:0}"

# Fix basic-auth: the runtime dir holding nginx's .htpasswd is created mode 700
# (root) but nginx workers run as www-data, so login 500s. Make it traversable.
( for _ in $(seq 1 60); do
    d="${XDG_RUNTIME_DIR:-/tmp/runtime-ubuntu}"
    if [ -f "$d/.htpasswd" ]; then chmod 755 "$d" 2>/dev/null; nginx -s reload 2>/dev/null; break; fi
    sleep 1
  done ) &

# The KDE shell (panel + wallpaper) is deliberately LEFT RUNNING.
#
# This used to kill plasmashell for a single-app kiosk. The owner reversed that
# decision: seeing the desktop is worth more than the few pixels the panel
# costs -- you can reach a terminal, a file manager and the app menu without
# leaving the stream, and FreeCAD is no longer the only thing you can do in it.

# Restore the user's preferences from FreeCAD's own auto-backup before every
# (re)launch. Verified empirically: the Server Manager tears an instance down
# with `docker rm -f` (instant SIGKILL, no grace period) and even a clean
# SIGTERM/window-close does NOT make FreeCAD write its live user.cfg — so
# settings (dark mode, navigation style, ...) never survived a restart despite
# .config/.local persisting on the NAS. What DOES survive: FreeCAD's own
# SavedPreferencePacks/Backups/user.<epoch>.cfg snapshots, written throughout
# the session as preferences change. Seeding the live user.cfg from the newest
# one before each launch makes settings durable across restarts/crashes/kills
# without depending on a graceful-shutdown path that doesn't actually exist.
restore_freecad_prefs() {
  local backups="$HOME/.local/share/FreeCAD/v1-1/SavedPreferencePacks/Backups"
  [ -d "$backups" ] || return 0
  local latest
  latest="$(ls -t "$backups"/user.*.cfg 2>/dev/null | head -1)"
  [ -n "$latest" ] || return 0
  # ALWAYS overwrite — no "only if newer" guard. That guard was the actual bug:
  # .config/.local persist on the NAS across a full stop+relaunch (not just a
  # `docker restart`), so the STALE live user.cfg from the previous session is
  # still sitting there with its own (often newer, e.g. FreeCAD re-touches it
  # on its own read/normalize pass) mtime — so "-nt" would decide the stale
  # file looked newer than the very backup that was seeded FROM it last time,
  # and skip the restore, silently keeping old settings. There is no clean-
  # shutdown path that legitimately makes the live file MORE current than the
  # newest backup (verified: neither SIGTERM nor a graceful window-close ever
  # updates it correctly) — the backup is always the right answer.
  echo "[freecad-kiosk] restoring prefs from $(basename "$latest")"
  for dest in "$HOME/.config/FreeCAD/v1-1/user.cfg" "$HOME/.local/share/FreeCAD/v1-1/user.cfg"; do
    mkdir -p "$(dirname "$dest")"
    cp "$latest" "$dest"
  done
}

ensure_freecad_config_files() {
  local config_dir="$HOME/.config/FreeCAD/v1-1"
  mkdir -p "$config_dir"
  for f in user.cfg system.cfg; do
    [ -e "$config_dir/$f" ] || : > "$config_dir/$f"
    chmod 660 "$config_dir/$f" 2>/dev/null || true
  done
}

# Keep FreeCAD up: launch it maximized; relaunch if it exits. Guard against a
# tight crash loop (5 exits in under 5s each → give up so we don't spin).
# Run ONCE, not in a relaunch loop.
#
# Respawning was right when the desktop was hidden: closing FreeCAD left a
# black screen with nothing behind it. Now the KDE session is visible, so it
# fights the user instead -- closing the app brought it straight back, and
# repeated exits stacked more windows. Exiting now just leaves you on the
# desktop, where the app menu can start it again.
  ensure_freecad_config_files
  restore_freecad_prefs
  ensure_freecad_config_files
  # No auto-maximize: the window manager's own placement is left alone, so
  # FreeCAD opens where KDE puts it and any size the user sets afterwards
  # sticks. (Previously a background loop re-applied maximize for ~120s,
  # which fought manual resizing during startup.) Removed 2026-08-17.


/opt/freecad/AppRun "$@"

