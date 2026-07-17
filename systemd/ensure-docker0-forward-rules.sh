#!/usr/bin/env bash
# Idempotently restore NAT/FORWARD rules for EVERY Docker bridge network on
# this host — not just docker0.
#
# Found live on 2026-07-16, TWICE, on two different bridges:
#   1. docker0 (the default bridge) had NONE of its NAT masquerade /
#      FORWARD-chain rules. Host-initiated traffic to a published port
#      (curl 127.0.0.1:8100) still worked — that's the OUTPUT chain,
#      unaffected — which is exactly why nothing looked broken until a
#      container tried to initiate its OWN outbound connection (a `docker
#      build`'s package-manager DNS lookup, in the case that surfaced this).
#   2. sm-llm-net (Open WebUI + Ollama's shared custom network,
#      br-9aa1d98a084c, 172.18.0.0/16) had its NAT rule but NONE of its
#      FORWARD-chain rules — same exact gap, different bridge. Surfaced as
#      Open WebUI hanging indefinitely (0% CPU, stuck memory, no progress
#      past its first log line) on a plain HTTPS call to huggingface.co
#      during its own startup — no error, no timeout, just a permanently
#      blocked TCP connect the kernel never let complete.
#
# The first incident's fix only covered docker0 by name. The second
# incident is exactly why that was too narrow: ANY bridge — present now or
# created later — can end up missing these rules, apparently without a
# single conclusively-identified trigger (docker.service is already ordered
# After=ufw.service; this doesn't look like that specific race). Rather
# than chase each bridge individually as it bites, this script discovers
# every bridge network on this daemon (and the USB app-hosting daemon, if
# its socket exists) and asserts the same three rules for all of them.
# Safe to run any number of times — every check is `iptables -C` before
# `-A`, so an already-present rule is never duplicated.
set -euo pipefail

ensure_rules_for() {
  local bridge="$1" subnet="$2"
  [ -z "$bridge" ] || [ -z "$subnet" ] && return 0

  iptables -t nat -C POSTROUTING -s "$subnet" ! -o "$bridge" -j MASQUERADE 2>/dev/null \
    || iptables -t nat -A POSTROUTING -s "$subnet" ! -o "$bridge" -j MASQUERADE

  # DOCKER-FORWARD/DOCKER-CT are dockerd-managed chains that only exist once
  # at least one docker daemon has started on this host — by the time this
  # runs (After=docker.service) that's guaranteed for the main daemon; if
  # somehow absent, skip rather than fail the whole unit.
  if iptables -L DOCKER-FORWARD -n >/dev/null 2>&1; then
    iptables -C DOCKER-FORWARD -i "$bridge" -j ACCEPT 2>/dev/null \
      || iptables -A DOCKER-FORWARD -i "$bridge" -j ACCEPT
  fi
  if iptables -L DOCKER-CT -n >/dev/null 2>&1; then
    iptables -C DOCKER-CT -o "$bridge" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
      || iptables -A DOCKER-CT -o "$bridge" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
  fi

  echo "ensured NAT/FORWARD rules for $bridge ($subnet)"
}

process_daemon() {
  local host_args=()
  [ -n "${1:-}" ] && host_args=(-H "$1")

  local names
  names="$(docker "${host_args[@]}" network ls --filter driver=bridge --format '{{.Name}}' 2>/dev/null)" || return 0
  local name bridge subnet net_id
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    # Docker actually DOES populate com.docker.network.bridge.name for both
    # daemons' default "bridge" network (docker0 / docker-usb0, whichever
    # --bridge the daemon itself was started with) — confirmed live. Only a
    # CUSTOM network (sm-llm-net, etc.) has this empty, in which case Docker
    # derives the real interface name as "br-" + the network ID's first 12
    # hex chars (also confirmed live: sm-llm-net's ID starts 9aa1d98a084c...,
    # its actual interface is br-9aa1d98a084c) — no guessing needed either way.
    bridge="$(docker "${host_args[@]}" network inspect "$name" \
      --format '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null)" || continue
    if [ -z "$bridge" ]; then
      net_id="$(docker "${host_args[@]}" network inspect "$name" --format '{{.Id}}' 2>/dev/null)"
      bridge="br-${net_id:0:12}"
    fi
    subnet="$(docker "${host_args[@]}" network inspect "$name" \
      --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null)"
    ip link show "$bridge" >/dev/null 2>&1 && ensure_rules_for "$bridge" "$subnet"
  done <<< "$names"
}

# Main daemon (default socket).
process_daemon ""

# USB app-hosting daemon(s), if any drive is currently assigned — socket
# path is per-drive-UUID, so discover whatever's actually running rather
# than hardcoding one.
for sock in /run/sandos-usb-docker-*.sock; do
  [ -S "$sock" ] || continue
  process_daemon "unix://$sock"
done
