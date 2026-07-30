#!/usr/bin/env bash
# Sand-OS Server Manager — Uninstaller
# Reverses install.sh: stops + disables the sandos-server-manager systemd
# service and removes the unit file + env file it wrote.
#
# Usage:  sudo bash uninstall.sh            (keep the Python venv)
#         sudo bash uninstall.sh --purge    (also remove the venv)
set -euo pipefail

# ── Colour / terminal helpers (match install.sh) ──────────────────────────────
if [ -t 1 ] && command -v tput &>/dev/null && tput setaf 1 &>/dev/null 2>&1; then
  BOLD=$(tput bold);  RST=$(tput sgr0)
  RED=$(tput setaf 1); GRN=$(tput setaf 2); YLW=$(tput setaf 3)
  CYN=$(tput setaf 6); WHT=$(tput setaf 7)
  DIM=$(tput dim 2>/dev/null || echo "")
else
  BOLD=''; RST=''; RED=''; GRN=''; YLW=''; CYN=''; WHT=''; DIM=''
fi

HR="${DIM}$(printf '─%.0s' $(seq 1 64))${RST}"

header() {
  clear 2>/dev/null || true
  echo
  printf "  %s%sSand-OS Server Manager%s  ·  Uninstaller\n" "$BOLD" "$CYN" "$RST"
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

confirm() {
  if [ "${ASSUME_YES:-0}" = "1" ]; then
    echo "    ${DIM}$1 → yes${RST}"
    return 0
  fi
  printf "    %s [y/N] " "${BOLD}$1${RST}"
  read -r _ans
  [[ "$_ans" =~ ^[Yy] ]]
}

# ── Flags ──────────────────────────────────────────────────────────────────────
PURGE=0
WIPE_DOCKER=0
ASSUME_YES=0
KEEP_NAS=-1      # -1 = not stated; ask, or fall back to --purge/--yes
REMOVE_NAS=0
REMOVE_TUNNEL=0
DEREGISTER=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --wipe-docker) WIPE_DOCKER=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    # Explicit answers for the NAS-storage question, so an unattended teardown
    # never has to imply one. Without these, --yes would answer it too, and
    # "delete the venv" would quietly mean "delete the files as well".
    --keep-nas)   KEEP_NAS=1 ;;
    --remove-nas) KEEP_NAS=0; REMOVE_NAS=1 ;;
    --remove-tunnel) REMOVE_TUNNEL=1 ;;
    --deregister) DEREGISTER=1 ;;
    # Decommissioning this machine for good: everything local, plus telling the
    # Hub to forget it. Separate from the default because reinstalling on the
    # same box is the far more common reason to run this, and that case wants
    # the tunnel and the Hub registration kept.
    --full) PURGE=1; WIPE_DOCKER=1; REMOVE_TUNNEL=1; DEREGISTER=1 ;;
    -h|--help)
      echo "usage: sudo bash uninstall.sh [--purge] [--wipe-docker] [--yes]"
      echo "                              [--remove-tunnel] [--deregister] [--full]"
      echo "  --purge          also remove the Python venv (server/.venv)"
      echo "  --yes, -y        assume yes to every confirmation (scripted teardown)"
      echo "  --keep-nas       keep this node's contributed NAS storage and its files"
      echo "  --remove-nas     delete this node's NAS storage and everything in it"
      echo "  --wipe-docker    also remove every sm-* container/volume/network/image"
      echo "                   this node ever created — a real, destructive teardown"
      echo "                   of all locally-installed app data, for a genuinely"
      echo "                   clean slate before reinstalling with different settings"
      echo "  --remove-tunnel  also tear down the WireGuard enrollment tunnel and"
      echo "                   delete its config — the credential stops working, so"
      echo "                   rejoining needs a fresh enrollment link"
      echo "  --deregister     tell the Hub to forget this node: registry row,"
      echo "                   telemetry history, its WireGuard peer, scoped"
      echo "                   firewall rules and any staged files on the NAS"
      echo "  --full           everything above — decommission this machine"
      exit 0 ;;
    *) die "Unknown option: $arg" ;;
  esac
done

# ── Paths (must match install.sh) ─────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$REPO_ROOT/server"
VENV="$SERVER_DIR/.venv"
ENV_FILE="/etc/sandos-server-manager.env"
UNIT_NAME="sandos-server-manager"
UNIT_DEST="/etc/systemd/system/${UNIT_NAME}.service"
SUDOERS_FILE="/etc/sudoers.d/61-sandos-sm-restart"
CLI_SHIM="/usr/local/bin/server-manager"
PYCACHE_DIR="/var/cache/sandos-server-manager"

# This runs under sudo, so a bare ~ is root's home, not the account that owns
# the install — the per-node app library would be looked for in the wrong place
# and silently never found. Same resolution install.sh uses.
INVOKING_USER="${SUDO_USER:-$(whoami)}"
INVOKING_HOME="$(getent passwd "$INVOKING_USER" 2>/dev/null | cut -d: -f6)"
[ -n "$INVOKING_HOME" ] || INVOKING_HOME="/home/${INVOKING_USER}"
CATALOG_STATE_DIR="${INVOKING_HOME}/.sandos-sm"
WG_IFACE="sandos-hub"
WG_CONF="/etc/wireguard/${WG_IFACE}.conf"
WG_STAGING_DIR="/etc/sandos"


# ── Sudo wrapper ──────────────────────────────────────────────────────────────
if [ "$EUID" -eq 0 ]; then
  SUDO=""
else
  command -v sudo &>/dev/null || die "Not root and sudo not found. Run as root."
  SUDO="sudo"
  $SUDO true
fi

# Read the node's own identity BEFORE step 3 deletes the env file. Deregistering
# needs to know which Hub to call and which node to name, and by then the file
# that says so is gone.
#
# Must come after the sudo wrapper above: $SUDO does not exist until then, and
# under `set -u` using it earlier aborts the whole script.
SM_HUB_URL_SAVED=""
SM_NODE_NAME_SAVED=""
SM_LAN_IP_SAVED=""
if [ -f "$ENV_FILE" ]; then
  SM_HUB_URL_SAVED="$($SUDO grep -oP '^SM_HUB_URL=\K.*' "$ENV_FILE" 2>/dev/null || true)"
  SM_NODE_NAME_SAVED="$($SUDO grep -oP '^SM_NODE_NAME=\K.*' "$ENV_FILE" 2>/dev/null || true)"
  SM_LAN_IP_SAVED="$($SUDO grep -oP '^SM_LAN_IP=\K.*' "$ENV_FILE" 2>/dev/null || true)"
fi

header

cat << INTRO
  This will remove the sandos-server-manager systemd service from this
  machine:

    • stop + disable  ${UNIT_NAME}
    • remove  ${UNIT_DEST}
    • remove  ${ENV_FILE}
    • remove  ${SUDOERS_FILE}
    • remove  ${CLI_SHIM}
$( [ "$PURGE" -eq 1 ] && echo "    • remove  ${VENV}  (--purge)" )
$( [ "$PURGE" -eq 1 ] && echo "    • remove  ${CATALOG_STATE_DIR}  — this node's enabled-app list (--purge)" )
$( [ "$WIPE_DOCKER" -eq 1 ] && echo "    • remove EVERY sm-* container/volume/network/image on this node (--wipe-docker)" )
$( [ "$DEREGISTER" -eq 1 ] && echo "    • tell the Hub to forget this node entirely (--deregister)" )
$( [ "$REMOVE_TUNNEL" -eq 1 ] && echo "    • tear down the WireGuard tunnel + delete ${WG_CONF} (--remove-tunnel)" )

  This does NOT touch:
    • NAS-backed project/app data
$( [ "$REMOVE_TUNNEL" -eq 0 ] && echo "    • any WireGuard enrollment tunnel set up during install — pass --remove-tunnel to also remove it" )
$( [ "$WIPE_DOCKER" -eq 0 ] && echo "    • running app containers (FreeCAD, Nextcloud, WebCAD, …) or their images — pass --wipe-docker to also remove these" )

INTRO

confirm "Proceed with uninstall?" || { blank; warn "Aborted — no changes made."; exit 0; }

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — STOP + DISABLE SERVICE
# ═══════════════════════════════════════════════════════════════════════════════
header
step 1 "Stop + Disable Service"

if $SUDO systemctl list-unit-files "${UNIT_NAME}.service" &>/dev/null; then
  info "Stopping ${UNIT_NAME}…"
  $SUDO systemctl stop "$UNIT_NAME" 2>/dev/null || true
  info "Disabling ${UNIT_NAME}…"
  $SUDO systemctl disable "$UNIT_NAME" 2>/dev/null || true
  ok "Service stopped and disabled"
else
  warn "Service ${UNIT_NAME} not found — skipping"
fi

# Stop any detached dev instance holding the port too.
pkill -f "uvicorn app.main" 2>/dev/null || true

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — REMOVE SYSTEMD UNIT
# ═══════════════════════════════════════════════════════════════════════════════
header
step 2 "Remove Systemd Unit"

if [ -f "$UNIT_DEST" ]; then
  $SUDO rm -f "$UNIT_DEST"
  $SUDO systemctl daemon-reload
  ok "Removed ${UNIT_DEST}"
else
  warn "${UNIT_DEST} not present — skipping"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — REMOVE ENV FILE
# ═══════════════════════════════════════════════════════════════════════════════
header
step 3 "Remove Env File"

if [ -f "$ENV_FILE" ]; then
  $SUDO rm -f "$ENV_FILE"
  ok "Removed ${ENV_FILE}"
else
  warn "${ENV_FILE} not present — skipping"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 4 — REMOVE SUDOERS RULE
# ═══════════════════════════════════════════════════════════════════════════════
header
step 4 "Remove Sudoers Rule"

# The NAS pool holds a preallocated image that is REAL disk space — leaving it
# behind means an uninstalled node keeps tens of gigabytes hostage with nothing
# left on the machine to explain what took them. Only under --purge, though:
# a plain uninstall is usually a prelude to reinstalling on the same box, and
# silently discarding contributed storage would be a nasty surprise.
# The NAS pool holds a preallocated image that is REAL disk space, and it may
# hold the only copy of files stored on this node. Never decided by a flag
# alone: the operator is shown exactly how much space and how much data is at
# stake, and asked. --purge/--yes answer the question rather than skip it.
POOL_HELPER=/usr/local/lib/sandos-sm-pool
if [ -x "$POOL_HELPER" ]; then
  _pool_json="$($SUDO "$POOL_HELPER" status 2>/dev/null || echo '{}')"
  _pool_exists=$(printf '%s' "$_pool_json" | grep -o '"exists":[a-z]*' | cut -d: -f2)
  if [ "${_pool_exists:-false}" = "true" ]; then
    _pool_size=$(printf '%s' "$_pool_json" | grep -o '"image_bytes":[0-9]*' | cut -d: -f2)
    _pool_used=$(printf '%s' "$_pool_json" | grep -o '"used_bytes":[0-9]*' | cut -d: -f2)
    blank
    warn "This node contributes $(( ${_pool_size:-0} / 1024 / 1024 / 1024 ))G to the mesh NAS,"
    warn "holding $(( ${_pool_used:-0} / 1024 / 1024 ))M of data."
    warn "Removing it frees the space and DELETES anything stored only here."
    _drop_pool=0
    if   [ "$KEEP_NAS" -eq 1 ];   then _drop_pool=0
    elif [ "$REMOVE_NAS" -eq 1 ]; then _drop_pool=1
    elif confirm "Delete this node's NAS storage and its contents?"; then _drop_pool=1
    fi
    if [ "$_drop_pool" -eq 1 ]; then
      if $SUDO "$POOL_HELPER" destroy >/dev/null 2>&1; then
        ok "NAS pool storage released back to this machine"
        $SUDO rm -f "$POOL_HELPER"
      else
        warn "Could not release the NAS pool (still mounted or in use) — run:"
        warn "  sudo ${POOL_HELPER} destroy"
      fi
    else
      ok "NAS pool left in place — its files and reserved space are untouched"
      info "Reclaim it later with: sudo ${POOL_HELPER} destroy"
    fi
  else
    $SUDO rm -f "$POOL_HELPER"
  fi
fi

# The other privileged helpers install.sh places. Removed unconditionally: unlike
# the pool helper they hold no data and reserve no space, so there is no question
# to ask — leaving them behind just means a "removed" node still carries root-
# capable scripts that its sudoers rule no longer scopes.
#
# Kept in step with install.sh deliberately: a helper added there and forgotten
# here is invisible until someone uninstalls and finds it still sitting on disk,
# which is exactly how sandos-sm-cluster survived a --full teardown.
for _h in /usr/local/lib/sandos-sm-cluster /usr/local/lib/sandos-sm-gateway; do
  if [ -e "$_h" ]; then
    # A node serving the mesh over NFS should stop doing that before its helper
    # disappears, or the export outlives the thing that manages it.
    [ "$_h" = /usr/local/lib/sandos-sm-gateway ] && $SUDO "$_h" stop >/dev/null 2>&1
    $SUDO rm -f "$_h"
    ok "Removed ${_h}"
  fi
done

if [ -f "$SUDOERS_FILE" ]; then
  $SUDO rm -f "$SUDOERS_FILE"
  ok "Removed ${SUDOERS_FILE}"
else
  warn "${SUDOERS_FILE} not present — skipping"
fi

# install.sh also drops a `server-manager` shim in /usr/local/bin pointing at
# the repo's TUI. Leaving it behind left a command on PATH that outlives the
# thing it drives — after --purge it points at a venv that no longer exists.
if [ -f "$CLI_SHIM" ]; then
  $SUDO rm -f "$CLI_SHIM"
  ok "Removed ${CLI_SHIM}"
else
  warn "${CLI_SHIM} not present — skipping"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 5 — WIPE SM DOCKER RESOURCES (--wipe-docker only)
# ═══════════════════════════════════════════════════════════════════════════════
header
step 5 "Wipe SM Docker Resources"

if [ "$WIPE_DOCKER" -eq 1 ]; then
  warn "This deletes EVERY container, volume, network, and image this node ever"
  warn "created for any app (FreeCAD, Nextcloud, WebCAD, Ollama, …) — all of it,"
  warn "not just what's currently running. This cannot be undone."
  if confirm "Really wipe all sm-* Docker resources on this node?"; then
    _containers=$(docker ps -a --filter "name=^sm-" -q)
    _volumes=$(docker volume ls --filter "name=^sm-" -q)
    _networks=$(docker network ls --filter "name=^sm-" -q)
    [ -n "$_containers" ] && docker rm -f $_containers >/dev/null && ok "Removed $(echo "$_containers" | wc -l) container(s)"
    [ -n "$_volumes" ] && docker volume rm -f $_volumes >/dev/null && ok "Removed $(echo "$_volumes" | wc -l) volume(s)"
    [ -n "$_networks" ] && docker network rm $_networks >/dev/null 2>&1
    _images=$(docker images --filter "reference=sm-*" -q)
    if [ -n "$_images" ]; then
      docker rmi -f $_images >/dev/null 2>&1 && ok "Removed $(echo "$_images" | wc -l) locally-built sm-* image(s)"
    fi

    # Public upstream images an app pulls (stirlingtools/stirling-pdf,
    # ollama/ollama, onlyoffice/…) carry no sm- prefix, so the reference filter
    # above never matched them and a "genuinely clean slate" still left GBs
    # behind — a 1 GB Stirling image survived a full --purge --wipe-docker.
    #
    # They are not unidentifiable though: the catalogue declares the exact tag
    # each app uses, so ask it rather than guessing at names. Runs before the
    # venv is removed in the next step, and falls back to the system python if
    # the venv has already gone.
    _py="${VENV}/bin/python"
    [ -x "$_py" ] || _py="$(command -v python3 || true)"
    _catalog_tags=""
    if [ -n "$_py" ]; then
      _catalog_tags=$("$_py" -c "
import sys
sys.path.insert(0, '${SERVER_DIR}')
try:
    from app import registry
except Exception:
    raise SystemExit(0)
for a in registry.CATALOG.values():
    for t in (getattr(a, 'image', None), getattr(a, 'packaged_image', None)):
        if t and not t.startswith('sm-'):
            print(t)
" 2>/dev/null || true)
    fi
    if [ -n "$_catalog_tags" ]; then
      _removed=0
      while IFS= read -r _tag; do
        [ -n "$_tag" ] || continue
        if docker image inspect "$_tag" &>/dev/null; then
          docker rmi -f "$_tag" &>/dev/null && _removed=$((_removed + 1))
        fi
      done <<< "$_catalog_tags"
      [ "$_removed" -gt 0 ] && ok "Removed ${_removed} upstream app image(s) named by the catalogue"
    else
      warn "Couldn't read the app catalogue — upstream images (Stirling PDF,"
      warn "Ollama, …) left in place. Remove by hand with 'docker rmi <tag>'."
    fi
    # Helper images the SM pulls ITSELF, not on behalf of any catalogue app:
    # docker_backend shells out to a throwaway alpine to mkdir per-user paths
    # on the NAS export. Nothing named it, so it survived --wipe-docker on
    # every NAS-enabled node even though the SM is what put it there.
    for _helper in alpine; do
      if docker image inspect "$_helper" &>/dev/null; then
        docker rmi -f "$_helper" &>/dev/null && ok "Removed helper image ${_helper}"
      fi
    done
    info "Images this node pulled for anything OTHER than a catalogue app are"
    info "left alone — they can't be told apart from an unrelated use of the"
    info "same tag elsewhere on this machine."
    ok "Docker resources wiped"
  else
    warn "Skipped — nothing removed."
  fi
else
  info "Skipped (pass --wipe-docker to also remove all sm-* Docker resources)"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6 — PYTHON VENV (--purge only)
# ═══════════════════════════════════════════════════════════════════════════════
header
step 6 "Python Venv"

if [ "$PURGE" -eq 1 ]; then
  if [ -d "$VENV" ]; then
    # $SUDO, not a bare rm: an installer run with sudo used to leave this venv
    # owned by root, and a plain `rm -rf` then failed on every file. Under
    # `set -e` that aborted the whole uninstall part-way — service and env file
    # already gone, venv and app library still present, and no error summary,
    # so the machine was left half-uninstalled and looked done. install.sh now
    # fixes the ownership, but old installs still have root-owned venvs and
    # must still be removable.
    $SUDO rm -rf "$VENV"
    ok "Removed ${VENV}"
  else
    warn "${VENV} not present — skipping"
  fi
  # The per-node app library (which catalogue apps are enabled here). A fresh
  # install is meant to start with an EMPTY library and show only what the
  # owner installs; leaving this behind meant a reinstall silently inherited
  # the previous node's enabled set and was not fresh at all.
  if [ -d "$CATALOG_STATE_DIR" ]; then
    $SUDO rm -rf "$CATALOG_STATE_DIR"
    ok "Removed ${CATALOG_STATE_DIR} (per-node app library)"
  else
    warn "${CATALOG_STATE_DIR} not present — skipping"
  fi
  # Python bytecode. Newer installs redirect it here; older ones wrote it into
  # the checkout owned by the service user, which on a root-run install leaves
  # the repo undeletable by its own owner. Removed with privileges for exactly
  # that case.
  [ -d "$PYCACHE_DIR" ] && { $SUDO rm -rf "$PYCACHE_DIR"; ok "Removed ${PYCACHE_DIR}"; }
  if $SUDO find "$SERVER_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null; then
    ok "Cleared any bytecode left inside the checkout"
  fi
else
  info "Keeping ${VENV} (pass --purge to remove)"
  [ -d "$CATALOG_STATE_DIR" ] && \
    info "Keeping ${CATALOG_STATE_DIR} — this node's enabled-app list survives a reinstall"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 7 — TELL THE HUB TO FORGET THIS NODE (--deregister)
# ═══════════════════════════════════════════════════════════════════════════════
header
step 7 "Deregister From Hub"

if [ "$DEREGISTER" -eq 1 ]; then
  if [ -z "$SM_HUB_URL_SAVED" ]; then
    warn "This node had no Hub configured — nothing to deregister from."
  elif ! command -v curl >/dev/null 2>&1; then
    warn "curl not available — deregister by hand from the Hub's Fleet page."
  else
    _id="${SM_LAN_IP_SAVED:-$SM_NODE_NAME_SAVED}"
    info "Asking ${SM_HUB_URL_SAVED} to forget '${_id}'…"
    # Runs BEFORE the tunnel is torn down below: on a remote node the Hub is
    # only reachable through that tunnel, so revoking it first would remove the
    # path needed to make this call.
    #
    # -k: the Hub is normally fronted by Caddy's internal CA, same as every other
    # Hub call in this project.
    # Capture the body AND the status separately, and do NOT discard curl's
    # stderr: "the Hub did not answer" is useless when the real answer was 403
    # or a TLS error, and this is the step whose silent failure leaves a live
    # credential behind.
    _body_file="$(mktemp)"
    _code="$(curl -sSk --max-time 20 -X POST \
      -H 'Content-Type: application/json' \
      -d "{\"node_id\": \"${_id}\"}" \
      -o "$_body_file" -w '%{http_code}' \
      "${SM_HUB_URL_SAVED%/}/api/fleet/nodes/deregister" 2>"${_body_file}.err" || echo 000)"
    _resp="$(cat "$_body_file" 2>/dev/null)"
    _curl_err="$(cat "${_body_file}.err" 2>/dev/null)"
    rm -f "$_body_file" "${_body_file}.err"
    if [ "$_code" != "200" ]; then
      warn "The Hub refused the deregister (HTTP ${_code})."
      [ -n "$_resp" ] && echo "      ${DIM}${_resp}${RST}"
      [ -n "$_curl_err" ] && echo "      ${DIM}${_curl_err}${RST}"
      warn "It still lists this node and its WireGuard peer is still valid —"
      warn "remove it from the Fleet page so that credential does not stay live."
      _resp=""
    fi
    if [ -n "$_resp" ]; then
      ok "Hub acknowledged — registry, history, peer and staged files removed"
      echo "      ${DIM}${_resp}${RST}"
    else
      warn "The Hub did not answer. It still lists this node, and its WireGuard"
      warn "peer is still valid — remove it from the Fleet page so that stale"
      warn "credential does not stay live."
    fi
  fi
else
  info "Skipped (pass --deregister to have the Hub forget this node)"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 8 — REMOVE THE WIREGUARD TUNNEL (--remove-tunnel)
# ═══════════════════════════════════════════════════════════════════════════════
header
step 8 "WireGuard Tunnel"

if [ "$REMOVE_TUNNEL" -eq 1 ]; then
  if command -v sandos-wg-enroll >/dev/null 2>&1; then
    $SUDO sandos-wg-enroll down >/dev/null 2>&1 || true
  else
    $SUDO wg-quick down "$WG_IFACE" >/dev/null 2>&1 || true
  fi
  $SUDO rm -f "$WG_CONF" "${WG_STAGING_DIR}/wg-enroll-staging.conf"
  $SUDO rm -f /usr/local/bin/sandos-wg-enroll /etc/sudoers.d/63-sandos-wg-enroll
  $SUDO rmdir "$WG_STAGING_DIR" 2>/dev/null || true
  if ip link show "$WG_IFACE" >/dev/null 2>&1; then
    warn "Interface ${WG_IFACE} is still up — check: sudo wg show ${WG_IFACE}"
  else
    ok "Tunnel down, config and helper removed"
  fi
else
  info "Skipped (pass --remove-tunnel to also tear down the tunnel)"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════════════════════
header
echo "  ${BOLD}${GRN}Uninstall complete!${RST}"
blank
if [ "$PURGE" -eq 0 ]; then
  info "Re-run  sudo bash ${REPO_ROOT}/install.sh  any time to reinstall (venv reused)."
fi
# install.sh may have added this account to the docker group. That is left in
# place on purpose: it is a machine-level grant that other things may now rely
# on, and the installer only adds it when the account could not already reach
# Docker, so it cannot tell whether it was the one that granted it. Say so
# rather than reversing it silently or leaving it undisclosed.
if id -nG "$INVOKING_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
  warn "${INVOKING_USER} is still in the 'docker' group (effectively root on this"
  warn "machine). Left in place deliberately — remove with:"
  warn "  sudo gpasswd -d ${INVOKING_USER} docker"
  blank
fi

if [ "$WIPE_DOCKER" -eq 1 ]; then
  info "sm-* app containers/volumes/networks/images were wiped per --wipe-docker."
  info "NAS-backed project/app data was left in place."
else
  warn "App containers, images, and NAS data were left in place — pass"
  warn "--wipe-docker (or remove manually via docker) for a full teardown."
fi
blank
