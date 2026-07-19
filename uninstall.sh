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
  printf "    %s [y/N] " "${BOLD}$1${RST}"
  read -r _ans
  [[ "$_ans" =~ ^[Yy] ]]
}

# ── Flags ──────────────────────────────────────────────────────────────────────
PURGE=0
WIPE_DOCKER=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --wipe-docker) WIPE_DOCKER=1 ;;
    -h|--help)
      echo "usage: sudo bash uninstall.sh [--purge] [--wipe-docker]"
      echo "  --purge         also remove the Python venv (server/.venv)"
      echo "  --wipe-docker   also remove every sm-* container/volume/network/image"
      echo "                  this node ever created — a real, destructive teardown"
      echo "                  of all locally-installed app data, for a genuinely"
      echo "                  clean slate before reinstalling with different settings"
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

# ── Sudo wrapper ──────────────────────────────────────────────────────────────
if [ "$EUID" -eq 0 ]; then
  SUDO=""
else
  command -v sudo &>/dev/null || die "Not root and sudo not found. Run as root."
  SUDO="sudo"
  $SUDO true
fi

header

cat << INTRO
  This will remove the sandos-server-manager systemd service from this
  machine:

    • stop + disable  ${UNIT_NAME}
    • remove  ${UNIT_DEST}
    • remove  ${ENV_FILE}
    • remove  ${SUDOERS_FILE}
$( [ "$PURGE" -eq 1 ] && echo "    • remove  ${VENV}  (--purge)" )
$( [ "$WIPE_DOCKER" -eq 1 ] && echo "    • remove EVERY sm-* container/volume/network/image on this node (--wipe-docker)" )

  This does NOT touch:
    • NAS-backed project/app data
    • any WireGuard enrollment tunnel set up during install
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

if [ -f "$SUDOERS_FILE" ]; then
  $SUDO rm -f "$SUDOERS_FILE"
  ok "Removed ${SUDOERS_FILE}"
else
  warn "${SUDOERS_FILE} not present — skipping"
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
    info "Pulled/public images (Ollama, Stirling PDF, FreeCAD's base image, etc.) were"
    info "left in place — they don't carry an sm- prefix so they can't be safely told"
    info "apart from an unrelated use of the same tag elsewhere on this machine. Remove"
    info "them by hand with 'docker rmi <tag>' if you want that space back too."
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
    rm -rf "$VENV"
    ok "Removed ${VENV}"
  else
    warn "${VENV} not present — skipping"
  fi
else
  info "Keeping ${VENV} (pass --purge to remove)"
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
if [ "$WIPE_DOCKER" -eq 1 ]; then
  info "sm-* app containers/volumes/networks/images were wiped per --wipe-docker."
  info "NAS-backed project/app data was left in place."
else
  warn "App containers, images, and NAS data were left in place — pass"
  warn "--wipe-docker (or remove manually via docker) for a full teardown."
fi
blank
