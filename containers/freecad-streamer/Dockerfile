# FreeCAD streamer — full FreeCAD streamed to the browser over WebRTC.
#
# Base: Selkies nvidia-egl-desktop (headless KDE + Xvfb + VirtualGL + Selkies
# WebRTC/HTML5 on :8080). GPU-accelerated GL + NVENC when run with the NVIDIA
# container runtime; software GL + x264enc encode as a fallback for dev boxes.
# See ./README.md.  Design: vault Remote FreeCAD + Server Manager Apps & Instances.

ARG BASE_TAG=24.04
FROM ghcr.io/selkies-project/nvidia-egl-desktop:${BASE_TAG}

# Which FreeCAD to bundle. Default = newest weekly (26.3dev — FreeCAD moved to
#   CalVer; 1.1.1 remains the latest *stable* but lacks the 26.x features).
#   Note: the AppImage lives on FreeCAD/FreeCAD releases, NOT FreeCAD/FreeCAD-Bundle
#   (the Bundle repo lags). Override for stable / a different weekly with:
#   --build-arg FREECAD_APPIMAGE_URL=https://github.com/FreeCAD/FreeCAD/releases/download/<tag>/<asset>.AppImage
#   (stable: .../download/1.1.1/FreeCAD_1.1.1-Linux-x86_64-py311.AppImage)
ARG FREECAD_APPIMAGE_URL=https://github.com/FreeCAD/FreeCAD/releases/download/weekly-2026.07.09/FreeCAD_weekly-2026.07.09-Linux-x86_64.AppImage

USER root

# Fix a real bug in the base image's vendored selkies-gstreamer (1.6.2):
# on_signalling_error/on_audio_signalling_error are `async def` but call the
# BLOCKING `time.sleep(2)` on every "no peer yet" retry, stalling the whole
# single-threaded asyncio signalling loop for 2s each time. After a few
# connect/disconnect cycles these stalls compound and the signalling
# WebSocket stops servicing new offers entirely — the exact "stream refuses
# to reconnect after ~3 disconnects, needs a full container restart" bug.
# `await asyncio.sleep(2)` is the correct, non-blocking equivalent.
RUN set -eux; \
    f=/usr/local/lib/python3.12/dist-packages/selkies_gstreamer/__main__.py; \
    grep -q "time.sleep(2)" "$f"; \
    sed -i "s/time\.sleep(2)/await asyncio.sleep(2)/g" "$f"; \
    ! grep -q "           time.sleep(2)" "$f"

# Fetch + unpack the AppImage (extract, not FUSE-mount, so it works in-build).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    curl -fsSL "${FREECAD_APPIMAGE_URL}" -o /tmp/freecad.AppImage; \
    chmod +x /tmp/freecad.AppImage; \
    cd /opt; \
    /tmp/freecad.AppImage --appimage-extract >/dev/null; \
    mv squashfs-root freecad; \
    rm -f /tmp/freecad.AppImage; \
    ln -sf /opt/freecad/AppRun /usr/local/bin/freecad

# Auto-resize the remote display to match the browser window → the stream fills
# the viewport with no letterboxing (combine with browser fullscreen / PWA install
# for a native, edge-to-edge look). Placed after the FreeCAD layer to keep cache.
ENV SELKIES_ENABLE_RESIZE=true

# PWA branding: install as "FreeCAD" (not "Selkies WebRTC"), with the FreeCAD
# icon. The web client already ships a manifest + service worker; we just rebrand
# it. NOTE: the "Install app" button only appears over HTTPS (secure context) —
# run with HTTPS (see run-lan.sh) or front it with the Hub's TLS proxy.
COPY freecad-pwa-manifest.json /opt/gst-web/manifest.json
# (find, not a fixed path — weekly AppImages have moved the icon around)
RUN set -eux; \
    icon="$(find /opt/freecad -name 'org.freecad.FreeCAD*.svg' | head -1)"; \
    cp "$icon" /opt/gst-web/freecad.svg

# Make the web client PROXY-FRIENDLY: it builds the signalling WebSocket and the
# TURN-creds fetch from ABSOLUTE paths (/{appName}/signalling/, /turn), which
# break when served under a Hub subpath (e.g. /apps/stream/freecad/). Rewrite them
# to be relative to the current path — works both at root and behind the proxy.
RUN sed -i \
      -e 's#"/" + app.appName + "/signalling/"#window.location.pathname + "webrtc/signalling/"#g' \
      -e 's#fetch("/turn")#fetch("turn")#g' \
      -e 's#fetch("/publish/" + app.appName)#fetch("publish/" + app.appName)#g' \
      /opt/gst-web/app.js
# NOTE: the client's own `navigator.serviceWorker.register('./sw.js?ts=…')` is now
# LEFT INTACT — it registers our passthrough SW below (so FreeCAD is installable).
# (Previously this was rewritten to a getRegistrations().unregister() loop to kill
# the old caching SW; that also wiped the dashboard's SW on the same origin.)

# Replace the Selkies caching service worker with a PASSTHROUGH one: no caching
# (so no stale-client shell) but a real fetch handler → the streamed app is
# installable as a PWA without breaking the WebRTC stream.
COPY freecad-sw.js /opt/gst-web/sw.js

# Kiosk launcher: stream FreeCAD ONLY (no KDE panel/desktop). Used by the
# autostart entry below.
COPY freecad-kiosk.sh /usr/local/bin/freecad-kiosk.sh
RUN chmod +x /usr/local/bin/freecad-kiosk.sh

# App-menu entry (reliable) + KDE autostart so the session boots straight into
# FreeCAD (kiosk). The base image's KDE session runs as ROOT with HOME=/home/ubuntu
# (root reads/writes any file regardless of owner, so root ownership here was never
# required) — /etc/skel is NOT seeded because that home already exists in the base
# image. (The session sets LD_PRELOAD=VirtualGL globally, so autostarted FreeCAD
# gets GPU GL automatically.) /etc/skel + /root are kept as belt-and-suspenders.
COPY freecad.desktop /usr/share/applications/org.freecad.FreeCAD.desktop
RUN set -eux; \
    for d in /home/ubuntu /root /etc/skel; do \
      mkdir -p "$d/.config/autostart" "$d/Desktop"; \
      cp /usr/share/applications/org.freecad.FreeCAD.desktop "$d/.config/autostart/freecad.desktop"; \
      cp /usr/share/applications/org.freecad.FreeCAD.desktop "$d/Desktop/freecad.desktop"; \
    done; \
    # /home/ubuntu/.config + Desktop are per-user NFS mounts (sm-nfs-*-freecad-config,
    # Mount "freecad-config" in registry.py). The NFS export all_squashes every client
    # to uid 1000 (see containers/nfs-server/run-nas.sh), so on a brand-new user's
    # empty volume, Docker's populate-from-image step fails trying to chown these to
    # root:root ("failed to chown ... permission denied") — a squashed client can never
    # become uid 0. Owning them as 1000:1000 up front matches anonuid so that populate
    # step is a same-uid chown, which is permitted without privilege (POSIX 4.1: owner
    # chowning to itself). Fixed 2026-07-13 after every existing user's freecad-config
    # volume turned out empty/never-successfully-populated for this reason.
    chown -R 1000:1000 /home/ubuntu/.config /home/ubuntu/Desktop

# Inherit the base image's ENTRYPOINT/CMD (starts Selkies + KDE). Do not override.
