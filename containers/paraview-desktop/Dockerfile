# ParaView desktop — the full Qt application, streamed to the browser.
#
# Deliberately SEPARATE from the existing `paraview` app, which is ParaViewWeb:
# a different program with a different feature set, not a lesser skin on this
# one. ParaViewWeb runs a fixed visualizer in the browser and renders with
# OSMesa on the CPU; this is the real desktop client, with the full filter
# library, the Python shell, plugins, and GPU rendering. Someone opening a large
# dataset wants this; someone glancing at a result on a phone wants that. Both
# stay.
#
# Base: Selkies nvidia-egl-desktop (headless KDE + Xvfb + VirtualGL + Selkies
# WebRTC on :8080), the same base FreeCAD uses — see
# ../freecad-streamer/Dockerfile, whose comments cover the session mechanics
# this image inherits rather than repeating them here.

ARG BASE_TAG=24.04
FROM ghcr.io/selkies-project/nvidia-egl-desktop:${BASE_TAG}

# Kitware's own Linux binary. Not a distro package: Ubuntu's `paraview` lags
# badly and links against the system VTK, which drags in a second GL stack that
# fights VirtualGL. The official tarball is self-contained apart from X libs.
#
# MPI build on purpose — it is the one Kitware publishes with the full feature
# set, and pvserver/pvbatch come with it for later use even though the desktop
# session does not need them.
ARG PARAVIEW_VERSION=5.13.3
ARG PARAVIEW_TARBALL=ParaView-${PARAVIEW_VERSION}-MPI-Linux-Python3.10-x86_64.tar.gz
ARG PARAVIEW_URL=https://www.paraview.org/paraview-downloads/download.php?submit=Download&version=v5.13&type=binary&os=Linux&downloadFile=${PARAVIEW_TARBALL}

USER root

# X/xcb libraries the bundled Qt needs and a headless base does not ship.
# libxcb-cursor0 is the one that bites: Qt refuses to start with "could not load
# the Qt platform plugin xcb" and names every library EXCEPT the missing one, so
# the error points nowhere near the cause.
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
      libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 \
      libxcb-xkb1 libxkbcommon-x11-0 libnss3 libasound2t64 \
      libopengl0 libglx-mesa0 libgomp1 \
      wmctrl xdotool; \
    rm -rf /var/lib/apt/lists/*

# Extracted with --strip-components so the path has no version in it: the launch
# script, the .desktop entry and the app-scale label all stay correct across a
# version bump, which is then a one-line ARG change.
RUN set -eux; \
    mkdir -p /opt/paraview; \
    curl -fL --retry 3 --retry-delay 5 -o /tmp/pv.tar.gz "${PARAVIEW_URL}"; \
    tar -xzf /tmp/pv.tar.gz -C /opt/paraview --strip-components=1; \
    rm -f /tmp/pv.tar.gz; \
    test -x /opt/paraview/bin/paraview

# Two fixes to the base image, both carried over from ../freecad-streamer —
# they are properties of this Selkies base, not of the app on top of it, so any
# image built on it needs them. Placed AFTER the ParaView download so a change
# here does not invalidate that ~690MB layer.
#
# 1. selkies-gstreamer 1.6.2 ships on_signalling_error/on_audio_signalling_error
#    as `async def` that call the BLOCKING time.sleep(2) on every "no peer yet"
#    retry, stalling the single-threaded asyncio signalling loop for 2s each
#    time. Symptom seen live on this image: the viewer's loading bars render but
#    the status text ("connecting to stream", "registering with peer id…") never
#    appears, because signalling progress never reaches the browser. Compounding
#    stalls also stop the socket servicing new offers at all, which is the
#    "refuses to reconnect after a few disconnects" bug.
RUN set -eux; \
    f=/usr/local/lib/python3.12/dist-packages/selkies_gstreamer/__main__.py; \
    grep -q "time.sleep(2)" "$f"; \
    sed -i "s/time\\.sleep(2)/await asyncio.sleep(2)/g" "$f"; \
    ! grep -q "           time.sleep(2)" "$f"

# 2. gst-web's hidden Ctrl+Shift+LeftClick -> requestPointerLock() hotkey.
#    ParaView uses Ctrl+Click and Shift+Click constantly for selection and
#    camera work, so this silently grabs the pointer mid-workflow and reads as
#    an unwanted auto-fullscreen. Same reasoning as FreeCAD, same fix.
RUN set -eux; \
    f=/opt/gst-web/input.js; \
    grep -q "if (down && event.button === 0 && event.ctrlKey && event.shiftKey) {" "$f"; \
    sed -i "s/if (down && event.button === 0 && event.ctrlKey && event.shiftKey) {/if (false \&\& down \&\& event.button === 0 \&\& event.ctrlKey \&\& event.shiftKey) { \/\/ disabled: ParaView uses Ctrl+Shift+Click for selection/" "$f"; \
    grep -q "if (false && down" "$f"

# THE ONE THAT BREAKS THE STREAM BEHIND THE HUB.
#
# The web client builds its signalling WebSocket and TURN-credentials fetch from
# ABSOLUTE paths — "/{appName}/signalling/" and "/turn" — which resolve to the
# Hub's own root instead of the app's subpath (/apps/stream/paraview-desktop/)
# and 404. The stream then never starts and, because signalling never progresses,
# the viewer's status line stays BLANK: no "Connecting to server", no
# "Registering with server, peer ID: …". The loading bars render because they are
# static markup, which makes it look like a hung app rather than a routing bug.
#
# Observed exactly that on this image before this patch. Rewriting the paths to be
# relative to the current location works both at the root and behind the proxy.
RUN set -eux; \
    f=/opt/gst-web/app.js; \
    grep -q '"/" + app.appName + "/signalling/"' "$f"; \
    sed -i \
      -e 's#"/" + app.appName + "/signalling/"#window.location.pathname + "webrtc/signalling/"#g' \
      -e 's#fetch("/turn")#fetch("turn")#g' \
      -e 's#fetch("/publish/" + app.appName)#fetch("publish/" + app.appName)#g' \
      "$f"; \
    grep -q 'window.location.pathname + "webrtc/signalling/"' "$f"; \
    ! grep -q 'fetch("/turn")' "$f"

# Auto-resize the remote display to match the browser window, so the stream fills
# the viewport with no letterboxing.
ENV SELKIES_ENABLE_RESIZE=true

# Replace the Selkies CACHING service worker with a passthrough one: no caching,
# so the client shell can never go stale against a rebuilt image, but a real
# fetch handler so the app stays installable. Same file FreeCAD uses — the
# behaviour wanted is identical and duplicating it would let the two drift.
COPY paraview-sw.js /opt/gst-web/sw.js

COPY paraview-launch.sh /usr/local/bin/paraview-launch.sh
RUN chmod +x /usr/local/bin/paraview-launch.sh

# App-menu entry + KDE autostart, so the session opens straight into ParaView
# while leaving the desktop reachable behind it. Same three-home seeding as
# FreeCAD: the KDE session runs as root with HOME=/home/ubuntu, and /etc/skel is
# not consulted for a home that already exists in the base image.
COPY paraview.desktop /usr/share/applications/org.paraview.ParaView.desktop
RUN set -eux; \
    for d in /home/ubuntu /root /etc/skel; do \
      mkdir -p "$d/.config/autostart" "$d/Desktop"; \
      cp /usr/share/applications/org.paraview.ParaView.desktop "$d/.config/autostart/paraview.desktop"; \
      cp /usr/share/applications/org.paraview.ParaView.desktop "$d/Desktop/paraview.desktop"; \
    done; \
    chown -R 1000:1000 /home/ubuntu/.config /home/ubuntu/Desktop

# Inherit the base image's ENTRYPOINT/CMD (starts Selkies + KDE). Do not override.

# What this app's settings panel should offer.
#
# Declared by the IMAGE because it is a fact about what is inside this
# container: ParaView is Qt, so QT_SCALE_FACTOR scales all of it and one slider
# is the whole story. Contrast Isaac Sim, where Omniverse Kit ignores
# QT_SCALE_FACTOR and genuinely needs a second control.
#
# LAST on purpose: a label is metadata, so adding or changing it rebuilds one
# trivial layer instead of invalidating the ~700MB behind it.
LABEL sandos.ui.app_scale="false"
