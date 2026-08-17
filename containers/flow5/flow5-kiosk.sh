#!/bin/bash
# flow5 launcher: starts flow5 at its own natural window size on the full KDE desktop.
#
# Deliberately does NOT force-maximise the window (an earlier version did, matching
# freecad-kiosk.sh's shape) — flow5's own multi-window workflow (foil/plane/analysis dialogs
# alongside the main window) is awkward when the main window is pinned maximised, and the
# user asked to control window size themselves via the normal maximise button/drag-resize
# instead of having it done automatically on launch.
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

exec /opt/flow5/flow5 "$@"
