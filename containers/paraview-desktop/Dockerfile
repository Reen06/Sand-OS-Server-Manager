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
