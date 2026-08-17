# flow5 streamer — full flow5 (potential-flow aero/hydro design tool, successor to xflr5)
# streamed to the browser over WebRTC.
#
# Base: Selkies nvidia-egl-desktop (headless KDE + Xvfb + VirtualGL + Selkies WebRTC/HTML5 on
# :8080), same shape as containers/freecad-streamer. GPU-accelerated GL + NVENC when run with the
# NVIDIA container runtime; software GL + x264enc encode as a fallback for dev boxes.
#
# UNLIKE freecad-streamer, this does NOT start from a prebuilt upstream binary — flow5 ships no
# Linux release at all (only .dmg and a Windows .zip; see the project log entry dated 2026-08-17,
# "flow5 as a Hub app"), so this image builds it from source at image-build time, against a fork
# (Reen06/flow5, branch ubuntu24.04-linux-build) carrying the patch set that makes it build and
# run on Ubuntu 24.04's actual apt-packaged Qt6/OpenCascade/Gmsh versions — none of the patches
# are workarounds specific to this container; they're genuine gaps between what flow5's own .pro
# files assume (Fedora paths, OCCT 7.9.2, Gmsh SDK 4.14.1) and what Debian/Ubuntu ships. See that
# fork branch's own commit message for the full, itemized list.

ARG BASE_TAG=24.04
FROM ghcr.io/selkies-project/nvidia-egl-desktop:${BASE_TAG} AS build

USER root

# The same toolchain combination that was verified (by hand, iteratively) to actually build
# flow5 — see the fork branch commit message for how each version gap was found and worked
# around. occt-misc pulls in the DRAWEXE-adjacent pieces some OCCT dev headers reference.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git ca-certificates pkg-config \
    qt6-base-dev qt6-base-dev-tools qmake6 \
    libocct-data-exchange-dev libocct-draw-dev libocct-foundation-dev \
    libocct-modeling-algorithms-dev libocct-modeling-data-dev libocct-ocaf-dev \
    libocct-visualization-dev occt-misc \
    libgmsh-dev libgmsh4.12t64 \
    libopenblas-dev liblapack-dev \
    libglu1-mesa-dev freeglut3-dev mesa-common-dev \
    && rm -rf /var/lib/apt/lists/*

# Debian/Ubuntu's libopenblas-dev doesn't ship openblas/lapack.h at all (Fedora-only packaging
# convention) and flow5's #include <openblas/cblas.h> needs the real Debian cblas.h at that
# specific path — see the fork commit message, points 4-5, for the full reasoning. lapack.h here
# is a minimal hand-written shim (just the 7 LAPACK routines flow5 actually calls); cblas.h and
# openblas_config.h are verbatim copies of Ubuntu's own headers, just relocated.
COPY shim-include /shim-include

ARG FLOW5_GIT_URL=https://github.com/Reen06/flow5.git
ARG FLOW5_GIT_REF=ubuntu24.04-linux-build
RUN git clone --depth 1 --branch "${FLOW5_GIT_REF}" "${FLOW5_GIT_URL}" /src

RUN set -eux; \
    mkdir -p /build; \
    cd /build; \
    qmake6 QMAKE_CXXFLAGS+=-I/shim-include /src/flow5.pro; \
    make -j"$(nproc)"

# Runtime image: the build stage's toolchain (Qt dev headers, compilers, git) has no business in
# what actually ships — only the shared libs flow5 links against at RUNTIME are needed, which the
# base Selkies image doesn't have installed by default (it's not a Qt/CAD image like FreeCAD's
# AppImage brings its own libs).
FROM ghcr.io/selkies-project/nvidia-egl-desktop:${BASE_TAG}
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    libqt6core6t64 libqt6gui6t64 libqt6widgets6t64 libqt6opengl6t64 libqt6openglwidgets6t64 \
    libqt6xml6t64 libqt6printsupport6t64 \
    libocct-foundation-7.6t64 libocct-modeling-data-7.6t64 libocct-modeling-algorithms-7.6t64 \
    libocct-visualization-7.6t64 libocct-ocaf-7.6t64 libocct-data-exchange-7.6t64 libocct-draw-7.6t64 \
    libgmsh4.12t64 libopenblas0-pthread liblapack3 \
    libglu1-mesa libgl1 \
    && rm -rf /var/lib/apt/lists/*

# The app binary + its 3 sibling shared libs (XFoil/flow5-lib/flow5-io-lib — see flow5.pro's
# SUBDIRS) — flat into one directory rather than preserving the build tree's ../XFoil-lib etc.
# layout, so the kiosk launcher's LD_LIBRARY_PATH is one directory, not three.
RUN mkdir -p /opt/flow5
COPY --from=build /build/flow5-app/flow5 /opt/flow5/flow5
COPY --from=build /build/XFoil-lib/libXFoil.so.1.1.0 /opt/flow5/
COPY --from=build /build/flow5-lib/libflow5-lib.so.1.0.0 /opt/flow5/
COPY --from=build /build/flow5-io-lib/libflow5-io-lib.so.1.0.0 /opt/flow5/
RUN cd /opt/flow5 && \
    ln -sf libXFoil.so.1.1.0 libXFoil.so.1 && \
    ln -sf libflow5-lib.so.1.0.0 libflow5-lib.so.1 && \
    ln -sf libflow5-io-lib.so.1.0.0 libflow5-io-lib.so.1 && \
    chmod +x flow5
# The mac iconset's 512x512 PNG, not meta/res/flow5.png (only 128x128) — sharper for the PWA
# icon and desktop entry, both of which scale an "any size" source down as needed.
COPY --from=build /src/meta/mac/iconset/appicon.iconset/icon_512x512.png /opt/flow5/flow5.png

# Make the web client PROXY-FRIENDLY the same way freecad-streamer does: it builds the
# signalling WebSocket and the TURN-creds fetch from ABSOLUTE paths (/{appName}/signalling/,
# /turn), which break when served under a Hub subpath (e.g. /apps/stream/flow5/). Rewritten to be
# relative to the current path — works both at root and behind the proxy.
RUN sed -i \
      -e 's#"/" + app.appName + "/signalling/"#window.location.pathname + "webrtc/signalling/"#g' \
      -e 's#fetch("/turn")#fetch("turn")#g' \
      -e 's#fetch("/publish/" + app.appName)#fetch("publish/" + app.appName)#g' \
      /opt/gst-web/app.js

# Replace the Selkies caching service worker with a PASSTHROUGH one (no caching, so no stale
# client shell) — same file freecad-streamer uses, verbatim: its only job is registering a real
# fetch handler so the streamed app is installable as a PWA, nothing here is FreeCAD-specific.
COPY freecad-sw.js /opt/gst-web/sw.js
COPY flow5-pwa-manifest.json /opt/gst-web/manifest.json
COPY --from=build /src/meta/mac/iconset/appicon.iconset/icon_512x512.png /opt/gst-web/flow5.png

# Breeze Dark by default — same reasoning as freecad-streamer: the base image ships the
# look-and-feel package but doesn't set the colour scheme, so it comes up light by default.
RUN set -eux; \
    for h in /home/ubuntu /root; do \
      [ -d "$h" ] || continue; \
      mkdir -p "$h/.config"; \
      printf '%s\n' '[General]' 'ColorScheme=BreezeDark' \
        'LookAndFeelPackage=org.kde.breezedark.desktop' 'widgetStyle=Breeze' \
        >> "$h/.config/kdeglobals"; \
    done; \
    chown -R 1000:1000 /home/ubuntu/.config 2>/dev/null || true

COPY flow5-kiosk.sh /usr/local/bin/flow5-kiosk.sh
RUN chmod +x /usr/local/bin/flow5-kiosk.sh

COPY flow5.desktop /usr/share/applications/tech.flow5.flow5.desktop
RUN set -eux; \
    for d in /home/ubuntu /root /etc/skel; do \
      mkdir -p "$d/.config/autostart" "$d/Desktop"; \
      cp /usr/share/applications/tech.flow5.flow5.desktop "$d/.config/autostart/flow5.desktop"; \
      cp /usr/share/applications/tech.flow5.flow5.desktop "$d/Desktop/flow5.desktop"; \
    done

# Inherit the base image's ENTRYPOINT/CMD (starts Selkies + KDE). Do not override.

LABEL sandos.ui.app_scale="false"
