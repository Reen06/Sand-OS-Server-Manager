#!/bin/bash
# Single-app kiosk launcher: stream FreeCAD ONLY — no KDE panel, wallpaper, or
# desktop. Keeps KWin (the window manager / GL compositor) so FreeCAD's window
# and 3D view work; removes plasmashell (the panel + desktop shell). FreeCAD is
# maximized to fill the whole stream, and is RELAUNCHED if it exits/crashes so
# closing it never leaves a dead black screen. (True teardown of the instance is
# the Server Manager's job — on disconnect/idle — not "the user closed the app".)
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

# Remove the KDE shell (panel + wallpaper) once it appears; keep KWin. It does
# not respawn, so a one-shot kill is enough.
( for _ in $(seq 1 90); do
    if pgrep -x plasmashell >/dev/null 2>&1; then pkill -x plasmashell 2>/dev/null; break; fi
    sleep 1
  done ) &

# Keep FreeCAD up: launch it maximized; relaunch if it exits. Guard against a
# tight crash loop (5 exits in under 5s each → give up so we don't spin).
fast_fails=0
while true; do
  # Make FreeCAD truly fullscreen (fills the display, no decorations/margin) so
  # it looks native — no black border. Match by window CLASS (precise: skips the
  # transient startup windows) and re-apply for a while, since the window appears
  # late and the display may resize (SELKIES_ENABLE_RESIZE) when a client connects.
  # ~120s window so a slow first-run (FreeCAD builds caches, main window can take
  # 40-60s) is always caught.
  ( for _ in $(seq 1 120); do
      for wid in $(xdotool search --class 'FreeCAD' 2>/dev/null); do
        wmctrl -ir "$wid" -b remove,maximized_vert,maximized_horz 2>/dev/null
        wmctrl -ir "$wid" -b add,fullscreen 2>/dev/null
      done
      sleep 1
    done ) &

  start="$(date +%s)"
  /opt/freecad/AppRun "$@"
  run_secs=$(( "$(date +%s)" - start ))

  if [ "$run_secs" -lt 5 ]; then
    fast_fails=$(( fast_fails + 1 ))
    [ "$fast_fails" -ge 5 ] && { echo "FreeCAD exited <5s, 5x in a row — stopping kiosk loop"; break; }
  else
    fast_fails=0
  fi
  sleep 1
done
