# OpenFOAM GUI — homegrown FastAPI case-manager UI for OpenFOAM, served as a
# 'web' app (shared). Runs the app LIVE from a bind-mounted source tree
# (/home/control/OpenFOAM_GUI → /gui) the same way webcad/helix do — no COPY
# of app code into the image, so edits on the host take effect on next
# relaunch with no rebuild. What IS baked into the image is the actual
# multi-GB OpenFOAM toolchain (opencfd/openfoam-run:2506) + the Python venv,
# which is why this is built directly against the USB app-hosting drive's
# own dockerd (-H <usb-socket>) rather than locally.
FROM opencfd/openfoam-run:2506
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

ENV VENV=/opt/gui-venv
RUN python3 -m venv $VENV
ENV PATH=$VENV/bin:$PATH

WORKDIR /gui
EXPOSE 6060

COPY entrypoint.sh /usr/local/bin/foamgui-entrypoint.sh
RUN chmod +x /usr/local/bin/foamgui-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/foamgui-entrypoint.sh"]
