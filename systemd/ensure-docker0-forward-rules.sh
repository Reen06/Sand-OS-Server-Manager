#!/usr/bin/env bash
# Idempotently restore docker0's NAT/FORWARD rules if they're missing.
#
# Found live on 2026-07-16: docker0 (the default bridge — sm-ollama,
# sm-open-webui, and any container not on a USB-hosted or custom app
# network) had NONE of its NAT masquerade / FORWARD-chain rules, while every
# OTHER bridge on the host (docker-usb0, and Docker Compose's custom
# networks) had theirs intact. Host-initiated traffic to a published port
# (curl 127.0.0.1:8100) still worked — that's the OUTPUT chain, unaffected —
# which is exactly why nothing looked broken until a container tried to
# initiate its OWN outbound connection (a `docker build`'s package-manager
# DNS lookup, in the case that surfaced this). With FORWARD's default policy
# DROP and no per-bridge ACCEPT for docker0, EVERY container-initiated
# outbound request was silently dropped.
#
# Root cause was never conclusively pinned down (docker.service is already
# ordered After=ufw.service via the sibling after-ufw.conf drop-in, so this
# wasn't the same race that bit the USB dockerd) — plausibly a side effect
# of the many docker network/daemon restarts during that day's diagnostic
# work. Since the exact trigger is unconfirmed, don't rely on "it won't
# happen again" — this script re-asserts the three rules unconditionally,
# safe to run any number of times (each check is `iptables -C` before
# `-A`, so a rule already present is never duplicated).
set -euo pipefail

iptables -t nat -C POSTROUTING -s 172.17.0.0/16 ! -o docker0 -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s 172.17.0.0/16 ! -o docker0 -j MASQUERADE

iptables -C DOCKER-FORWARD -i docker0 -j ACCEPT 2>/dev/null \
  || iptables -A DOCKER-FORWARD -i docker0 -j ACCEPT

iptables -C DOCKER-CT -o docker0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
  || iptables -A DOCKER-CT -o docker0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT

echo "docker0 forward/NAT rules present."
