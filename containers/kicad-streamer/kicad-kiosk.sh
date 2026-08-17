#!/bin/bash
# KiCad launcher: starts the KiCad project manager on the full KDE desktop.
#
# The KDE shell (panel + wallpaper) is deliberately LEFT RUNNING, same
# decision as FreeCAD's launcher — a terminal/file manager stays reachable
# without leaving the stream.
#
# KiCad is RELAUNCHED if the project manager exits or crashes, so closing it
# never leaves a dead black screen. (True teardown of the instance is the
# Server Manager's job — on disconnect/idle — not "the user closed the app".)
#
# Used as the Exec of the KDE autostart entry (see kicad.desktop).
export DISPLAY="${DISPLAY:-:0}"

# Fix basic-auth: the runtime dir holding nginx's .htpasswd is created mode 700
# (root) but nginx workers run as www-data, so login 500s. Same fix as FreeCAD.
( for _ in $(seq 1 60); do
    d="${XDG_RUNTIME_DIR:-/tmp/runtime-ubuntu}"
    if [ -f "$d/.htpasswd" ]; then chmod 755 "$d" 2>/dev/null; nginx -s reload 2>/dev/null; break; fi
    sleep 1
  done ) &

# No auto-maximize: KiCad's own window placement is left alone, so its
# project manager and each editor open where the window manager puts them,
# and any size the user sets sticks. (Earlier versions maximized new windows
# from a background loop; KiCad handles this itself.) Removed 2026-08-17.

/opt/kicad/AppRun "$@"
