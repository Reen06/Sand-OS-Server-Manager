#!/bin/bash
# Give selkies-gstreamer a SECOND, independently-reachable relay candidate for
# remote/VPN clients, alongside its primary LAN one — by running a second,
# local-only turnserver process and feeding Selkies' own multi-TURN-server
# config file (SELKIES_RTC_CONFIG_JSON, default /tmp/rtc.json).
#
# Why a SECOND TURN SERVER PROCESS, not just a second entry pointed at the
# extra host directly: the relay candidate embedded in the WebRTC offer is
# generated SERVER-SIDE, by selkies-gstreamer performing its OWN TURN
# allocation against whatever host:port it's configured with — it has to
# actually be able to REACH that address to allocate anything at all. Vortex-
# Eclipse (or any node not itself a WireGuard peer of the Hub's mesh) has no
# route to SELKIES_TURN_EXTRA_HOST (the Hub's WG tunnel address, e.g.
# 10.79.114.1) — an allocation attempt against it just fails, silently
# dropped, no second candidate ever appears. Confirmed live 2026-08-17: the
# JSON config had both hosts, Selkies logged reading it, and the SDP offer
# STILL only ever contained the LAN candidate.
#
# The fix uses TURN's own NAT-traversal feature: a turnserver's
# --external-ip is what it REPORTS as the relayed address, independent of
# what address the client actually connected to it on. So a SECOND
# turnserver, bound locally (always reachable via 127.0.0.1 — no routing
# needed) but started with --external-ip=SELKIES_TURN_EXTRA_HOST, lets
# selkies-gstreamer allocate from it over loopback while the candidate it
# gets back genuinely claims the WireGuard-reachable address. The real
# relayed DATA then flows on whatever port got allocated from THIS
# instance's own relay range (TURN_EXTRA_RELAY_MIN..MAX, docker_backend.py's
# job to publish + the Hub's job to DNAT, both alongside the primary range).
#
# selkies-gstreamer's own multi-server support (gstwebrtc_app.py looping
# add-turn-server over every "turn:" entry in this JSON) already exists —
# nothing here patches Selkies itself, only feeds it a second, genuinely
# reachable server.
#
# Only runs when SELKIES_TURN_EXTRA_HOST is set — an app/node with no extra
# host configured (docker_backend.py's config.TURN_EXTRA_HOST unset) starts
# nothing extra and gets byte-identical behavior to before this existed.
set -eu

[ -n "${SELKIES_TURN_EXTRA_HOST:-}" ] || exit 0

EXTRA_PORT="${SELKIES_TURN_EXTRA_PORT:?SELKIES_TURN_EXTRA_PORT must be set alongside SELKIES_TURN_EXTRA_HOST}"
EXTRA_RELAY_MIN="${TURN_EXTRA_RELAY_MIN:?TURN_EXTRA_RELAY_MIN must be set alongside SELKIES_TURN_EXTRA_HOST}"
EXTRA_RELAY_MAX="${TURN_EXTRA_RELAY_MAX:?TURN_EXTRA_RELAY_MAX must be set alongside SELKIES_TURN_EXTRA_HOST}"

# Same shape as /etc/start-turnserver.sh, but a second, independent instance:
# own control port (never published to the host — reached only over loopback
# from selkies-gstreamer itself), own relay range, own --external-ip. Same
# long-term credential as the primary so rtc.json can reuse one password.
turnserver --verbose \
  --listening-ip=0.0.0.0 --listening-ip=:: \
  --listening-port="${EXTRA_PORT}" \
  --realm="${TURN_REALM:-example.com}" \
  --external-ip="${SELKIES_TURN_EXTRA_HOST}" \
  --min-port="${EXTRA_RELAY_MIN}" --max-port="${EXTRA_RELAY_MAX}" \
  --channel-lifetime="${TURN_CHANNEL_LIFETIME:--1}" \
  --lt-cred-mech \
  --user="${SELKIES_TURN_USERNAME}:${SELKIES_TURN_PASSWORD}" \
  --no-cli \
  --pidfile="${XDG_RUNTIME_DIR:-/tmp}/turnserver-extra.pid" \
  --log-file=stdout \
  --allow-loopback-peers &

python3 - <<PYEOF
import json
import os

port = os.environ["SELKIES_TURN_PORT"]
extra_port = "${EXTRA_PORT}"
proto = os.environ.get("SELKIES_TURN_PROTOCOL", "tcp")
user = os.environ["SELKIES_TURN_USERNAME"]
password = os.environ["SELKIES_TURN_PASSWORD"]
stun_host = os.environ.get("SELKIES_STUN_HOST", "stun.l.google.com")
stun_port = os.environ.get("SELKIES_STUN_PORT", "19302")

cfg = {
    "lifetimeDuration": "86400s",
    "blockStatus": "NOT_BLOCKED",
    "iceTransportPolicy": "all",
    "iceServers": [
        {"urls": [f"stun:{stun_host}:{stun_port}"]},
        {
            # Primary: real LAN address, matches SM's normal published port.
            "urls": [f"turn:{os.environ['SELKIES_TURN_HOST']}:{port}?transport={proto}"],
            "username": user,
            "credential": password,
        },
        {
            # Extra: connect over LOOPBACK (always reachable from inside this
            # container, no routing needed) — the second turnserver above
            # reports SELKIES_TURN_EXTRA_HOST as ITS relayed address
            # regardless of what address was used to reach it.
            "urls": [f"turn:127.0.0.1:{extra_port}?transport={proto}"],
            "username": user,
            "credential": password,
        },
    ],
}

path = os.environ.get("SELKIES_RTC_CONFIG_JSON", "/tmp/rtc.json")
with open(path, "w") as f:
    json.dump(cfg, f)
print(f"[write-rtc-config] wrote {path}: LAN candidate via {os.environ['SELKIES_TURN_HOST']}:{port}, "
      f"extra candidate via local:{extra_port} advertising {os.environ['SELKIES_TURN_EXTRA_HOST']}")
PYEOF
