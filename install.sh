#!/usr/bin/env bash
# Sand-OS Server Manager — Interactive installer
# Usage:  sudo bash install.sh                (system-wide install)
#         bash install.sh                     (prompts for sudo when needed)
#         sudo bash install.sh --advanced     (ask about every value, not just
#                                              the ones only you can decide)
#         sudo bash install.sh --unattended   (no prompts; read config from env)
#
# NORMAL vs --advanced
#   A normal run only stops for decisions the installer cannot make for you --
#   mode, NAS on or off, TLS verification, moving Docker's data root -- and
#   reports every value it worked out for itself (IPs, port, node name, Hub URL,
#   mount path, NAS host, slot count, GPU). --advanced turns those back into
#   prompts, and is the thing to reach for when something needs overriding.
#   Every value remains settable by SM_* env var in either mode.
#
# UNATTENDED MODE
#   Every question below takes its answer from the matching SM_* environment
#   variable, falling back to the same default the prompt would have offered.
#   Nothing is asked and nothing blocks, so this is what CI, a re-provision, or
#   a scripted fleet rollout should use.
#
#     SM_MODE           lan | vpn | colocated        (default: lan)
#     SM_LAN_IP         this node's LAN/WireGuard IP (default: auto-detected)
#     SM_PORT           listen port                  (default: 8170)
#     SM_NODE_NAME      name shown in Hub fleet      (default: hostname)
#     SM_HUB_URL        Hub base URL, blank=standalone
#     SM_HUB_VERIFY_TLS true | false                 (default: false)
#     SM_EXTERNAL_BASE  Hub mount path               (default: /apps)
#     SM_NAS_ENABLED    true | false                 (default: false)
#     SM_NAS_HOST       NFS server IP                (default: discovered/self)
#     SM_NAS_ROOT       export root path
#     SM_GPU            true | false                 (default: auto-detect)
#     SM_SLOT_COUNT     max concurrent apps          (default: 8)
#     SM_DOCKER_ROOT    move Docker's data-root here (default: leave alone)
#     SM_ENROLL_LINK    one-time Hub enrollment link (vpn mode only)
#
#   Example:
#     sudo SM_HUB_URL=https://10.0.0.177 SM_NODE_NAME=mini-eclipse \
#          bash install.sh --unattended
set -euo pipefail

# ── Unattended flag ───────────────────────────────────────────────────────────
# Parsed before anything else so the prompt helpers below can consult it.
UNATTENDED=0
# --advanced restores the prompts for values the installer can work out for
# itself. A normal run only stops for genuine policy questions (mode, NAS on or
# off, TLS verification, moving Docker's data root) and reports everything else
# it decided. This is not cosmetic: a real install answered ~10 prompts and
# every single one took the value already on screen, which trains you to press
# Enter through the two that actually matter.
ADVANCED=0
for _arg in "$@"; do
  case "$_arg" in
    --unattended|-y|--yes) UNATTENDED=1 ;;
    --advanced) ADVANCED=1 ;;
    -h|--help)
      # Print the whole header block rather than a fixed line range: the range
      # was 2,30 and silently started truncating the SM_* list the first time
      # the header grew.
      sed -n '2,/^[^#]/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "install.sh: unknown option '$_arg' (try --help)" >&2; exit 2 ;;
  esac
done
[ "${SM_UNATTENDED:-0}" = "1" ] && UNATTENDED=1
[ "${SM_ADVANCED:-0}" = "1" ] && ADVANCED=1

# Snapshot what the CALLER passed in, before any code below runs.
#
# Several of these names are pre-initialised further down to the value they'd
# hold if the user declined the corresponding prompt (SM_NAS_ENABLED="false",
# SM_HUB_VERIFY_TLS="false", SM_EXTERNAL_BASE="/apps", SM_NAS_HOST=…). Reading
# the live variable at prompt time would therefore see the script's own default
# rather than what was asked for, and silently discard it — passing
# SM_NAS_ENABLED=true really did come back out as false before this existed.
for _v in SM_MODE SM_LAN_IP SM_PORT SM_NODE_NAME SM_HUB_URL SM_HUB_VERIFY_TLS \
          SM_EXTERNAL_BASE SM_NAS_ENABLED SM_NAS_HOST SM_NAS_ROOT SM_GPU \
          SM_SLOT_COUNT SM_DOCKER_ROOT SM_ENROLL_LINK SM_SSH_PORT; do
  eval "_IN_${_v}=\${${_v}-}"
done
unset _v

# Pick a value without asking: a caller-supplied override wins, else the
# default. Used to seed each prompt's default so interactive and unattended
# runs resolve a value identically — the only difference is whether the user is
# offered the chance to change it.
_env_or() {   # _env_or VARNAME default
  local _v="_IN_${1}" _d="${2-}" _cur
  _cur="${!_v-}"
  printf '%s' "${_cur:-$_d}"
}

# Boolean env vars documented as "true | false". Anything else is a mistake, and
# the point is to SAY so: the old behaviour treated every non-"true" value as
# false, so `SM_NAS_ENABLED=1` quietly produced an install with the NAS off —
# the caller asked for a thing and got its opposite, silently. Meanwhile the
# `pick`-based vars (SM_GPU) rejected the same input loudly. Same class of
# mistake deserves the same answer, and the loud one is correct.
_env_bool() {   # _env_bool VARNAME default   -> echoes true|false, exits on junk
  local _name="$1" _d="${2:-false}" _val
  _val="$(_env_or "$_name" "$_d")"
  case "$_val" in
    true|false) printf '%s' "$_val" ;;
    *)
      echo "  ${RED}✗${RST}  Invalid value '${_val}' for ${_name} — expected: true or false" >&2
      echo "     (1/0/yes/no are not accepted; the same rule as SM_GPU)" >&2
      exit 1 ;;
  esac
}

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

confirm() {            # confirm "prompt" [default: y|n]
  local _d="${2:-y}"
  if [ "$UNATTENDED" = "1" ]; then
    echo "    ${DIM}$1 → ${_d}${RST}" >&2
    [[ "$_d" =~ ^[Yy] ]]
    return
  fi
  # The default applies interactively too. It used to apply only when
  # unattended, so pressing Enter always meant YES -- including on
  # "Continue anyway?" after a failed preflight, "Carry on with a tunnel that
  # is not passing traffic?", and "Verify the Hub's TLS certificate?" where
  # every node in the fleet needs no. The prompt said [Y/n] while the caller
  # had explicitly asked for n: the safe answer was written down and then
  # ignored.
  if [[ "$_d" =~ ^[Yy] ]]; then
    ask "${BOLD}$1${RST} [Y/n]"
    read -r _ans
    [[ -z "$_ans" || "$_ans" =~ ^[Yy] ]]
  else
    ask "${BOLD}$1${RST} [y/N]"
    read -r _ans
    [[ "$_ans" =~ ^[Yy] ]]
  fi
}

read_val() {           # read_val "prompt" "default"  →  echoes value
  local prompt="$1" default="$2"
  if [ "$UNATTENDED" = "1" ]; then
    # Echo the choice to stderr so an unattended run still leaves a readable
    # transcript of what it decided — otherwise a scripted install is silent
    # about the very values that are hardest to debug later.
    echo "    ${DIM}${prompt} → ${default:-(blank)}${RST}" >&2
    printf '%s' "$default"
    return
  fi
  ask "${prompt} ${DIM}[${default}]${RST}:"
  read -r _val
  printf '%s' "${_val:-$default}"
}

# A value the installer has already determined (detected IP, hostname, the Hub
# URL out of the enrollment link, a discovered NAS host). Normally taken as-is
# and reported; --advanced turns it back into a real prompt. Unattended runs
# behave as they always did.
#
# The distinction being drawn is "did the installer work this out" vs "is this
# a decision only the operator can make" — NOT "is this important". An
# important value the installer got right is still not worth interrupting for,
# and burying the two real questions among eight rhetorical ones is what made
# them easy to skip past.
read_auto() {          # read_auto "prompt" "default"  →  echoes value
  if [ "$ADVANCED" = "1" ] && [ "$UNATTENDED" != "1" ]; then
    read_val "$1" "$2"
    return
  fi
  echo "    ${DIM}$1 → ${2:-(blank)}${RST}" >&2
  printf '%s' "$2"
}

pick_auto() {          # pick_auto "prompt" default val1 "label1" ...
  if [ "$ADVANCED" = "1" ] && [ "$UNATTENDED" != "1" ]; then
    pick "$@"
    return
  fi
  echo "    ${DIM}$1 → ${2}${RST}" >&2
  printf '%s' "$2"
}

pick() {               # pick "prompt" default  val1 "label1"  val2 "label2"  ...
  local prompt="$1" default="$2"; shift 2
  local -a vals labels
  while (( $# >= 2 )); do vals+=("$1"); labels+=("$2"); shift 2; done
  if [ "$UNATTENDED" = "1" ]; then
    # Validate against the offered set rather than trusting it blindly: a typo
    # in an env var would otherwise be written straight into the env file and
    # only surface as a confusing runtime failure much later.
    local _v
    for _v in "${vals[@]}"; do
      if [[ "$_v" == "$default" ]]; then
        echo "    ${DIM}${prompt} → ${default}${RST}" >&2
        echo "$default"; return
      fi
    done
    echo "  ${RED}✗${RST}  Invalid value '${default}' for '${prompt}' — expected one of: ${vals[*]}" >&2
    exit 2
  fi
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

# ── Who is actually installing this ───────────────────────────────────────────
# The installer is documented to be run with sudo, so a bare `whoami` here says
# "root" and any path built from it belongs to the wrong account. Prefer the
# invoking user, and read their real home from passwd rather than assuming
# /home/<user> — root's home is /root, so guessing produces /home/root, a
# directory that does not exist on any normal system.
INVOKING_USER="${SUDO_USER:-$(whoami)}"
INVOKING_HOME="$(getent passwd "$INVOKING_USER" 2>/dev/null | cut -d: -f6)"
[ -n "$INVOKING_HOME" ] || INVOKING_HOME="/home/${INVOKING_USER}"

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$REPO_ROOT/server"
VENV="$SERVER_DIR/.venv"
ENV_FILE="/etc/sandos-server-manager.env"
PYCACHE_DIR="/var/cache/sandos-server-manager"
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

# Run a command AS a given user, whether or not we are already root.
#
# Not "$SUDO -u user cmd": $SUDO is empty when running as root — which is the
# documented way to invoke this installer — so that expands to "-u user cmd",
# a command which does not exist. It fails every time, and a check written that
# way silently reports "this user cannot do X" no matter what is true.
_as_user() {   # _as_user <user> <cmd...>
  local _u="$1"; shift
  if [ "$EUID" -eq 0 ]; then
    if command -v runuser &>/dev/null; then
      runuser -u "$_u" -- "$@"
    else
      sudo -u "$_u" "$@"
    fi
  else
    sudo -u "$_u" "$@"
  fi
}

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
# Having python3 is not the same as being able to build a venv. Debian ships
# them as separate packages, so a bare Debian or Proxmox host passes the check
# above and then fails several steps later, halfway through the install, with an
# ensurepip error that reads like a Python bug rather than a missing package.
# Check the capability, not the binary, and say exactly what to install.
if ! python3 -c "import ensurepip" &>/dev/null; then
  _pyver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo 3)"
  die "python3 is present but cannot create virtual environments.
     Install it first:  sudo apt install python${_pyver}-venv
     (on Debian and Proxmox this ships separately from python3 itself)"
fi
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

MODE=$(pick "Mode" "$(_env_or SM_MODE lan)" \
  "lan"        "Same LAN          (direct LAN, same subnet as Hub)" \
  "vpn"        "Remote / VPN      (different network, WireGuard tunnel)" \
  "colocated"  "On the Hub        (both services on one device)")

AUTO_IP=$(_lan_ip)

# ── Remote-node reachability preflight ────────────────────────────────────────
# A remote node reaches the Hub over WireGuard, which is UDP. If the network this
# machine sits on blocks outbound UDP — guest wifi, a corporate LAN, a hotel —
# nothing here can work, and without this check that discovery happens LATE: the
# interface comes up (it is just a local device), the installer reports success,
# and the node then fails to do anything with no obvious cause.
#
# Honesty about what each probe proves matters more than a green tick:
#   * TCP to the Hub is definitive — enrollment itself needs it.
#   * Outbound UDP in general is definitive when it FAILS. A DNS query that gets
#     an answer proves UDP leaves this box.
#   * UDP to the WireGuard port specifically cannot be proven from here.
#     WireGuard is silent by design: it never answers an unauthenticated packet,
#     so "no reply" means "open" and "filtered" equally. Only a real handshake
#     settles it, which is why _verify_wg_handshake() below exists and runs after
#     the tunnel comes up.
_preflight_remote() {   # _preflight_remote <hub-host>
  local host="$1" fail=0
  blank
  info "Checking this machine can reach the Hub before changing anything…"

  # 1. TCP to the Hub's dashboard — the enrollment link is fetched over this.
  if command -v curl &>/dev/null && curl -fsSk --max-time 8 -o /dev/null "https://${host}/" 2>/dev/null; then
    ok "Hub reachable over HTTPS (${host})"
  elif command -v nc &>/dev/null && nc -z -w5 "$host" 443 2>/dev/null; then
    ok "Hub TCP 443 reachable (${host})"
  else
    warn "Cannot reach ${host} on TCP 443."
    warn "The enrollment link is fetched over HTTPS, so this must work first."
    fail=1
  fi

  # 2. Does ANY outbound UDP leave this machine? A DNS answer proves it does.
  #    Failure here is the clearest possible signal: WireGuard cannot work.
  local udp_ok=0
  if command -v dig &>/dev/null; then
    dig +time=3 +tries=1 +short @1.1.1.1 cloudflare.com >/dev/null 2>&1 && udp_ok=1
  elif command -v nslookup &>/dev/null; then
    timeout 5 nslookup cloudflare.com 1.1.1.1 >/dev/null 2>&1 && udp_ok=1
  else
    udp_ok=2   # no tool to test with
  fi
  case "$udp_ok" in
    1) ok "Outbound UDP works (DNS query answered)" ;;
    0) warn "No outbound UDP got through (DNS query to 1.1.1.1 failed)."
       warn "WireGuard is UDP-only, so a tunnel cannot work on this network."
       fail=1 ;;
    2) warn "No dig/nslookup here — - skipping the outbound-UDP test." ;;
  esac

  # 3. The WireGuard port itself. Best effort, and said plainly: a silent result
  #    is not a pass. An ICMP rejection, though, IS a definite no.
  if command -v nc &>/dev/null; then
    if nc -z -u -w3 "$host" "${WG_PORT:-51820}" 2>/dev/null; then
      info "UDP ${WG_PORT:-51820} to ${host}: no rejection seen (cannot be confirmed until the tunnel handshakes)"
    else
      warn "UDP ${WG_PORT:-51820} to ${host} was actively rejected — that port looks blocked."
      fail=1
    fi
  fi

  if [ "$fail" -ne 0 ]; then
    blank
    warn "This machine does not look able to reach the Hub the way a remote node must."
    warn "Common causes: guest/corporate wifi blocking UDP, or the Hub's WireGuard"
    warn "port (${WG_PORT:-51820}/udp) not forwarded to it."
    if [ "$UNATTENDED" -eq 1 ]; then
      die "Refusing to install a remote node that cannot reach the Hub."
    fi
    confirm "Continue anyway?" "n" || die "Stopped before changing anything."
  fi
}

# Definitive: did the tunnel actually carry a packet both ways? Everything before
# this is inference; a handshake is proof. Without it the installer would happily
# finish on a node whose tunnel will never pass traffic.
_verify_wg_handshake() {   # _verify_wg_handshake <iface> [seconds]
  local iface="$1" secs="${2:-20}" i hs
  info "Waiting for the tunnel to complete a handshake…"
  for ((i = 0; i < secs; i++)); do
    hs=$($SUDO wg show "$iface" latest-handshakes 2>/dev/null | awk '{print $2}' | sort -rn | head -1)
    if [ -n "$hs" ] && [ "$hs" -gt 0 ] 2>/dev/null; then
      ok "Handshake completed — the tunnel is really passing traffic"
      return 0
    fi
    sleep 1
  done
  warn "No handshake after ${secs}s. The interface is up, but nothing is getting through."
  warn "That almost always means outbound UDP ${WG_PORT:-51820} is blocked on this network,"
  warn "or the Hub's WireGuard port is not reachable from here."
  return 1
}

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
  ENROLL_LINK=$(read_val "Enrollment link (blank to skip)" "$(_env_or SM_ENROLL_LINK "")")
  
  # Runs BEFORE anything is installed or configured. The whole point is to fail
  # here, with a reason, rather than half-way through with a tunnel that will
  # never carry a packet.
  _pf_host=""
  if [ -n "$ENROLL_LINK" ]; then
    _pf_host=$(printf '%s' "$ENROLL_LINK" | sed -E 's#^[a-z]+://##; s#[/?].*##; s#:.*##')
  elif [ -n "${SM_HUB_URL:-}" ]; then
    _pf_host=$(printf '%s' "$SM_HUB_URL" | sed -E 's#^[a-z]+://##; s#[/?].*##; s#:.*##')
  fi
  [ -n "$_pf_host" ] && _preflight_remote "$_pf_host"
  
  if [ -n "$ENROLL_LINK" ]; then
    blank
    info "Setting up the WireGuard enrollment tunnel…"
    command -v curl &>/dev/null || die "curl is required to fetch the enrollment link. Install curl and re-run."

    if $SUDO wg show sandos-hub &>/dev/null; then
      warn "A 'sandos-hub' tunnel is already up — reusing it. (Run 'sudo sandos-wg-enroll down' first to join with a different link.)"
    else
      if ! command -v wg-quick &>/dev/null; then
        info "Installing wireguard-tools…"
        # DEBIAN_FRONTEND: without it apt is free to stop and ask — a conffile
        # difference, a service-restart prompt, or dpkg's "start a shell to
        # examine the situation" option. In the middle of an installer that is
        # baffling: the script appears to hang at a root prompt, and only
        # typing `exit` lets it continue. Answer those the default way instead.
        $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq \
          && $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
             -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
             wireguard-tools
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

    # An interface existing proves nothing — it is a local device that appears
    # whether or not a single packet reaches the Hub. Everything after this point
    # assumes the tunnel carries traffic, so prove it before building on it.
    if ! _verify_wg_handshake sandos-hub 20; then
      if [ "${UNATTENDED:-0}" -eq 1 ]; then
        die "Tunnel never handshook — refusing to continue on an unreachable node."
      fi
      confirm "Carry on with a tunnel that is not passing traffic?" "n" \
        || die "Stopped. Fix outbound UDP ${WG_PORT:-51820} to the Hub, then re-run."
    fi
    AUTO_IP="$ENROLL_WG_IP"

    # Talk to the Hub over the TUNNEL from here on, not the public hostname the
    # enrollment link came from.
    #
    # The Hub serves its dashboard and API to the mesh only — a request arriving
    # from the public internet gets a deliberate 404 ("contact the server
    # administrator"). The enrollment link is the single documented exception,
    # and it has now been used. So a remote node that keeps pointing at the
    # public URL gets 404 for everything afterwards: the fleet NAS lookup in
    # Step 4 silently fails and the node makes ITSELF the NAS, and every Hub
    # session validation fails, which breaks SSO for every user of that node.
    # Confirmed against a live Hub: public URL 404, same request over the
    # tunnel 200.
    #
    # The Hub tells us its own tunnel address in the conf's DNS= line; fall
    # back to the first host of the tunnel subnet, which is where the Hub sits
    # by convention.
    _hub_wg=$($SUDO sh -c "grep -m1 -i '^[[:space:]]*DNS' /etc/sandos/wg-enroll-staging.conf" 2>/dev/null \
      | sed -E 's/.*=[[:space:]]*//; s/,.*//' | tr -d '[:space:]')
    if [ -z "$_hub_wg" ]; then
      _hub_wg=$(echo "$ENROLL_WG_IP" | awk -F. 'NF==4{print $1"."$2"."$3".1"}')
    fi
    if [ -n "$_hub_wg" ] && curl -fsSk --max-time 8 "https://${_hub_wg}/api/fleet/nas-host" >/dev/null 2>&1; then
      ENROLL_HUB_BASE="https://${_hub_wg}"
      ok "Hub reachable over the tunnel at ${_hub_wg} — using it for Hub SSO"
    else
      ENROLL_HUB_BASE="${ENROLL_LINK%%/api/pairing/enroll/*}"
      warn "Couldn't reach the Hub over the tunnel; falling back to ${ENROLL_HUB_BASE}."
      warn "If the Hub only serves the mesh, SSO and NAS discovery will fail there."
    fi
  else
    # No link given. The prompt above explicitly offers this for a machine that
    # ALREADY has its tunnel, so adopt it rather than ignoring it — otherwise a
    # re-run or upgrade on an enrolled node silently reconfigures it back to its
    # LAN address with no Hub URL at all, i.e. standalone with no SSO, and names
    # itself the NAS. Observed exactly that on a live enrolled node.
    _existing_wg=$($SUDO sh -c "grep -m1 '^Address' /etc/wireguard/sandos-hub.conf" 2>/dev/null \
      | sed -E 's/.*=\s*//; s#/.*##' | tr -d '[:space:]')
    if [ -n "$_existing_wg" ]; then
      ok "Found an existing tunnel — this machine's WireGuard IP is ${_existing_wg}"
      AUTO_IP="$_existing_wg"
      _hub_wg=$(echo "$_existing_wg" | awk -F. 'NF==4{print $1"."$2"."$3".1"}')
      if [ -n "$_hub_wg" ] && curl -fsSk --max-time 8 "https://${_hub_wg}/api/fleet/nas-host" >/dev/null 2>&1; then
        ENROLL_HUB_BASE="https://${_hub_wg}"
        ok "Hub reachable over that tunnel at ${_hub_wg} — using it for Hub SSO"
      else
        warn "Tunnel is up but the Hub didn't answer at ${_hub_wg}."
        warn "Set the Hub URL manually below, or SSO stays off."
      fi
    fi
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
    SM_LAN_IP=$(read_auto "LAN IP of this machine" "$(_env_or SM_LAN_IP "$AUTO_IP")")
    SM_TURN_EXTRA_HOST=""
    blank
    info "Apps will be reachable at  http://${SM_LAN_IP}:8170"
    ;;
  vpn)
    echo "  Enter the WireGuard IP assigned to this machine."
    echo "  This IP is used for both the API endpoint and TURN relay"
    echo "  so the Hub and browsers can reach it over the VPN."
    blank
    SM_LAN_IP=$(read_auto "WireGuard IP of this machine" "$(_env_or SM_LAN_IP "$AUTO_IP")")
    SM_TURN_EXTRA_HOST="$SM_LAN_IP"
    blank
    info "API + TURN will use WireGuard IP  ${SM_LAN_IP}"
    ;;
  colocated)
    echo "  Enter the Hub's LAN IP. Both services share this machine;"
    echo "  the Server Manager binds on the same interface."
    blank
    SM_LAN_IP=$(read_auto "This machine's LAN IP" "$(_env_or SM_LAN_IP "$AUTO_IP")")
    SM_TURN_EXTRA_HOST=""
    blank
    info "Co-located at  ${SM_LAN_IP}"
    ;;
esac

blank
SM_PORT=$(read_auto "Server Manager port" "$(_env_or SM_PORT 8170)")
SM_NODE_NAME=$(read_auto "Friendly node name (shown in Hub fleet)" "$(_env_or SM_NODE_NAME "$(hostname)")")

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

SM_HUB_URL=$(read_auto "Hub URL  (e.g. https://10.0.0.177 — blank for standalone)" "$(_env_or SM_HUB_URL "$ENROLL_HUB_BASE")")
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
  SM_EXTERNAL_BASE=$(read_auto "Hub mount path" "$(_env_or SM_EXTERNAL_BASE /apps)")
  blank
  if confirm "Verify the Hub's TLS certificate? (no = accept self-signed Caddy internal CA)" \
       "$([ "$(_env_bool SM_HUB_VERIFY_TLS false)" = "true" ] && echo y || echo n)"; then
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
SM_NAS_ROOT="${INVOKING_HOME}/sandos-nas"

# Ask the Hub which node already self-hosts the fleet's real NFS export
# (GET /api/fleet/nas-host — unauthenticated, exists precisely so a brand
# new node can learn this before it has any Hub session). Without this, the
# obvious-looking default was always "this machine" — which silently turns
# a plain app node into its own (usually nonfunctional, e.g. WSL2 can't
# reliably run an NFS server) NAS instead of pointing it at the real one.
_discovered_nas_host=""
if [ -n "$SM_HUB_URL" ] && command -v curl &>/dev/null; then
  # -k unless the user asked for real verification. A Hub fronted by Caddy's
  # internal CA (the default, and what SM_HUB_VERIFY_TLS=false above already
  # assumes) fails an unqualified curl every time — and because the failure is
  # swallowed below, discovery silently returned nothing and the node quietly
  # made ITSELF the fleet NAS. That is the exact "manual wiring" this lookup
  # exists to remove, so it must use the same TLS posture as the rest of the
  # installer's Hub calls (see the enrollment fetch above, which always -k's).
  _nas_curl_tls=""
  [ "$SM_HUB_VERIFY_TLS" = "true" ] || _nas_curl_tls="-k"
  _nas_info=$(curl -fsS $_nas_curl_tls --max-time 5 "${SM_HUB_URL%/}/api/fleet/nas-host" 2>/dev/null || true)
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

if confirm "Enable the NAS layer?" "$([ "$(_env_bool SM_NAS_ENABLED false)" = "true" ] && echo y || echo n)"; then
  SM_NAS_ENABLED="true"

  _nas_root_default="${INVOKING_HOME}/sandos-nas"
  SM_NAS_ROOT=$(read_auto "Local path to the NAS export root (on the NAS host)" "$(_env_or SM_NAS_ROOT "$_nas_root_default")")

  blank
  if [ -n "$_discovered_nas_host" ]; then
    ok "Found this fleet's NAS already running at ${_discovered_nas_host} — defaulting to it."
  else
    warn "No existing fleet NAS found — defaulting to this machine (${SM_LAN_IP})."
    warn "Only accept this if THIS node should be the shared NAS host."
  fi
  SM_NAS_HOST=$(read_auto "IP of the NFS server host" "$(_env_or SM_NAS_HOST "$SM_NAS_HOST")")

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

SM_GPU=$(pick_auto "GPU support" "$(_env_or SM_GPU "$_gpu_default")" \
  "true"  "Enable  — advertise GPU; streamed apps (FreeCAD) available" \
  "false" "Disable — web apps only (Nextcloud, Files, WebCAD, Renode…)")

# The SM asks Docker for GPU access via the CDI device spec ("nvidia.com/gpu=all"),
# which needs /etc/cdi/nvidia.yaml to actually exist — on native Linux (including
# a plain WSL2 distro running its OWN dockerd directly, no Docker Desktop) this is
# usually generated automatically by the nvidia-container-toolkit apt package's
# own install hook, or by the nvidia-ctk call below when it isn't. Safe to always
# attempt in that case: skipped instantly if the file's already there.
#
# Docker DESKTOP (the common case when this node is a Windows machine using its
# WSL2 integration — confirmed live on one) is a different animal: dockerd itself
# runs inside Docker Desktop's own internal `docker-desktop` VM/distro, a
# filesystem this installer (running in the user's own WSL distro, e.g.
# "Ubuntu-24.04") has no access to at all — writing /etc/cdi/nvidia.yaml HERE
# would land on a filesystem the real daemon never reads, so it would silently
# do nothing useful. (Confirmed live: even after this exact nvidia-ctk step ran
# "successfully" on a fresh Docker-Desktop/WSL2 node, the first real GPU app
# launch still failed with "CDI device injection failed: unresolvable CDI
# devices nvidia.com/gpu=all" — this is why that detection was added.) Docker
# Desktop's own GPU passthrough works through the older `--gpus all` flag
# instead, which SM's docker_backend.py already falls back to automatically
# whenever a CDI launch fails this exact way — nothing to configure here for
# that case, so don't burn time attempting a fix that can't work.
_IS_DOCKER_DESKTOP=false
if [[ "$(docker info --format '{{.OperatingSystem}}' 2>/dev/null)" == "Docker Desktop" ]]; then
  _IS_DOCKER_DESKTOP=true
fi

if [[ "$SM_GPU" == "true" ]] && [[ "$_IS_DOCKER_DESKTOP" == "true" ]]; then
  info "Docker Desktop detected — its own GPU passthrough doesn't use the CDI"
  info "spec this installer would otherwise generate, so skipping that step."
  info "SM's own launch code already retries with Docker Desktop's supported"
  info "flag automatically if a GPU container launch ever needs it."
elif [[ "$SM_GPU" == "true" ]] && [ ! -f /etc/cdi/nvidia.yaml ]; then
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
SM_SLOT_COUNT=$(read_auto "Max concurrent app instances" "$(_env_or SM_SLOT_COUNT 8)")

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
  if confirm "Change where Docker stores images/volumes on this machine?" \
       "$([ -n "${SM_DOCKER_ROOT:-}" ] && echo y || echo n)"; then
    warn "Anything Docker already has at ${_docker_root} becomes invisible to it"
    warn "the moment this changes — NOT deleted, just no longer where Docker looks."
    _new_root=$(read_val "New Docker data directory" "$(_env_or SM_DOCKER_ROOT "$_docker_root")")
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
    # Same reasoning as the wireguard-tools install above: never let apt open an
    # interactive prompt part-way through an unattended-looking install.
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq \
      && $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
         -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
         openssh-server
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
# Carry the reverse-link credential across a reconfigure. It is provisioned by
# the Hub over SSH, never by this installer, so it exists only in the file we
# are about to overwrite. On a node whose inbound port is firewalled shut the
# link is the ONLY way the Hub can reach it, and dropping it here would strand
# the node with nothing anywhere saying why — the failure would look exactly
# like the firewall problem the link exists to solve.
_KEEP_LINK_TOKEN=""
_KEEP_LINK_HUB=""
if [ -f "$ENV_FILE" ]; then
  _KEEP_LINK_TOKEN=$($SUDO sed -n 's/^SM_LINK_TOKEN=//p' "$ENV_FILE" 2>/dev/null | head -1)
  _KEEP_LINK_HUB=$($SUDO sed -n 's/^SM_LINK_HUB=//p' "$ENV_FILE" 2>/dev/null | head -1)
  [ -n "$_KEEP_LINK_TOKEN" ] && ok "Preserving this node's existing reverse link to the Hub"
fi

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

# ── Reverse link (Hub-provisioned) ────────────────────────────────────────────
# Set by the Hub's Fleet page, not by this installer, and preserved verbatim
# when you re-run it. When present this node dials OUT to the Hub and holds
# the connection open, so the Hub never needs to connect in — the only way to
# run a node on a network whose firewall is not yours to change.
# Empty means the feature is off and the Hub reaches this node normally.
SM_LINK_TOKEN=${_KEEP_LINK_TOKEN}
SM_LINK_HUB=${_KEEP_LINK_HUB}
EOF
ok "Wrote ${ENV_FILE}"

# ── 2. Python venv ─────────────────────────────────────────────────────────────
if [ ! -x "${VENV}/bin/uvicorn" ]; then
  info "Creating Python venv and installing dependencies…"
  ( cd "$SERVER_DIR" \
    && python3 -m venv .venv \
    && .venv/bin/pip install -q --upgrade pip \
    && .venv/bin/pip install -q -r requirements.txt )
  # The installer is normally run with sudo, which left the whole venv owned by
  # root inside a repo owned by the user the SERVICE runs as — the service could
  # read it but never modify it, and ownership differed depending on whether
  # install.sh happened to be invoked as root or not. It also broke teardown:
  # uninstall.sh --purge run as a normal user could not delete a root-owned
  # venv, and aborted part-way through, leaving the machine half-uninstalled.
  # Own it as the user the service runs as, regardless of how we got here.
  chown -R "${INVOKING_USER}:$(id -gn "$INVOKING_USER" 2>/dev/null || echo "$INVOKING_USER")" "$VENV" 2>/dev/null || true
  ok "Venv ready at ${VENV}"
else
  ok "Venv already exists — skipping pip install"
fi

# ── 3. Systemd unit ────────────────────────────────────────────────────────────
# Somewhere to put it, owned by the account the service will run as so the
# service can actually write there.
$SUDO mkdir -p "$PYCACHE_DIR"
$SUDO chown "${INVOKING_USER}:$(id -gn "$INVOKING_USER" 2>/dev/null || echo "$INVOKING_USER")" "$PYCACHE_DIR" 2>/dev/null || true
# Clear bytecode an earlier install already wrote into the checkout, which may
# be root-owned and is exactly what blocks a later reinstall.
$SUDO find "$SERVER_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Make sure the service user owns this checkout's git metadata.
#
# Fleet auto-update works by running `git -C <repo_root> pull` on the node as
# that user. Git writes FETCH_HEAD, ORIG_HEAD, refs and objects while doing so,
# so if any of that is owned by someone else — a root-run `git pull` during
# setup, a checkout made by a different account — the pull fails with a
# permission error and the node silently stops updating. Seen in practice:
# .git/FETCH_HEAD and .git/ORIG_HEAD owned by root inside a user-owned clone.
#
# Only .git, not the whole tree: the working files may legitimately belong to
# someone else (a shared or root-owned deployment), but whoever the service runs
# as must be able to write git's own bookkeeping.
if [ -d "${REPO_ROOT}/.git" ]; then
  _grp="$(id -gn "$INVOKING_USER" 2>/dev/null || echo "$INVOKING_USER")"
  if $SUDO chown -R "${INVOKING_USER}:${_grp}" "${REPO_ROOT}/.git" 2>/dev/null; then
    ok "Git metadata owned by ${INVOKING_USER} (auto-update can pull)"
  else
    warn "Couldn't set ownership on ${REPO_ROOT}/.git — fleet auto-update may"
    warn "fail to pull on this node until that is fixed by hand."
  fi
fi

info "Installing systemd unit → ${UNIT_DEST}…"
CURRENT_USER="$INVOKING_USER"
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
# Keep Python's bytecode cache OUT of the repo. Without this the interpreter
# writes __pycache__ next to the source, owned by whoever the service runs as —
# and when that is root (install.sh run with no SUDO_USER, i.e. from a
# provisioning script, cron, or `su -`), the account that owns the checkout can
# no longer `rm -rf` it or `git pull` into it. That silently breaks both a
# reinstall and the fleet auto-update, and leaves a repo its owner cannot
# delete. Redirecting is better than chowning after the fact: it needs no
# assumption about who should own the checkout, and it holds for every service
# user rather than only the one install.sh happened to see.
Environment=PYTHONPYCACHEPREFIX=${PYCACHE_DIR}
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
# Mesh NAS pool helper: creating, growing and shrinking a loop-mounted storage
# reservation needs root. Installed as a fixed, root-owned script with a narrow
# sudoers entry rather than granting the service user general privileges — the
# same pattern the Sand-OS node uses for its sand-* helpers.
POOL_HELPER=/usr/local/lib/sandos-sm-pool
if [ -f "${REPO_ROOT}/scripts/sandos-sm-pool" ]; then
  $SUDO install -o root -g root -m 0750 "${REPO_ROOT}/scripts/sandos-sm-pool" "$POOL_HELPER"
  ok "NAS pool helper installed at ${POOL_HELPER}"
else
  warn "scripts/sandos-sm-pool not found — this node cannot contribute NAS storage"
fi

# Mesh NAS gateway helper. Installed everywhere rather than only where it is
# needed: which node ends up serving a gateway is the Hub's decision, made after
# install from the whole fleet's state, so any node may be asked. A node never
# chosen simply never runs it.
GATEWAY_HELPER=/usr/local/lib/sandos-sm-gateway
if [ -f "${REPO_ROOT}/scripts/sandos-sm-gateway" ]; then
  $SUDO install -o root -g root -m 0750 "${REPO_ROOT}/scripts/sandos-sm-gateway" "$GATEWAY_HELPER"
  ok "Mesh gateway helper installed at ${GATEWAY_HELPER}"
fi

# Per-drive cluster allocation helper. A server may contribute from several
# drives at different amounts, and which drives those are is set from the Hub
# after install — so like the gateway helper, it goes on every node and simply
# goes unused on one that stores nothing.
CLUSTER_HELPER=/usr/local/lib/sandos-sm-cluster
if [ -f "${REPO_ROOT}/scripts/sandos-sm-cluster" ]; then
  $SUDO install -o root -g root -m 0750 "${REPO_ROOT}/scripts/sandos-sm-cluster" "$CLUSTER_HELPER"
  ok "Cluster allocation helper installed at ${CLUSTER_HELPER}"
fi

_sudoers_tmp=$(mktemp)
{
  echo "${CURRENT_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart ${UNIT_NAME}"
  [ -f "$POOL_HELPER" ] && echo "${CURRENT_USER} ALL=(root) NOPASSWD: ${POOL_HELPER}"
  [ -f "$GATEWAY_HELPER" ] && echo "${CURRENT_USER} ALL=(root) NOPASSWD: ${GATEWAY_HELPER}"
  [ -f "$CLUSTER_HELPER" ] && echo "${CURRENT_USER} ALL=(root) NOPASSWD: ${CLUSTER_HELPER}"
} > "$_sudoers_tmp"
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

# ── 4. Docker socket access for the service user ──────────────────────────────
# The unit runs as CURRENT_USER and every app launch shells out to `docker`, so
# without membership of the docker group the install "succeeds", the service
# starts, and then EVERY app launch fails forever with
#   permission denied while trying to connect to the docker API at
#   unix:///var/run/docker.sock
# That is silent: nothing in the install output hints at it, because the
# service itself is perfectly healthy — only the thing it exists to do is
# broken. It went unnoticed on existing nodes because their accounts had been
# added to the group by hand long ago.
#
# Membership is evaluated when a process starts, so the group must be granted
# BEFORE the service is (re)started below.
if ! _as_user "$CURRENT_USER" docker info &>/dev/null; then
  if getent group docker >/dev/null 2>&1; then
    info "Granting ${CURRENT_USER} access to the Docker socket…"
    $SUDO usermod -aG docker "$CURRENT_USER"
    if _as_user "$CURRENT_USER" docker info &>/dev/null; then
      ok "${CURRENT_USER} can now reach Docker"
    else
      # Adding to a group does not affect already-running sessions; the service
      # started below gets it regardless, which is what actually matters here.
      ok "Added ${CURRENT_USER} to the docker group"
      info "Your own shell won't see this until you log out and back in —"
      info "the service picks it up when it starts, so apps still work now."
    fi
    warn "Note: docker group membership is effectively root-level access on"
    warn "this machine. That is what running app containers requires."
  else
    warn "No 'docker' group on this system — ${CURRENT_USER} cannot reach the"
    warn "Docker socket, so no app will be able to launch. Check how Docker was"
    warn "installed, then re-run this installer."
  fi
else
  ok "${CURRENT_USER} can reach Docker"
fi

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

# ── Return-path check ─────────────────────────────────────────────────────────
# Everything up to here tested that this machine can reach OUT. Nothing tested
# whether the Hub can reach BACK, which is the half that actually decides
# whether the node works: the Hub drives a node by connecting to :$SM_PORT. A
# real install passed every check, handshaked its tunnel, answered on SSH, and
# was still invisible in the fleet because a local firewall rejected that one
# port -- and the installer finished looking successful, so the failure was
# only discovered much later, by hand.
#
# This DETECTS AND REPORTS ONLY. It never edits nftables/ufw/firewalld. On a
# work or university machine those rules are somebody else's policy, and an
# installer that quietly opens ports there is doing something it has no
# business doing. It prints what the operator may choose to run, and names the
# option that needs no firewall change at all.
if [ -n "$SM_HUB_URL" ] && command -v curl &>/dev/null; then
  blank
  info "Checking the Hub can reach back to this machine…"
  # -k unless the operator asked for real verification: the Hub's cert is
  # Caddy's internal CA, which a fresh node has no reason to trust yet.
  _rb_insecure=""
  [ "$SM_HUB_VERIFY_TLS" = "true" ] || _rb_insecure="-k"
  _rb=$(curl -fsS $_rb_insecure --max-time 15 \
          "${SM_HUB_URL%/}/api/fleet/reachback?port=${SM_PORT}" 2>/dev/null || true)
  _rb_ok=$(printf '%s' "$_rb" | python3 -c "
import json, sys
try:
    print('yes' if json.load(sys.stdin).get('reachable') else 'no')
except Exception:
    print('unknown')
" 2>/dev/null || echo unknown)
  _rb_detail=$(printf '%s' "$_rb" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('detail') or '')
except Exception:
    print('')
" 2>/dev/null || echo "")

  if [ "$_rb_ok" = "yes" ]; then
    ok "Hub → this machine on :${SM_PORT} — reachable"
  elif [ "$_rb_ok" = "unknown" ]; then
    warn "Couldn't ask the Hub to test the return path (older Hub, or unreachable)."
    warn "Not a failure by itself — but the return path is unverified."
  else
    blank
    warn "The Hub CANNOT reach this machine on :${SM_PORT}."
    [ -n "$_rb_detail" ] && echo "    ${DIM}${_rb_detail}${RST}"
    blank
    echo "  ${BOLD}This node will install correctly and still never appear in the fleet.${RST}"
    blank
    echo "  ${BOLD}Option 1 — no firewall change (use this on a machine you don't own)${RST}"
    echo "  $HR"
    echo "  Have this node dial OUT to the Hub and hold the connection open, so"
    echo "  nothing ever needs to connect in. On the Hub's Fleet page, enable"
    echo "  \"Reverse link\" for this node. Nothing here has to be opened."
    blank
    echo "  ${BOLD}Option 2 — allow the port (only if this machine's policy is yours)${RST}"
    echo "  $HR"
    # Detected, printed, and left entirely to the operator to run or ignore.
    if command -v firewall-cmd &>/dev/null; then
      echo "    ${DIM}firewalld detected${RST}"
      if [ -n "${ENROLL_WG_IP:-}" ]; then
        echo "    sudo firewall-cmd --permanent --zone=trusted --add-interface=sandos-hub"
      else
        echo "    sudo firewall-cmd --permanent --add-port=${SM_PORT}/tcp"
      fi
      echo "    sudo firewall-cmd --reload"
    elif command -v ufw &>/dev/null; then
      echo "    ${DIM}ufw detected${RST}"
      if [ -n "${ENROLL_WG_IP:-}" ]; then
        echo "    sudo ufw allow in on sandos-hub to any port ${SM_PORT} proto tcp"
      else
        echo "    sudo ufw allow ${SM_PORT}/tcp"
      fi
    elif command -v nft &>/dev/null; then
      echo "    ${DIM}nftables detected — inspect first:${RST}"
      echo "    sudo nft list ruleset | head -40"
    else
      echo "    ${DIM}No familiar firewall tool found; check with your administrator.${RST}"
    fi
    blank
    echo "  ${DIM}Nothing was changed on this machine's firewall.${RST}"
  fi
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
