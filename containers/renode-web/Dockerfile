# Server-Manager Renode — a real open-source microcontroller simulator
# (MIT-licensed: https://github.com/renode/renode), exposed as a browser
# terminal onto its interactive monitor console via ttyd (also MIT-licensed:
# https://github.com/tsl0922/ttyd). This is the genuine self-hostable FOSS
# alternative to Wokwi — Wokwi's own simulator is closed-source SaaS, not
# self-hostable, so it isn't in this catalogue. Console-first, not a
# drag-and-drop breadboard GUI: load a .resc script, simulate a board
# (STM32/ESP32/etc.), read UART output, right from the app card.
FROM antmicro/renode:latest

# The base image's default user ("developer") can't write to /var/lib/apt or
# /usr/local/bin — switch to root for the install, same as any other package
# step; the ENTRYPOINT below runs as root too (fine for a single-tenant
# homelab console tool, and simplest for writing into WORKDIR as any user).
USER root

ARG TTYD_VERSION=1.7.7
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL -o /usr/local/bin/ttyd \
       "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
    && chmod +x /usr/local/bin/ttyd \
    && apt-get purge -y curl && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /root/projects
EXPOSE 8080

# -W = writable (the whole point — typing monitor commands); serves on 8080 to
# match the App Definition's internal_port. `renode --console` runs the
# interactive monitor headless (no X11), which is what a terminal wrapper needs.
ENTRYPOINT ["ttyd", "-p", "8080", "-W", "renode", "--console"]
