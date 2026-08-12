#!/bin/bash
# ParaView launcher: starts ParaView maximised on the full KDE desktop.
#
# The desktop is deliberately left running — panel, wallpaper and app menu stay
# reachable, so a terminal or file manager is available without leaving the
# stream, and closing ParaView leaves you somewhere rather than on a dead black
# screen. Same decision as the FreeCAD launcher, for the same reason.
#
# Not respawned on exit. Tearing an instance down is the Server Manager's job
# (on disconnect/idle); relaunching here would fight a user who deliberately
# closed the app.
export DISPLAY="${DISPLAY:-:0}"

# Basic-auth fix inherited from the FreeCAD launcher: the runtime dir holding
# nginx's .htpasswd is created mode 700 by root, but nginx workers run as
# www-data, so login 500s until it is traversable.
( for _ in $(seq 1 60); do
    d="${XDG_RUNTIME_DIR:-/tmp/runtime-ubuntu}"
    if [ -f "$d/.htpasswd" ]; then chmod 755 "$d" 2>/dev/null; nginx -s reload 2>/dev/null; break; fi
    sleep 1
  done ) &

# ParaView writes its settings to ~/.config/ParaView/. That lives on the user's
# NAS home, so it persists across instances without any of the save-on-exit
# gymnastics FreeCAD needs — ParaView writes settings when they change, not
# only at a clean shutdown.
mkdir -p "$HOME/.config/ParaView" 2>/dev/null || true

# Maximise the main window once it appears.
#
# Matched by window NAME anchored to "ParaView <digit>", not by WM_CLASS: the
# class matches auxiliary windows too (the Python shell, the colour-map editor,
# progress dialogs), and maximising one of those strands its buttons on a
# screen-sized blank canvas — which reads as the app having hung. This is the
# lesson the FreeCAD launcher records after exactly that happened there.
#
# ~180s window rather than FreeCAD's 120: ParaView's first start builds shader
# caches and can take appreciably longer on a cold container.
( for _ in $(seq 1 180); do
    for wid in $(xdotool search --name '^ParaView [0-9]' 2>/dev/null); do
      # Maximised, not fullscreen: fullscreen covers the KDE panel, which
      # defeats the point of keeping the desktop reachable. F11 inside ParaView
      # still gives true fullscreen when it is wanted.
      wmctrl -ir "$wid" -b remove,fullscreen 2>/dev/null
      wmctrl -ir "$wid" -b add,maximized_vert,maximized_horz 2>/dev/null
    done
    sleep 1
  done ) &

# --force-onscreen-rendering: without it ParaView may pick an offscreen EGL
# path and render to nothing visible, because the base image advertises EGL for
# VirtualGL's benefit. The session already routes GL through VirtualGL, so
# onscreen is both correct and the accelerated path.
exec /opt/paraview/bin/paraview --force-onscreen-rendering "$@"
