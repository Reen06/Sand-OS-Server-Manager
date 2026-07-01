#!/bin/sh
# Server-Manager Filebrowser entrypoint.
#
# The Hub session is the real gate, so Filebrowser runs *noauth* (no login
# prompt) behind the session-gated SM proxy. noauth needs a one-time DB setup:
# init the DB, switch the auther to noauth, and create the single implicit user
# (its password is never used, but must clear the 12-char minimum). The DB lives
# in /tmp so it re-inits cleanly on every start — there's no per-user Filebrowser
# state worth persisting; the *files* persist in the mounted volumes.
#
# FB_BASEURL is injected by the SM (= {EXTERNAL_BASE}/stream/filebrowser) so the
# SPA's asset/API URLs resolve under the proxy subpath.
set -e

DB=/tmp/filebrowser.db
BASE="${FB_BASEURL:-}"
PORT="${FB_PORT:-8080}"
ROOT="${FB_ROOT:-/srv}"

filebrowser config init -d "$DB" >/dev/null
filebrowser config set -d "$DB" \
  --auth.method=noauth \
  --baseurl "$BASE" \
  --root "$ROOT" \
  --branding.name "${FB_BRAND:-Files}" >/dev/null
filebrowser users add admin sm-noauth-placeholder --perm.admin -d "$DB" >/dev/null 2>&1 || true

# exec so signals reach filebrowser directly (clean container stop).
exec filebrowser -d "$DB" -a 0.0.0.0 -p "$PORT" -r "$ROOT" -b "$BASE"
