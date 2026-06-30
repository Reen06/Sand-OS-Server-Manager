# FreeCAD streamer — full FreeCAD streamed to the browser over WebRTC.
#
# Base: Selkies nvidia-egl-desktop (headless KDE + Xvfb + VirtualGL + Selkies
# WebRTC/HTML5 on :8080). GPU-accelerated GL + NVENC when run with the NVIDIA
# container runtime; software GL + x264enc encode as a fallback for dev boxes.
# See ./README.md.  Design: vault Remote FreeCAD + Server Manager Apps & Instances.

ARG BASE_TAG=24.04
FROM ghcr.io/selkies-project/nvidia-egl-desktop:${BASE_TAG}

# Which FreeCAD to bundle. Default = 1.1.1 stable (official AppImage, main repo).
#   Note: the AppImage lives on FreeCAD/FreeCAD releases, NOT FreeCAD/FreeCAD-Bundle
#   (the Bundle repo lags). Override for a different version / weekly with:
#   --build-arg FREECAD_APPIMAGE_URL=https://github.com/FreeCAD/FreeCAD/releases/download/<tag>/<asset>.AppImage
ARG FREECAD_APPIMAGE_URL=https://github.com/FreeCAD/FreeCAD/releases/download/1.1.1/FreeCAD_1.1.1-Linux-x86_64-py311.AppImage

USER root

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
RUN cp /opt/freecad/usr/share/icons/hicolor/scalable/apps/org.freecad.FreeCAD.svg /opt/gst-web/freecad.svg

# Kiosk launcher: stream FreeCAD ONLY (no KDE panel/desktop). Used by the
# autostart entry below.
COPY freecad-kiosk.sh /usr/local/bin/freecad-kiosk.sh
RUN chmod +x /usr/local/bin/freecad-kiosk.sh

# App-menu entry (reliable) + KDE autostart so the session boots straight into
# FreeCAD (kiosk). The base image's KDE session runs as ROOT with HOME=/home/ubuntu,
# so the autostart entry must live in /home/ubuntu/.config/autostart (root-owned) —
# /etc/skel is NOT seeded because that home already exists in the base image.
# (The session sets LD_PRELOAD=VirtualGL globally, so autostarted FreeCAD gets
# GPU GL automatically.) /etc/skel + /root are kept as belt-and-suspenders.
COPY freecad.desktop /usr/share/applications/org.freecad.FreeCAD.desktop
RUN set -eux; \
    for d in /home/ubuntu /root /etc/skel; do \
      mkdir -p "$d/.config/autostart" "$d/Desktop"; \
      cp /usr/share/applications/org.freecad.FreeCAD.desktop "$d/.config/autostart/freecad.desktop"; \
      cp /usr/share/applications/org.freecad.FreeCAD.desktop "$d/Desktop/freecad.desktop"; \
    done; \
    chown -R root:root /home/ubuntu/.config /home/ubuntu/Desktop

# Inherit the base image's ENTRYPOINT/CMD (starts Selkies + KDE). Do not override.
