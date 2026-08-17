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

# Force Breeze Dark on the LIVE session.
#
# The image bakes a dark kdeglobals into /home/ubuntu/.config, but that path is
# a per-user NAS bind mount at runtime — the mount MASKS whatever the image put
# there, so the session reads the NAS copy instead and comes up light. Seeding
# only fills that directory when it is empty on first launch, so an already-
# populated one keeps whatever it had and never picks the theme up.
#
# Writing the files here fixes the config for next boot; plasma-apply-colorscheme
# fixes the session that is already running, so it goes dark now rather than on
# the next relaunch. Both, because either alone leaves one of the two wrong.
( mkdir -p "$HOME/.config/kdedefaults"
  for f in "$HOME/.config/kdeglobals" "$HOME/.config/kdedefaults/kdeglobals"; do
    if ! grep -q '^ColorScheme=BreezeDark' "$f" 2>/dev/null; then
      printf '%s\n' '[General]' 'ColorScheme=BreezeDark' \
        'LookAndFeelPackage=org.kde.breezedark.desktop' 'widgetStyle=Breeze' >> "$f"
    fi
  done
  # The PLASMA SHELL (panel, desktop, widgets) is themed separately from the
  # colour scheme. Setting only the colour scheme styles application windows
  # and leaves the shell light — which is exactly what happened:
  # plasma-apply-colorscheme reported "BreezeDark is already set" while the
  # desktop still rendered light, because the shell reads [Theme] name from
  # plasmarc and that was still the default.
  if ! grep -q 'name=breeze-dark' "$HOME/.config/plasmarc" 2>/dev/null; then
    printf '%s\n' '[Theme]' 'name=breeze-dark' >> "$HOME/.config/plasmarc"
  fi
  # Wait for plasma before applying live — this runs from a KDE autostart entry,
  # so the shell is usually up, but not guaranteed to be ready to take a call.
  for _ in $(seq 1 30); do
    pgrep -x plasmashell >/dev/null && break
    sleep 1
  done
  # look-and-feel sets colour scheme, plasma theme and widget style together,
  # so it covers the shell as well as the apps. Colour scheme is applied after
  # it too: the look-and-feel package can carry its own, and we want ours to
  # win if they ever disagree.
  plasma-apply-lookandfeel -a org.kde.breezedark.desktop >/tmp/sm-darkmode.log 2>&1 || true
  plasma-apply-colorscheme BreezeDark >>/tmp/sm-darkmode.log 2>&1 || true
) &

# No auto-maximize: KiCad's own window placement is left alone, so its
# project manager and each editor open where the window manager puts them,
# and any size the user sets sticks. (Earlier versions maximized new windows
# from a background loop; KiCad handles this itself.) Removed 2026-08-17.

# Size KiCad's own chrome from the full-UI slider. KiCad ignores GDK_SCALE
# (verified live), so this writes toolbar_icon_size where KiCad reads it —
# see apply-kicad-scale.py. Runs every launch, so reopening the app inside
# the streamed desktop picks up a new value with no container restart.
/usr/local/bin/apply-kicad-scale.py || true

/opt/kicad/AppRun "$@"
