#!/usr/bin/env bash
# Sand-OS Server Manager — Interactive installer
# Usage:  sudo bash install.sh          (system-wide install)
#         bash install.sh               (prompts for sudo when needed)
set -euo pipefail

# ── Colour / terminal helpers ─────────────────────────────────────────────────
# A real interactive terminal on BOTH ends, not just this process's own stdout —
# `[ -t 1 ]` alone can be true even when piped through something like
# `python subprocess.run(["wsl", ...])` (WSL's own console bridging), where a raw
# `clear` escape sequence can visually wipe or garble the display without this
# actually being a terminal a human is looking at directly. TERM being unset/
# "dumb" is the other reliable tell for that same situation.
if [ -t 1 ] && [ -n "${TERM:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
  IS_TTY=1
else
  IS_TTY=0
fi

if [ "$IS_TTY" = "1" ] && command -v tput &>/dev/null && tput setaf 1 &>/dev/null 2>&1; then
  BOLD=$(tput bold);  RST=$(tput sgr0)
  RED=$(tput setaf 1); GRN=$(tput setaf 2); YLW=$(tput setaf 3)
  BLU=$(tput setaf 4); CYN=$(tput setaf 6); WHT=$(tput setaf 7)
  DIM=$(tput dim 2>/dev/null || echo "")
else
  BOLD=''; RST=''; RED=''; GRN=''; YLW=''; BLU=''; CYN=''; WHT=''; DIM=''
fi

HR="${DIM}$(printf '─%.0s' $(seq 1 64))${RST}"

header() {
  # Only actually clear on a real terminal — see IS_TTY above. Every step's
  # content still prints either way; this just controls whether the PREVIOUS
  # step's text is wiped first, so nothing risks disappearing where a clear
  # might not render the way it does on a native terminal.
  [ "$IS_TTY" = "1" ] && { clear 2>/dev/null || true; }
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

# Every interactive prompt goes to STDERR, never stdout — read_val()/pick()
# are called as `x=$(read_val ...)`/`x=$(pick ...)` everywhere, and command
# substitution captures a function's ENTIRE stdout, not just its final
# return line. Printing the prompt to stdout meant it silently vanished
# into the captured variable instead of ever reaching the screen — the
# actual root cause of steps appearing to "show up with nothing" (this
# was never Windows/WSL-specific; the same bug exists on native Linux, it
# was just never faced squarely before now).
ask()     { printf "    %s " "$*" >&2; }

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
  # The whole menu is display-only — same reasoning as ask() above, redirect
  # it all to stderr so it can never be swallowed by `x=$(pick ...)`.
  {
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
  } >&2
  while true; do
    ask "${prompt} — type a number 1-${#vals[@]} and press Enter (or just Enter for the default):"
    read -r _sel
    [[ -z "$_sel" ]] && { echo "$default"; return; }
    if [[ "$_sel" =~ ^[0-9]+$ ]] && (( _sel >= 1 && _sel <= ${#vals[@]} )); then
      echo "${vals[$(( _sel - 1 ))]}"; return
    fi
    warn "Enter a number between 1 and ${#vals[@]}" >&2
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
# A bare IP/hostname with no scheme (an easy slip — "just the IP" instead of
# the full URL) doesn't fail here; it silently gets written into the env
# file as-is and only crashes later, deep in an unrelated request handler,
# with a confusing generic 500 (urllib refuses to build a request from a
# schemeless URL). Auto-prepend https:// — the scheme this project always
# assumes elsewhere anyway — rather than let a malformed value reach disk.
if [ -n "$SM_HUB_URL" ] && [[ "$SM_HUB_URL" != http://* && "$SM_HUB_URL" != https://* ]]; then
  warn "No http(s):// on that Hub URL — assuming https://${SM_HUB_URL}"
  SM_HUB_URL="https://${SM_HUB_URL}"
fi

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
SM_NAS_ROOT="/home/$(whoami)/sandos-nas"

# Ask the Hub which node already self-hosts the fleet's real NFS export
# (GET /api/fleet/nas-host — unauthenticated, exists precisely so a brand
# new node can learn this before it has any Hub session). Without this, the
# obvious-looking default was always "this machine" — which silently turns
# a plain app node into its own (usually nonfunctional, e.g. WSL2 can't
# reliably run an NFS server) NAS instead of pointing it at the real one.
_discovered_nas_host=""
if [ -n "$SM_HUB_URL" ] && command -v curl &>/dev/null; then
  _nas_info=$(curl -fsS --max-time 5 "${SM_HUB_URL%/}/api/fleet/nas-host" 2>/dev/null || true)
  if [ -n "$_nas_info" ]; then
    _discovered_nas_host=$(python3 -c "
import json, sys
try:
    print(json.loads(sys.argv[1]).get('host') or '')
except Exception:
    print('')
" "$_nas_info" 2>/dev/null || true)
  fi
fi
SM_NAS_HOST="${_discovered_nas_host:-$SM_LAN_IP}"

if confirm "Enable the NAS layer?"; then
  SM_NAS_ENABLED="true"

  _nas_root_default="/home/$(whoami)/sandos-nas"
  SM_NAS_ROOT=$(read_val "Local path to the NAS export root (on the NAS host)" "$_nas_root_default")

  blank
  if [ -n "$_discovered_nas_host" ]; then
    ok "Found this fleet's NAS already running at ${_discovered_nas_host} — defaulting to it."
  else
    warn "No existing fleet NAS found — defaulting to this machine (${SM_LAN_IP})."
    warn "Only accept this if THIS node should be the shared NAS host."
  fi
  SM_NAS_HOST=$(read_val "IP of the NFS server host" "$SM_NAS_HOST")

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

# The SM asks Docker for GPU access via the CDI device spec ("nvidia.com/gpu=all"),
# which needs /etc/cdi/nvidia.yaml to actually exist — on native Linux this is
# usually generated automatically by the nvidia-container-toolkit apt package's
# own install hook (confirmed present already on an existing node), but WSL2's
# NVIDIA driver shim has no equivalent hook, so it's silently missing there
# until generated by hand — confirmed live: docker run failed with "CDI device
# injection failed: unresolvable CDI devices nvidia.com/gpu=all" the first time
# a GPU app was actually launched on a fresh WSL2 node, well after install had
# otherwise "succeeded." Safe to always attempt: skipped instantly if the file
# is already there, and this is the exact same nvidia-ctk command on every
# platform, not something WSL-specific in itself.
if [[ "$SM_GPU" == "true" ]] && [ ! -f /etc/cdi/nvidia.yaml ]; then
  if command -v nvidia-ctk &>/dev/null; then
    info "Generating the NVIDIA CDI device spec (needed for GPU containers)…"
    if $SUDO nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml; then
      ok "CDI spec generated"
    else
      warn "CDI spec generation failed — GPU app containers may not start until"
      warn "this is fixed by hand: sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
    fi
  else
    warn "nvidia-ctk not found — install the NVIDIA Container Toolkit, then run:"
    warn "  sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
  fi
fi

blank
SM_SLOT_COUNT=$(read_val "Max concurrent app instances" "8")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6 — LOCAL STORAGE (Docker's own data-root — images/volumes/build cache)
# ═══════════════════════════════════════════════════════════════════════════════
header
step 6 "Local Storage"

cat << 'DESC'
  Docker stores every image, volume, and container this node ever builds or
  pulls somewhere on this machine's own disk — separate from the shared
  Fleet NAS (that's per-user files, not app images) and from the per-app
  "move to USB" feature (that relocates ONE app after the fact). This is
  about where Docker itself defaults to for everything, from the start.

DESC

if grep -qi microsoft /proc/version 2>/dev/null; then
  _docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "unknown")
  info "Docker is running via Docker Desktop's WSL2 integration (reports its own"
  info "internal path as ${_docker_root}, not a Windows drive letter)."
  blank
  echo "  Its real storage location is controlled entirely by Docker Desktop's"
  echo "  own setting, not by this installer:"
  blank
  echo "    Docker Desktop → Settings → Resources → Advanced → Disk image location"
  blank
else
  _docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "/var/lib/docker")
  _docker_free=$(df -h "$_docker_root" 2>/dev/null | awk 'NR==2{print $4}')
  info "Docker currently stores data at ${_docker_root}$( [ -n "$_docker_free" ] && echo " (${_docker_free} free there)")."
  blank
  if confirm "Change where Docker stores images/volumes on this machine?"; then
    warn "Anything Docker already has at ${_docker_root} becomes invisible to it"
    warn "the moment this changes — NOT deleted, just no longer where Docker looks."
    _new_root=$(read_val "New Docker data directory" "$_docker_root")
    if [ "$_new_root" != "$_docker_root" ]; then
      $SUDO mkdir -p "$_new_root"
      _daemon_json="/etc/docker/daemon.json"
      _tmp_json=$(mktemp)
      if [ -f "$_daemon_json" ]; then
        python3 -c "
import json, sys
with open('$_daemon_json') as f:
    cfg = json.load(f)
cfg['data-root'] = '$_new_root'
json.dump(cfg, sys.stdout, indent=2)
" > "$_tmp_json" 2>/dev/null || echo "{\"data-root\": \"$_new_root\"}" > "$_tmp_json"
      else
        echo "{\"data-root\": \"$_new_root\"}" > "$_tmp_json"
      fi
      $SUDO install -m 644 "$_tmp_json" "$_daemon_json"
      rm -f "$_tmp_json"
      info "Restarting Docker to apply…"
      if $SUDO systemctl restart docker && $SUDO docker info &>/dev/null; then
        ok "Docker now stores data at ${_new_root}"
      else
        warn "Docker restart or verification failed — ${_daemon_json} was written,"
        warn "but check 'sudo systemctl status docker' and 'docker info' by hand."
      fi
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 7 — WSL SSH ACCESS (WSL-only; native Linux installs are untouched —
# they keep the standard port 22 with zero code path exercised here)
# ═══════════════════════════════════════════════════════════════════════════════
SM_SSH_PORT="22"
if grep -qi microsoft /proc/version 2>/dev/null; then
  header
  step 7 "WSL SSH Access (for peer-to-peer app installs)"
  cat << 'DESC'
  Windows' own OpenSSH Server (not WSL's) answers port 22 on this machine's
  LAN IP — it only knows Windows accounts, never accounts inside WSL. The
  Hub's peer-install feature (copying an app's image + files directly
  between two nodes over SSH) needs to reach THIS WSL environment, so WSL
  gets its own sshd on a separate port instead — set up automatically below.

DESC
  SM_SSH_PORT="2222"

  if ! dpkg -s openssh-server &>/dev/null; then
    info "Installing openssh-server inside WSL…"
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq openssh-server
  fi

  _sshd_dropin="/etc/ssh/sshd_config.d/60-sandos-altport.conf"
  if [ "$($SUDO cat "$_sshd_dropin" 2>/dev/null)" != "Port ${SM_SSH_PORT}" ]; then
    echo "Port ${SM_SSH_PORT}" | $SUDO tee "$_sshd_dropin" > /dev/null
  fi
  $SUDO mkdir -p /run/sshd

  # ssh.socket hardcodes port 22 in its own systemd unit and ignores
  # sshd_config's Port drop-in entirely — a standalone sshd is required to
  # actually honor the alternate port.
  $SUDO systemctl disable --now ssh.socket &>/dev/null || true
  $SUDO systemctl enable --now ssh.service &>/dev/null || true

  if $SUDO ss -tlnp 2>/dev/null | grep -q ":${SM_SSH_PORT} "; then
    ok "WSL sshd listening on port ${SM_SSH_PORT}"
  else
    warn "Couldn't confirm WSL sshd is listening on ${SM_SSH_PORT} — check"
    warn "  sudo systemctl status ssh.service"
  fi

  # Forward the port from the Windows host into WSL and open it in the
  # Windows Firewall, via WSL→Windows interop. No elevation is requested
  # here — this runs with whatever privileges this WSL session's Windows
  # account already has, same as install.sh's own sudo prompts on the
  # Linux side. Each netsh call runs separately (a single batched/chained
  # call to Windows was found to silently drop later commands). Best-
  # effort: warns and prints the manual command rather than failing the
  # whole install, since Windows-side admin rights aren't guaranteed from
  # inside WSL.
  _wsl_ip=$(hostname -I | awk '{print $1}')
  if command -v netsh.exe &>/dev/null && [ -n "$_wsl_ip" ]; then
    netsh.exe interface portproxy delete v4tov4 listenport=${SM_SSH_PORT} listenaddress=0.0.0.0 &>/dev/null || true
    if netsh.exe interface portproxy add v4tov4 listenport=${SM_SSH_PORT} listenaddress=0.0.0.0 connectport=${SM_SSH_PORT} connectaddress="$_wsl_ip" &>/dev/null; then
      ok "Windows portproxy: 0.0.0.0:${SM_SSH_PORT} -> ${_wsl_ip}:${SM_SSH_PORT}"
    else
      warn "Couldn't add the Windows portproxy rule automatically. Run this in"
      warn "an elevated Windows PowerShell/cmd:"
      warn "  netsh interface portproxy add v4tov4 listenport=${SM_SSH_PORT} listenaddress=0.0.0.0 connectport=${SM_SSH_PORT} connectaddress=${_wsl_ip}"
    fi
    if netsh.exe advfirewall firewall show rule name="SandOS SSH (WSL)" &>/dev/null; then
      ok "Windows Firewall rule already present"
    elif netsh.exe advfirewall firewall add rule name="SandOS SSH (WSL)" dir=in action=allow protocol=TCP localport=${SM_SSH_PORT} &>/dev/null; then
      ok "Windows Firewall: allowed inbound TCP ${SM_SSH_PORT}"
    else
      warn "Couldn't add the Windows Firewall rule automatically. Run this in"
      warn "an elevated Windows PowerShell/cmd:"
      warn "  netsh advfirewall firewall add rule name=\"SandOS SSH (WSL)\" dir=in action=allow protocol=TCP localport=${SM_SSH_PORT}"
    fi
    blank
    warn "Note: WSL's internal IP (${_wsl_ip}) can change after a Windows"
    warn "reboot — if peer-installs to this node later fail, re-run this"
    warn "installer to refresh the portproxy rule."
  else
    warn "netsh.exe not reachable from WSL — skipping Windows-side port"
    warn "forwarding. Peer-installs to this node will fail until the"
    warn "portproxy + firewall rule above are added by hand."
  fi
  blank
fi

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
[ "$SM_SSH_PORT" != "22" ] && _row "SSH port (WSL)" "$SM_SSH_PORT"
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

# ── SSH (Hub-relayed peer-installs / auto-update) ──────────────────────────────
# Port THIS node's own sshd listens on. 22 on native Linux. On WSL, Windows'
# own OpenSSH Server answers 22 on the LAN IP and knows nothing about WSL
# accounts, so this installer stood up a separate sshd inside WSL — see
# Step 7 above.
SM_SSH_PORT=${SM_SSH_PORT}
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

# Needed for the Hub's "Restart Server Manager" button (POST /api/sm/restart)
# and the fleet-wide auto-update feature (both just shell out to `sudo -n
# systemctl restart sandos-server-manager` as this same user) — documented
# as a prerequisite in main.py's own restart endpoint for a while, but never
# actually automated here, so it silently never existed on any real install
# until now (confirmed directly: neither of two real nodes had it, despite
# both features appearing to work off a coincidentally-still-warm cached
# sudo credential rather than a real permanent rule).
info "Granting passwordless restart permission (needed for the Hub restart button + auto-update)…"
_sudoers_tmp=$(mktemp)
echo "${CURRENT_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart ${UNIT_NAME}" > "$_sudoers_tmp"
# Validate BEFORE it ever touches /etc/sudoers.d — a malformed file there can
# break sudo system-wide, so a bad rule must never be written live even
# briefly, not just cleaned up after the fact.
if $SUDO visudo -cf "$_sudoers_tmp"; then
  $SUDO install -m 440 "$_sudoers_tmp" /etc/sudoers.d/61-sandos-sm-restart
else
  warn "Generated sudoers rule failed validation — skipped, nothing written."
  warn "The Hub's restart button / auto-update won't work until this is fixed by hand."
fi
rm -f "$_sudoers_tmp"

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

# `server-manager` command: a terminal Busy/Available toggle for headless
# boxes with no display (the curses twin of the GUI toggles) — stdlib only,
# runs fine under the system python3, no venv needed (it only talks to the
# already-running service's own local HTTP API).
$SUDO tee /usr/local/bin/server-manager > /dev/null << EOF
#!/usr/bin/env bash
exec python3 "${REPO_ROOT}/cli/server_manager_tui.py" "\$@"
EOF
$SUDO chmod +x /usr/local/bin/server-manager

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
  echo "  Nothing to add here — the Hub's own backend already dynamically"
  echo "  proxies app traffic to whichever node currently hosts each app"
  echo "  (sm_proxy.py, resolved per-request from its live Fleet registry),"
  echo "  and the Hub's Caddyfile already forwards every path to that"
  echo "  backend with one generic block. Just register this node on the"
  echo "  Hub's Fleet page (\"Add device\") and it's reachable immediately —"
  echo "  no Caddy edit or reload needed."
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
