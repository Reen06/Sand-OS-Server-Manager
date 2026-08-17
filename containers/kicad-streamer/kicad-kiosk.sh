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

# Maximize each KiCad top-level window ONCE, the first time it's seen, for
# the whole session rather than a fixed startup window.
#
# This still differs from FreeCAD's launcher (which only chases new windows
# for ~120s after start): KiCad is genuinely multi-window — the project
# manager comes up first, then the Schematic Editor and PCB Editor open as
# SEPARATE top-level windows whenever the user opens them from the project
# manager, which can be minutes into the session, well outside any fixed
# startup window. Matching by WM_CLASS (not window name/title, unlike
# FreeCAD) is correct here: KiCad's editors are each their own real top-level
# application window, not auxiliary panels/dialogs the way FreeCAD's Ribbon/
# Searchbar panels were.
#
# Unlike FreeCAD (one binary), KiCad's project manager, Schematic Editor, PCB
# Editor, Gerber Viewer, PCB Calculator and Worksheet Editor are SEPARATE
# binaries (verified inside the AppImage's own AppDir: bin/kicad, bin/
# eeschema, bin/pcbnew, bin/gerbview, bin/pcb_calculator, bin/pl_editor), each
# setting its own WM_CLASS to its own binary name — matching only "kicad"
# would maximize the project manager and silently ignore every editor window.
#
# What changed: this used to re-force maximize on EVERY window EVERY second
# forever, which fought the user — manually resizing or unmaximizing a KiCad
# window snapped it back within a second. Each window ID is now maximized
# once and then remembered, so a later manual resize sticks.
( declare -A seen
  while true; do
    for wid in $(xdotool search --class '^(kicad|eeschema|pcbnew|gerbview|pcb_calculator|pl_editor|bitmap2component)$' 2>/dev/null); do
      [ -n "${seen[$wid]:-}" ] && continue
      wmctrl -ir "$wid" -b remove,fullscreen 2>/dev/null
      wmctrl -ir "$wid" -b add,maximized_vert,maximized_horz 2>/dev/null
      seen[$wid]=1
    done
    sleep 1
  done ) &

/opt/kicad/AppRun "$@"
