# KiCad streamer — full KiCad (schematic capture + PCB layout + 3D viewer)
# streamed to the browser over WebRTC.
#
# Base: Selkies nvidia-egl-desktop (headless KDE + Xvfb + VirtualGL + Selkies
# WebRTC/HTML5 on :8080). GPU-accelerated GL + NVENC when run with the NVIDIA
# container runtime; software GL + x264enc encode as a fallback for dev boxes.
# Copied from containers/freecad-streamer/ — see vault
# "Streamed Apps — The Selkies Base Patches" for why all four patches below
# are mandatory on EVERY image built on this base, not just FreeCAD's.

ARG BASE_TAG=24.04
FROM ghcr.io/selkies-project/nvidia-egl-desktop:${BASE_TAG}

# KiCad ships its official Linux build as a .tar containing the AppImage
# (not a bare .AppImage file directly), e.g.
#   https://mirrors.mit.edu/kicad/appimage/stable/kicad-10.0.5-x86_64.AppImage.tar
# STABLE, not the nightly/testing PPA build — this app is for daily use, not
# tracking KiCad's development branch. Override with --build-arg to test another.
ARG KICAD_APPIMAGE_TAR_URL=https://mirrors.mit.edu/kicad/appimage/stable/kicad-10.0.5-x86_64.AppImage.tar

USER root

# Patch 2 (selkies-gstreamer blocking sleep) — see the vault page for why this
# is a real base-image bug independent of which app sits on top.
RUN set -eux; \
    f=/usr/local/lib/python3.12/dist-packages/selkies_gstreamer/__main__.py; \
    grep -q "time.sleep(2)" "$f"; \
    sed -i "s/time\.sleep(2)/await asyncio.sleep(2)/g" "$f"; \
    ! grep -q "           time.sleep(2)" "$f"

# Patch 3 (hidden Ctrl+Shift+LeftClick pointer lock). KiCad uses Ctrl+Click
# (add to selection) and Shift+Click (drag-select / measure) constantly in
# both the schematic and PCB editors, so this would grab the pointer
# mid-workflow with no warning — same reasoning as FreeCAD.
RUN set -eux; \
    f=/opt/gst-web/input.js; \
    grep -q "if (down && event.button === 0 && event.ctrlKey && event.shiftKey) {" "$f"; \
    sed -i "s/if (down && event.button === 0 && event.ctrlKey && event.shiftKey) {/if (false \&\& down \&\& event.button === 0 \&\& event.ctrlKey \&\& event.shiftKey) { \/\/ disabled: surprised users, KiCad uses Ctrl+Shift+Click for selection\/measure/" "$f"; \
    grep -q "if (false && down" "$f"

# Fetch + unpack KiCad. The upstream artifact is a .tar wrapping the real
# .AppImage (not a bare AppImage), so unpack the tar first, then extract the
# AppImage itself (not FUSE-mount, so it works in-build). `find` locates the
# actual .AppImage inside the tar rather than hardcoding its exact filename,
# since that filename embeds the version.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates libglu1-mesa; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /tmp/kicad-dl; \
    curl -fsSL "${KICAD_APPIMAGE_TAR_URL}" -o /tmp/kicad-dl/kicad.AppImage.tar; \
    tar -xf /tmp/kicad-dl/kicad.AppImage.tar -C /tmp/kicad-dl; \
    appimage="$(find /tmp/kicad-dl -maxdepth 1 -name '*.AppImage' | head -1)"; \
    chmod +x "$appimage"; \
    cd /opt; \
    "$appimage" --appimage-extract >/dev/null; \
    mv squashfs-root kicad; \
    rm -rf /tmp/kicad-dl; \
    ln -sf /opt/kicad/AppRun /usr/local/bin/kicad

# Auto-resize the remote display to match the browser window — same trade as
# FreeCAD: the stream fills the viewport, Selkies' client-rendered cursor can
# misbehave at odd aspect ratios in exchange.
ENV SELKIES_ENABLE_RESIZE=true

# PWA branding: install as "KiCad" (not "Selkies WebRTC"), with the KiCad icon.
COPY kicad-pwa-manifest.json /opt/gst-web/manifest.json
RUN set -eux; \
    icon="$(find /opt/kicad -name 'org.kicad_pcb.KiCad.svg' -o -name 'kicad.svg' | head -1)"; \
    cp "$icon" /opt/gst-web/kicad.svg

# Patch 1 (MANDATORY — absolute proxy paths). Without this the stream never
# connects behind the Hub's /apps/stream/kicad/ subpath: loading bars render
# with no status text under them, which looks like a hung app.
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

# Patch 4 (replace the caching service worker with a passthrough one) — keeps
# the app installable as a PWA without ever masking a rebuilt image behind a
# stale cached client shell.
COPY kicad-sw.js /opt/gst-web/sw.js

# Breeze Dark by default — same as every other streamed app in this fleet, so
# the desktop chrome is consistent even though KiCad's own canvas colours are
# set separately inside its own preferences.
RUN set -eux; \
    for h in /home/ubuntu /root; do \
      [ -d "$h" ] || continue; \
      mkdir -p "$h/.config"; \
      printf '%s\n' '[General]' 'ColorScheme=BreezeDark' \
        'LookAndFeelPackage=org.kde.breezedark.desktop' 'widgetStyle=Breeze' \
        >> "$h/.config/kdeglobals"; \
      printf '%s\n' '[General]' 'ColorScheme=BreezeDark' \
        >> "$h/.config/kdedefaults/kdeglobals" 2>/dev/null || true; \
    done; \
    chown -R 1000:1000 /home/ubuntu/.config 2>/dev/null || true

# Stop a click-then-drag on a titlebar being read as a double-click-maximise —
# same KWin trap as FreeCAD, not toolkit-specific.
RUN set -eux; \
    for h in /home/ubuntu /root; do \
      [ -d "$h" ] || continue; \
      mkdir -p "$h/.config"; \
      printf '%s\n' '[Windows]' 'TitlebarDoubleClickCommand=Nothing' \
        >> "$h/.config/kwinrc"; \
      printf '%s\n' '[KDE]' 'DoubleClickInterval=250' \
        >> "$h/.config/kdeglobals"; \
    done; \
    chown -R 1000:1000 /home/ubuntu/.config 2>/dev/null || true

# Kiosk launcher: starts KiCad's project manager on the full KDE desktop
# (panel/wallpaper kept, same reasoning as FreeCAD's launcher).
COPY kicad-kiosk.sh /usr/local/bin/kicad-kiosk.sh
RUN chmod +x /usr/local/bin/kicad-kiosk.sh

# App-menu entry + KDE autostart so the session boots straight into KiCad.
# The base image's KDE session runs as ROOT with HOME=/home/ubuntu (root
# reads/writes any file regardless of owner), matching FreeCAD's setup.
COPY kicad.desktop /usr/share/applications/org.kicad.KiCad.desktop
RUN set -eux; \
    for d in /home/ubuntu /root /etc/skel; do \
      mkdir -p "$d/.config/autostart" "$d/Desktop"; \
      cp /usr/share/applications/org.kicad.KiCad.desktop "$d/.config/autostart/kicad.desktop"; \
      cp /usr/share/applications/org.kicad.KiCad.desktop "$d/Desktop/kicad.desktop"; \
    done; \
    # /home/ubuntu/.config + Desktop are per-user NFS mounts, all_squash'd to
    # uid 1000 (see nfs-server/run-nas.sh) — owning these 1000:1000 up front
    # matches anonuid so Docker's populate-from-image chown on a brand-new
    # user's empty volume is a same-uid chown, not a root-owned one that
    # fails on the squashed export. Same fix FreeCAD needed 2026-07-13.
    chown -R 1000:1000 /home/ubuntu/.config /home/ubuntu/Desktop

# Inherit the base image's ENTRYPOINT/CMD (starts Selkies + KDE). Do not override.

# KiCad is wxWidgets, not Qt — there is no single QT_SCALE_FACTOR-style knob
# that scales the whole application the way FreeCAD's does, so this app does
# not offer an "app scale" slider in its settings panel.
LABEL sandos.ui.app_scale="false"
