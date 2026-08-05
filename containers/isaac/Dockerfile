# NVIDIA Isaac Sim AND Isaac Lab in one streamed image.
#
# Isaac's own image streams over the Omniverse protocol (47998/udp + 49100/tcp)
# to a native client. That cannot work here: the node this runs on is reached
# through a reverse link that carries TCP over a WebSocket, so UDP media has no
# path, and there is no browser UI to proxy either.
#
# So it is wrapped the same way FreeCAD is: the Selkies nvidia-egl-desktop base
# gives a GPU-accelerated X session with WebRTC/HTML5 on :8080, which the Hub
# already knows how to proxy and already has TURN for. Isaac is copied in from
# the official image rather than reinstalled, so the bits are byte-identical to
# the release and the build is a copy rather than a 20-minute installer run.
ARG SELKIES_TAG=24.04
# isaac-lab-base IS Isaac Sim plus Lab: Lab pip-installs into /isaac-sim's
# own python (22 packages there) and adds a 16MB workspace on top. Sourcing
# from it therefore costs almost nothing over Sim alone and gives both apps.
ARG ISAAC_IMAGE=isaac-lab-base:latest

FROM ${ISAAC_IMAGE} AS isaac

FROM ghcr.io/selkies-project/nvidia-egl-desktop:${SELKIES_TAG}
USER root

# /isaac-sim is self-contained (own python, exts, extscache) — copying it is
# enough, no package installs needed.
# --chown on the COPY, NOT a later `chown -R`: chown-ing 23GB of just-copied
# files in a RUN step rewrites every one of them into a second layer, so the
# image carried the whole tree TWICE — 57.7GB for ~23GB of content, which then
# would not fit on a peer node it was being transferred to. Doing it as part
# of the copy costs nothing. 1000 is the base image's desktop user.
COPY --from=isaac --chown=1000:1000 /isaac-sim /isaac-sim
COPY --from=isaac --chown=1000:1000 /workspace/isaaclab /workspace/isaaclab

# Isaac writes caches, logs and its EULA acceptance under $HOME at runtime.
# The desktop session runs as the base image's unprivileged user, so hand it
# ownership rather than running Isaac as root.
ARG DESKTOP_USER=ubuntu
RUN set -eux; \
    id -u "${DESKTOP_USER}" >/dev/null 2>&1 || DESKTOP_USER=user; \
    mkdir -p /home/"${DESKTOP_USER}"/.nvidia-omniverse/config \
             /home/"${DESKTOP_USER}"/.cache/ov \
             /home/"${DESKTOP_USER}"/.local/share/ov/data; \
    chown -R "${DESKTOP_USER}":"${DESKTOP_USER}" /home/"${DESKTOP_USER}"

# Accept the Omniverse EULA non-interactively: without it Isaac stops at a
# prompt no one can answer inside a streamed session.
ENV OMNI_KIT_ACCEPT_EULA=YES \
    ACCEPT_EULA=Y \
    PRIVACY_CONSENT=Y \
    ISAAC_PATH=/isaac-sim

# ── Selkies fixes carried over from the FreeCAD streamer ──────────────────────
# These are not optional polish; without the first one the stream cannot
# connect at all when served under a Hub subpath, which looks like a loading
# bar that never progresses and never prints a status line.
#
# 1. The web client builds its signalling WebSocket and TURN fetch from
#    ABSOLUTE paths (/{appName}/signalling/, /turn), which 404 behind
#    /stream/isaac-sim/. Rewritten to be relative to the current path, so the
#    same client works at the root and behind the proxy.
RUN set -eux; \
    f=/opt/gst-web/app.js; \
    grep -q 'app.appName + "/signalling/"' "$f"; \
    sed -i \
      -e 's#"/" + app.appName + "/signalling/"#window.location.pathname + "webrtc/signalling/"#g' \
      -e 's#fetch("/turn")#fetch("turn")#g' \
      -e 's#fetch("/publish/" + app.appName)#fetch("publish/" + app.appName)#g' \
      "$f"

# 2. selkies-gstreamer's signalling error handlers are `async def` but call the
#    BLOCKING time.sleep(2) on every "no peer yet" retry, stalling the whole
#    single-threaded asyncio loop. That is the "stream refuses to reconnect
#    after a few disconnects until the container is restarted" bug -- which
#    matters more here than for CAD, since these instances are meant to stay
#    up for days and be reconnected to repeatedly.
RUN set -eux; \
    f=$(ls /usr/local/lib/python3.*/dist-packages/selkies_gstreamer/__main__.py 2>/dev/null | head -1); \
    if [ -n "$f" ] && grep -q "time.sleep(2)" "$f"; then \
      sed -i "s/time\.sleep(2)/await asyncio.sleep(2)/g" "$f"; \
      echo "patched $f"; \
    else echo "selkies signalling already correct or not found"; fi

# Isaac's OWN interface is Omniverse Kit, which ignores QT_SCALE_FACTOR
# entirely -- so the desktop could be rescaled while Isaac's toolbars stayed
# tiny. Kit's equivalent is /app/window/dpiScaleOverride, which has NO
# preferences UI (it appears in no extension, only in omni.ui's settings), so
# it can only be passed at launch.
#
# This wrapper reads the same value the Hub's settings panel writes, so one
# slider scales both the desktop and Isaac. Unset = Kit's own default.
RUN printf '%s\n' \
    '#!/bin/sh' \
    '# Scale Isaac to match the desktop. SM_UI_SCALE is set by the Server' \
    '# Manager from the user preference; empty means leave Kit alone.' \
    'if [ -n "$SM_UI_SCALE" ]; then' \
    '  exec /isaac-sim/isaac-sim.sh --/app/window/dpiScaleOverride="$SM_UI_SCALE" "$@"' \
    'fi' \
    'exec /isaac-sim/isaac-sim.sh "$@"' \
    > /usr/local/bin/isaac-launch && chmod +x /usr/local/bin/isaac-launch

# Replace Selkies' caching service worker with a passthrough one.
#
# The stock SW caches the client shell, so the browser keeps serving an old
# copy of the page -- which silently defeats anything the Hub injects into it
# (the settings panel and F11 handler) and can pin a stale client whose
# absolute paths do not route through the proxy. The FreeCAD image hit this
# first and carries the same fix. Network-only, so the page is always current;
# it still registers a fetch handler, which is what Chrome requires for the
# app to be installable as a PWA.
COPY passthrough-sw.js /opt/gst-web/sw.js

# Isaac's own logo, so the launcher and window are the mark people recognise
# rather than a generic science glyph.
RUN cp /isaac-sim/exts/isaacsim.app.about/data/icon.png \
       /usr/share/pixmaps/isaac-sim.png 2>/dev/null || true

# Start Isaac WITH the desktop session rather than waiting for a click.
#
# Two reasons. Shader compilation on a cold cache takes minutes and happens on
# first launch either way -- doing it at container start means it overlaps the
# time you spend connecting, instead of beginning after you have already sat
# through the stream connecting. And Isaac draws its own splash and progress,
# so an autostarted session gives real feedback about what is loading, where
# an empty desktop gives none.
#
# xdg/autostart runs it as part of the session, so it inherits the session's
# display, GPU access and environment without a wrapper.
RUN printf '%s\n' \
    '#!/bin/sh' \
    '# Isaac Lab: a shell in the Lab workspace with Isaac Sim on PATH, since' \
    '# most Lab work is scripted rather than a single GUI app.' \
    'cd /workspace/isaaclab 2>/dev/null || cd /isaac-sim' \
    'exec konsole --noclose -e /bin/bash' \
    > /usr/local/bin/isaaclab-launch && chmod +x /usr/local/bin/isaaclab-launch

# One image backs two catalogue entries; SM_ISAAC_APP decides which opens.
RUN printf '%s\n' \
    '#!/bin/sh' \
    'case "$SM_ISAAC_APP" in' \
    '  lab) exec /usr/local/bin/isaaclab-launch ;;' \
    '  *)   exec /usr/local/bin/isaac-launch ;;' \
    'esac' \
    > /usr/local/bin/sm-isaac-autostart && chmod +x /usr/local/bin/sm-isaac-autostart

RUN printf '%s\n' \
    '[Desktop Entry]' \
    'Type=Application' \
    'Name=NVIDIA Isaac' \
    'Comment=Robotics simulation (Omniverse Kit)' \
    'Exec=/usr/local/bin/sm-isaac-autostart' \
    'Icon=/usr/share/pixmaps/isaac-sim.png' \
    'Terminal=false' \
    'X-GNOME-Autostart-enabled=true' \
    'Categories=Science;Engineering;' \
    | tee /usr/share/applications/isaac-sim.desktop \
          /etc/xdg/autostart/isaac-sim.desktop > /dev/null

USER 1000
