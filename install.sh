#!/usr/bin/env bash
# Sand-OS Server Manager — Interactive installer
# Usage:  sudo bash install.sh          (system-wide install)
#         bash install.sh               (prompts for sudo when needed)
set -euo pipefail

# ── Colour / terminal helpers ─────────────────────────────────────────────────
if [ -t 1 ] && command -v tput &>/dev/null && tput setaf 1 &>/dev/null 2>&1; then
  BOLD=$(tput bold);  RST=$(tput sgr0)
  RED=$(tput setaf 1); GRN=$(tput setaf 2); YLW=$(tput setaf 3)
  BLU=$(tput setaf 4); CYN=$(tput setaf 6); WHT=$(tput setaf 7)
  DIM=$(tput dim 2>/dev/null || echo "")
else
  BOLD=''; RST=''; RED=''; GRN=''; YLW=''; BLU=''; CYN=''; WHT=''; DIM=''
fi

HR="${DIM}$(printf '─%.0s' $(seq 1 64))${RST}"

header() {
  clear 2>/dev/null || true
  echo
  printf "  %s%sSand-OS Server Manager%s  ·  Installer\n" "$BOLD" "$CYN" "$RST"
  printf "  %s%s\n" "$DIM" "$RST"
  echo "  $HR"
  echo
}

step()    { echo "  ${BOLD}Step $1${RST}  ${WHT}$2${RST}"; echo "  ${DIM}$(printf '─%.0s' $(seq 1 42))${RST}"; echo; }
info()    { echo "  ${CYN}→${RST}  $*"; }
ok()      { echo "  ${GRN}✓${RST}  $*"; }
warn()    { echo "  ${YLW}!${RST}  $*"; }
err()     { echo "  ${RED}✗${RST}  $*" >&2; }
die()     { err "$*"; exit 1; }
blank()   { echo; }

ask()     { printf "    %s " "$*"; }

confirm() {
  ask "${BOLD}$1${RST} [Y/n]"
  read -r _ans
  [[ -z "$_ans" || "$_ans" =~ ^[Yy] ]]
}

read_val() {           # read_val "prompt" "default"  →  echoes value
  local prompt="$1" default="$2"
  ask "${prompt} ${DIM}[${default}]${RST}:"
  read -r _val
  printf '%s' "${_val:-$default}"
}

pick() {               # pick "prompt" default  val1 "label1"  val2 "label2"  ...
  local prompt="$1" default="$2"; shift 2
  local -a vals labels
  while (( $# >= 2 )); do vals+=("$1"); labels+=("$2"); shift 2; done
  blank
  for i in "${!vals[@]}"; do
    local n=$(( i + 1 ))
    if [[ "${vals[$i]}" == "$default" ]]; then
      printf "    ${BOLD}${GRN}%s)${RST}  %s ${DIM}(default)${RST}\n" "$n" "${labels[$i]}"
    else
      printf "    ${BOLD}%s)${RST}  %s\n" "$n" "${labels[$i]}"
    fi
  done
  blank
  while true; do
    ask "${prompt} [1-${#vals[@]}]:"
    read -r _sel
    [[ -z "$_sel" ]] && { echo "${vals[0]}"; return; }
    if [[ "$_sel" =~ ^[0-9]+$ ]] && (( _sel >= 1 && _sel <= ${#vals[@]} )); then
      echo "${vals[$(( _sel - 1 ))]}"; return
    fi
    warn "Enter a number between 1 and ${#vals[@]}"
  done
}

_row() { printf "  ${DIM}%-26s${RST}  ${BOLD}%s${RST}\n" "$1" "$2"; }

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$REPO_ROOT/server"
VENV="$SERVER_DIR/.venv"
ENV_FILE="/etc/sandos-server-manager.env"
UNIT_NAME="sandos-server-manager"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}.service"

# ── Sudo wrapper ──────────────────────────────────────────────────────────────
if [ "$EUID" -eq 0 ]; then
  SUDO=""
else
  command -v sudo &>/dev/null || die "Not root and sudo not found. Run as root."
  SUDO="sudo"
  # Pre-warm sudo ticket so it doesn't interrupt prompts mid-flow
  $SUDO true
fi

# ── Auto-detect helpers ───────────────────────────────────────────────────────
_lan_ip() {
  python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('10.255.255.255', 1)); print(s.getsockname()[0])
except Exception:
    print('127.0.0.1')
finally:
    s.close()
" 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1"
}

_has_gpu() {
  command -v nvidia-smi &>/dev/null && nvidia-smi -L &>/dev/null 2>&1 && echo true || echo false
}

# ═══════════════════════════════════════════════════════════════════════════════
# WELCOME
# ═══════════════════════════════════════════════════════════════════════════════
header

cat << 'INTRO'
  The Server Manager is the compute engine behind Sand-OS apps — it spawns
  and streams containerised apps (FreeCAD, Nextcloud, Files, WebCAD…) and
  connects them to your Sand-OS Hub for auth, placement, and discovery.

  This installer will:
    • ask a few questions about how this machine connects to your Hub
    • write  /etc/sandos-server-manager.env
    • install + start the  sandos-server-manager  systemd service

INTRO

command -v docker &>/dev/null || die "Docker is required but not installed. Install Docker first."
command -v python3 &>/dev/null || die "python3 is required but not found."
ok "Docker    $(docker --version 2>/dev/null | head -1)"
ok "Python    $(python3 --version 2>/dev/null)"
blank
confirm "Continue?" || { warn "Aborted."; exit 0; }

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — DEPLOYMENT MODE
# ═══════════════════════════════════════════════════════════════════════════════
header
step 1 "Deployment Mode"

cat << 'DESC'
  How is this machine connected to your Sand-OS Hub?

  Same LAN      — machine is on the same local network as the Hub.
                  The Hub reaches it directly; no VPN needed.

  Remote / VPN  — machine is on a different network and tunnels back
                  to the Hub over WireGuard. Its WireGuard IP is used
                  for both the API and TURN relay candidates.

  On the Hub    — this IS the Hub device. Both services share one
                  machine. The Server Manager binds on the same LAN IP;
                  Caddy routes /apps/* to it on localhost.

DESC

MODE=$(pick "Mode" "lan" \
  "lan"        "Same LAN          (direct LAN, same subnet as Hub)" \
  "vpn"        "Remote / VPN      (different network, WireGuard tunnel)" \
  "colocated"  "On the Hub        (both services on one device)")

AUTO_IP=$(_lan_ip)

# ── Remote enrollment: paste a one-time link from the Hub's Fleet page and ──
# this box joins as a scoped WireGuard peer automatically — no manual wg-quick,
# no hand-typed IP. Only offered for "Remote / VPN" since a same-LAN or
# co-located box reaches the Hub directly and doesn't need a tunnel at all.
ENROLL_HUB_BASE=""
if [[ "$MODE" == "vpn" ]]; then
  blank
  cat << 'DESC'
  If your Hub gave you a one-time enrollment link (Fleet page → "Enroll
  Remote Server…"), paste it below to join automatically — this brings up
  the WireGuard tunnel and pre-fills the rest of this installer for you.
  Leave blank if this machine already has its own tunnel configured.

DESC
  ENROLL_LINK=$(read_val "Enrollment link (blank to skip)" "")
  if [ -n "$ENROLL_LINK" ]; then
    blank
    info "Setting up the WireGuard enrollment tunnel…"
    command -v curl &>/dev/null || die "curl is required to fetch the enrollment link. Install curl and re-run."

    if $SUDO wg show sandos-hub &>/dev/null; then
      warn "A 'sandos-hub' tunnel is already up — reusing it. (Run 'sudo sandos-wg-enroll down' first to join with a different link.)"
    else
      if ! command -v wg-quick &>/dev/null; then
        info "Installing wireguard-tools…"
        $SUDO apt-get update -qq && $SUDO apt-get install -y -qq wireguard-tools
      fi
      $SUDO bash "$REPO_ROOT/containers/nfs-server/setup-wg-enroll.sh" >/dev/null

      STAGED_CONF="/etc/sandos/wg-enroll-staging.conf"
      # -k: the Hub's dashboard cert is commonly Caddy's self-signed internal
      # CA (see SM_HUB_VERIFY_TLS below, and the Hub's own hub-mesh client,
      # which trusts the enrollment token itself as the real credential, not
      # the TLS chain — same posture, applied consistently here).
      if ! curl -fsSk "$ENROLL_LINK" -o "$STAGED_CONF"; then
        die "Couldn't fetch the enrollment link — it may be expired or already used. Mint a new one from Fleet and re-run this installer."
      fi
      $SUDO sandos-wg-enroll up "$STAGED_CONF" >/dev/null
    fi

    ENROLL_WG_IP=$($SUDO sh -c "grep -m1 '^Address' /etc/wireguard/sandos-hub.conf" 2>/dev/null \
      | sed -E 's/.*=\s*//; s#/.*##' | tr -d '[:space:]')
    [ -n "$ENROLL_WG_IP" ] || die "Tunnel came up but its address couldn't be read — check: sudo wg show sandos-hub"
    ok "Tunnel up — this machine's WireGuard IP is ${ENROLL_WG_IP}"
    AUTO_IP="$ENROLL_WG_IP"
    ENROLL_HUB_BASE="${ENROLL_LINK%%/api/pairing/enroll/*}"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — NETWORK IDENTITY
# ═══════════════════════════════════════════════════════════════════════════════
header
step 2 "Network Identity"

case "$MODE" in
  lan)
    echo "  Enter the LAN IP the Hub will use to probe and reach this node."
    blank
    SM_LAN_IP=$(read_val "LAN IP of this machine" "$AUTO_IP")
    SM_TURN_EXTRA_HOST=""
    blank
    info "Apps will be reachable at  http://${SM_LAN_IP}:8170"
    ;;
  vpn)
    echo "  Enter the WireGuard IP assigned to this machine."
    echo "  This IP is used for both the API endpoint and TURN relay"
    echo "  so the Hub and browsers can reach it over the VPN."
    blank
    SM_LAN_IP=$(read_val "WireGuard IP of this machine" "$AUTO_IP")
    SM_TURN_EXTRA_HOST="$SM_LAN_IP"
    blank
    info "API + TURN will use WireGuard IP  ${SM_LAN_IP}"
    ;;
  colocated)
    echo "  Enter the Hub's LAN IP. Both services share this machine;"
    echo "  the Server Manager binds on the same interface."
    blank
    SM_LAN_IP=$(read_val "This machine's LAN IP" "$AUTO_IP")
    SM_TURN_EXTRA_HOST=""
    blank
    info "Co-located at  ${SM_LAN_IP}"
    ;;
esac

blank
SM_PORT=$(read_val "Server Manager port" "8170")
SM_NODE_NAME=$(read_val "Friendly node name (shown in Hub fleet)" "$(hostname)")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — HUB CONNECTION
# ═══════════════════════════════════════════════════════════════════════════════
header
step 3 "Hub Connection"

cat << 'DESC'
  The Server Manager can validate user sessions against your Sand-OS Hub
  (Hub SSO). This links the Hub's user accounts to the Server Manager so
  each person gets their own per-user app instances and files.

  Leave the Hub URL blank to run in standalone mode — anyone on the
  network can access all apps without logging in.

DESC

SM_HUB_URL=$(read_val "Hub URL  (e.g. https://10.0.0.177 — blank for standalone)" "$ENROLL_HUB_BASE")

SM_HUB_VERIFY_TLS="false"
SM_EXTERNAL_BASE="/apps"

if [ -n "$SM_HUB_URL" ]; then
  ok "Hub SSO enabled → ${SM_HUB_URL}"
  blank
  echo "  Caddy on the Hub routes  /apps/*  to this Server Manager, then"
  echo "  apps are reached at  {hub}/apps/stream/{app-id}/..."
  SM_EXTERNAL_BASE=$(read_val "Hub mount path" "/apps")
  blank
  if confirm "Verify the Hub's TLS certificate? (no = accept self-signed Caddy internal CA)"; then
    SM_HUB_VERIFY_TLS="true"
  fi
else
  warn "Standalone mode — no Hub account required."
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — SHARED STORAGE (NAS)
# ═══════════════════════════════════════════════════════════════════════════════
header
step 4 "Shared Storage (NAS)"

cat << 'DESC'
  The NAS layer mounts per-user and shared files into every container via
  NFSv4 — so your FreeCAD projects, Nextcloud data, and shared libraries
  are identical across every node in the fleet with no duplication.

  Required for: Nextcloud, Filebrowser, cross-node FreeCAD project files.
  Skip if: you only need ephemeral apps (Ray Optics, Stirling PDF, etc.)
  and don't need files to follow users between nodes.

DESC

SM_NAS_ENABLED="false"
SM_NAS_ROOT="/home/control/sandos-nas"
SM_NAS_HOST="$SM_LAN_IP"

if confirm "Enable the NAS layer?"; then
  SM_NAS_ENABLED="true"

  case "$MODE" in
    colocated) _nas_root_default="/home/$(whoami)/sandos-nas" ;;
    *)         _nas_root_default="/home/control/sandos-nas" ;;
  esac

  SM_NAS_ROOT=$(read_val "Local path to the NAS export root (on the NAS host)" "$_nas_root_default")
  SM_NAS_HOST=$(read_val "IP of the NFS server host" "$SM_LAN_IP")

  blank
  info "NFS: ${SM_NAS_HOST}:/ — containers mount sub-paths per user/app"
  info "Make sure ${SM_NAS_ROOT} is exported via NFSv4 (fsid=0)."
else
  warn "NAS disabled — apps use node-local Docker volumes only."
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — COMPUTE CAPACITY
# ═══════════════════════════════════════════════════════════════════════════════
header
step 5 "Compute Capacity"

cat << 'DESC'
  FreeCAD and other streamed apps need an NVIDIA GPU and the NVIDIA
  Container Toolkit (nvidia-container-toolkit + CDI configured). Without
  a GPU those apps are hidden; all web apps still work on any hardware.

DESC

AUTO_GPU=$(_has_gpu)
if [[ "$AUTO_GPU" == "true" ]]; then
  ok "NVIDIA GPU detected via nvidia-smi"
  _gpu_default="true"
else
  warn "No NVIDIA GPU detected (nvidia-smi not found or failed)"
  _gpu_default="false"
fi

SM_GPU=$(pick "GPU support" "$_gpu_default" \
  "true"  "Enable  — advertise GPU; streamed apps (FreeCAD) available" \
  "false" "Disable — web apps only (Nextcloud, Files, WebCAD, Renode…)")

blank
SM_SLOT_COUNT=$(read_val "Max concurrent app instances" "8")

# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY + CONFIRM
# ═══════════════════════════════════════════════════════════════════════════════
header
echo "  ${BOLD}Configuration summary${RST}"
echo "  $HR"
blank

case "$MODE" in
  lan)        _row "Deployment mode"       "Same LAN" ;;
  vpn)        _row "Deployment mode"       "Remote / VPN (WireGuard)" ;;
  colocated)  _row "Deployment mode"       "Co-located on Hub" ;;
esac

_row "LAN / WG IP"             "$SM_LAN_IP"
_row "Port"                    "$SM_PORT"
_row "Node name"               "$SM_NODE_NAME"
[ -n "$SM_TURN_EXTRA_HOST" ] && _row "TURN extra host" "$SM_TURN_EXTRA_HOST"
_row "Hub URL"                 "${SM_HUB_URL:-(standalone, no SSO)}"
[ -n "$SM_HUB_URL" ] && _row "Hub TLS verify"   "$SM_HUB_VERIFY_TLS"
[ -n "$SM_HUB_URL" ] && _row "Hub mount path"   "$SM_EXTERNAL_BASE"
_row "NAS enabled"             "$SM_NAS_ENABLED"
[[ "$SM_NAS_ENABLED" == "true" ]] && _row "NAS root" "$SM_NAS_ROOT"
[[ "$SM_NAS_ENABLED" == "true" ]] && _row "NAS host" "$SM_NAS_HOST"
_row "GPU support"             "$SM_GPU"
_row "Instance slots"          "$SM_SLOT_COUNT"
blank
_row "Env file"                "$ENV_FILE"
_row "Systemd unit"            "$UNIT_NAME"
blank
echo "  $HR"
blank

confirm "Apply this configuration and install?" || { blank; warn "Aborted — no changes made."; exit 0; }

# ═══════════════════════════════════════════════════════════════════════════════
# INSTALL
# ═══════════════════════════════════════════════════════════════════════════════
header
echo "  ${BOLD}Installing…${RST}"
blank

# ── 1. Env file ────────────────────────────────────────────────────────────────
info "Writing ${ENV_FILE}…"
cat << EOF | $SUDO tee "$ENV_FILE" > /dev/null
# Sand-OS Server Manager — environment config
# Generated by install.sh on $(date -u '+%Y-%m-%dT%H:%M:%SZ')
# Re-run  sudo bash install.sh  to reconfigure.

# ── Network identity ──────────────────────────────────────────────────────────
# The IP this node advertises: browsers and TURN relay connect here.
SM_LAN_IP=${SM_LAN_IP}
SM_PORT=${SM_PORT}
SM_NODE_NAME=${SM_NODE_NAME}

# WebRTC TURN extra host — VPN/WireGuard IP so off-LAN clients get a reachable
# TURN candidate. Empty for same-LAN installs (LAN IP is enough).
SM_TURN_EXTRA_HOST=${SM_TURN_EXTRA_HOST}

# ── Hub SSO ───────────────────────────────────────────────────────────────────
# URL of the SandOS Hub. When set, the SM validates every session here.
# Leave empty for standalone / dev mode (all requests treated as admin).
SM_HUB_URL=${SM_HUB_URL}
SM_HUB_VERIFY_TLS=${SM_HUB_VERIFY_TLS}

# Path the Hub's Caddy mounts the SM under (used to build asset URLs).
SM_EXTERNAL_BASE=${SM_EXTERNAL_BASE}

# ── Shared storage (NAS) ──────────────────────────────────────────────────────
SM_NAS_ENABLED=${SM_NAS_ENABLED}
SM_NAS_HOST=${SM_NAS_HOST}
SM_NAS_ROOT=${SM_NAS_ROOT}

# ── Compute capacity ──────────────────────────────────────────────────────────
# Override GPU auto-detection (true/false).
SM_GPU=${SM_GPU}
# Maximum concurrent app instances across all users.
SM_SLOT_COUNT=${SM_SLOT_COUNT}
EOF
ok "Wrote ${ENV_FILE}"

# ── 2. Python venv ─────────────────────────────────────────────────────────────
if [ ! -x "${VENV}/bin/uvicorn" ]; then
  info "Creating Python venv and installing dependencies…"
  ( cd "$SERVER_DIR" \
    && python3 -m venv .venv \
    && .venv/bin/pip install -q --upgrade pip \
    && .venv/bin/pip install -q -r requirements.txt )
  ok "Venv ready at ${VENV}"
else
  ok "Venv already exists — skipping pip install"
fi

# ── 3. Systemd unit ────────────────────────────────────────────────────────────
info "Installing systemd unit → ${UNIT_DEST}…"
CURRENT_USER="${SUDO_USER:-$(whoami)}"
# Quote paths in case they contain spaces (repo path may include spaces).
cat << EOF | $SUDO tee "$UNIT_DEST" > /dev/null
[Unit]
Description=Sand-OS Server Manager (streamed-app orchestrator)
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=${CURRENT_USER}
EnvironmentFile=-${ENV_FILE}
WorkingDirectory=${SERVER_DIR}
ExecStart="${VENV}/bin/uvicorn" app.main:app --host 0.0.0.0 --port ${SM_PORT}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$UNIT_NAME"

# Stop any dev instance that might be holding the port
pkill -f "uvicorn app.main" 2>/dev/null || true
sleep 1

info "Starting service…"
$SUDO systemctl restart "$UNIT_NAME"
sleep 3

if $SUDO systemctl is-active --quiet "$UNIT_NAME"; then
  ok "sandos-server-manager is running"
else
  warn "Service may not have started — check:"
  warn "  journalctl -u ${UNIT_NAME} -n 30 --no-pager"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════════════════════
header
echo "  ${BOLD}${GRN}Installation complete!${RST}"
blank
_row "Apps screen"    "http://${SM_LAN_IP}:${SM_PORT}"
_row "Service logs"   "journalctl -u ${UNIT_NAME} -f"
_row "Reconfigure"    "sudo bash ${REPO_ROOT}/install.sh"
blank

if [ -n "${ENROLL_WG_IP:-}" ]; then
  echo "  ${BOLD}Finish enrollment on the Hub${RST}"
  echo "  $HR"
  echo "  This machine is reachable over its WireGuard tunnel at  ${ENROLL_WG_IP}"
  echo "  On the Hub's Fleet page, click \"Add device\" and enter that IP —"
  echo "  Fleet registration is a deliberate manual step, so a new remote box"
  echo "  never joins the fleet silently."
  blank
fi

if [ -n "$SM_HUB_URL" ]; then
  echo "  ${BOLD}Hub Caddy integration${RST}"
  echo "  $HR"
  echo "  Add these lines inside your Hub's  https://…  site block"
  echo "  (before the catch-all  handle { reverse_proxy … } ):"
  blank
  if [[ "$MODE" == "colocated" ]]; then
    _upstream="127.0.0.1:${SM_PORT}"
  else
    _upstream="${SM_LAN_IP}:${SM_PORT}"
  fi
  printf "  %s  redir /apps /apps/\n" "$DIM"
  printf "  handle_path /apps/* {\n"
  printf "      reverse_proxy %s\n" "$_upstream"
  printf "  }%s\n" "$RST"
  blank
  echo "  Then reload Caddy:  sudo systemctl reload caddy"
  blank
fi

if [[ "$SM_NAS_ENABLED" == "true" && "$SM_NAS_HOST" == "$SM_LAN_IP" ]]; then
  echo "  ${BOLD}NAS setup${RST} (this machine is the NAS host)"
  echo "  $HR"
  echo "  Install NFS server and export the NAS root:"
  blank
  printf "  %ssudo apt install nfs-kernel-server\n" "$DIM"
  printf "  sudo mkdir -p %s\n" "$SM_NAS_ROOT"
  printf "  echo '%s  10.0.0.0/8(rw,fsid=0,no_subtree_check,all_squash,anonuid=1000,anongid=1000)' | sudo tee -a /etc/exports\n" "$SM_NAS_ROOT"
  printf "  sudo exportfs -ra%s\n" "$RST"
  blank
fi
