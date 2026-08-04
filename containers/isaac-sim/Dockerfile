# NVIDIA Isaac Sim, streamed to the browser — a normal Sand-OS app.
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
ARG ISAAC_IMAGE=nvcr.io/nvidia/isaac-sim:5.1.0

FROM ${ISAAC_IMAGE} AS isaac

FROM ghcr.io/selkies-project/nvidia-egl-desktop:${SELKIES_TAG}
USER root

# /isaac-sim is self-contained (own python, exts, extscache) — copying it is
# enough, no package installs needed.
COPY --from=isaac /isaac-sim /isaac-sim

# Isaac writes caches, logs and its EULA acceptance under $HOME at runtime.
# The desktop session runs as the base image's unprivileged user, so hand it
# ownership rather than running Isaac as root.
ARG DESKTOP_USER=ubuntu
RUN set -eux; \
    id -u "${DESKTOP_USER}" >/dev/null 2>&1 || DESKTOP_USER=user; \
    chown -R "${DESKTOP_USER}":"${DESKTOP_USER}" /isaac-sim; \
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
    '[Desktop Entry]' \
    'Type=Application' \
    'Name=NVIDIA Isaac Sim' \
    'Comment=Robotics simulation (Omniverse Kit)' \
    'Exec=/isaac-sim/isaac-sim.sh' \
    'Icon=/usr/share/pixmaps/isaac-sim.png' \
    'Terminal=false' \
    'X-GNOME-Autostart-enabled=true' \
    'Categories=Science;Engineering;' \
    | tee /usr/share/applications/isaac-sim.desktop \
          /etc/xdg/autostart/isaac-sim.desktop > /dev/null

USER 1000
