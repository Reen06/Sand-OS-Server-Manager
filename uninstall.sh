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
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --wipe-docker) WIPE_DOCKER=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help)
      echo "usage: sudo bash uninstall.sh [--purge] [--wipe-docker] [--yes]"
      echo "  --purge         also remove the Python venv (server/.venv)"
      echo "  --yes, -y       assume yes to every confirmation (scripted teardown)"
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
CLI_SHIM="/usr/local/bin/server-manager"

# This runs under sudo, so a bare ~ is root's home, not the account that owns
# the install — the per-node app library would be looked for in the wrong place
# and silently never found. Same resolution install.sh uses.
INVOKING_USER="${SUDO_USER:-$(whoami)}"
INVOKING_HOME="$(getent passwd "$INVOKING_USER" 2>/dev/null | cut -d: -f6)"
[ -n "$INVOKING_HOME" ] || INVOKING_HOME="/home/${INVOKING_USER}"
CATALOG_STATE_DIR="${INVOKING_HOME}/.sandos-sm"

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
    • remove  ${CLI_SHIM}
$( [ "$PURGE" -eq 1 ] && echo "    • remove  ${VENV}  (--purge)" )
$( [ "$PURGE" -eq 1 ] && echo "    • remove  ${CATALOG_STATE_DIR}  — this node's enabled-app list (--purge)" )
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
else
  info "Keeping ${VENV} (pass --purge to remove)"
  [ -d "$CATALOG_STATE_DIR" ] && \
    info "Keeping ${CATALOG_STATE_DIR} — this node's enabled-app list survives a reinstall"
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
