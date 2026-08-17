#!/bin/bash
# flow5 launcher: starts flow5 maximised on the full KDE desktop.
#
# Same "keep the desktop visible" shape as freecad-streamer's kiosk script — the panel,
# wallpaper and app menu stay reachable, only the app window itself is maximised (not
# fullscreen, which would cover the panel).
#
# flow5 is not an AppImage — it's a plain binary with 3 sibling .so files (built from source,
# see the Dockerfile), so unlike FreeCAD's AppRun this needs LD_LIBRARY_PATH pointed at them
# explicitly (they're flattened into /opt/flow5 alongside the binary at image-build time).
#
# Used as the Exec of the KDE autostart entry (see flow5.desktop).
export DISPLAY="${DISPLAY:-:0}"
export LD_LIBRARY_PATH="/opt/flow5:${LD_LIBRARY_PATH}"

# Fix basic-auth the same way freecad-kiosk.sh does: the runtime dir holding nginx's .htpasswd
# is created mode 700 (root) but nginx workers run as www-data, so login 500s until it's opened up.
( for _ in $(seq 1 60); do
    d="${XDG_RUNTIME_DIR:-/tmp/runtime-ubuntu}"
    if [ -f "$d/.htpasswd" ]; then chmod 755 "$d" 2>/dev/null; nginx -s reload 2>/dev/null; break; fi
    sleep 1
  done ) &

# Maximise the main window once it appears, matching by window NAME ("flow5 <version>") rather
# than WM_CLASS — same reasoning as freecad-kiosk.sh: an auxiliary panel/dialog can share the
# same class as the main window, and fullscreening THAT instead strands its contents off-screen.
# ~120s window covers a slow first launch.
( for _ in $(seq 1 120); do
    for wid in $(xdotool search --name '^flow5' 2>/dev/null); do
      wmctrl -ir "$wid" -b remove,fullscreen 2>/dev/null
      wmctrl -ir "$wid" -b add,maximized_vert,maximized_horz 2>/dev/null
    done
    sleep 1
  done ) &

exec /opt/flow5/flow5 "$@"
