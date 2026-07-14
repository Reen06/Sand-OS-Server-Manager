#!/bin/sh
# Nextcloud before-starting hook — runs every boot (after install/upgrade,
# before apache). Ensures environment-variable SSO is on so the SM proxy's
# trusted `Remote-User` header (mapped to SM_SSO_USER by sm-sso.conf) logs the
# user straight in and auto-provisions the account. user_saml is bundled in the
# image, so this is offline + idempotent.
#
# before-starting hooks run as www-data; post-installation runs as root. Handle
# both (this image has no gosu). Never abort startup on a hiccup (no set -e;
# always exit 0) — a misconfigured SSO should degrade to password login.
occ() {
  if [ "$(id -u)" = 0 ]; then
    runuser -u www-data -- php /var/www/html/occ "$@"
  else
    php /var/www/html/occ "$@"
  fi
}

occ app:enable user_saml

# `type` + these general toggles live in appconfig...
occ config:app:set user_saml type --value environment-variable
occ config:app:set user_saml general-require_provisioned_account --value 0
occ config:app:set user_saml general-allow_multiple_user_back_ends --value 1

# ...but the UID mapping lives in the provider CONFIGURATIONS table (env-var mode
# uses provider id 1), reached ONLY via saml:config:* — NOT `config:app:set`.
# Create provider 1 if missing (fresh install → first create is id 1), then map
# the UID to the SM_SSO_USER env var.
occ saml:config:get 1 >/dev/null 2>&1 || occ saml:config:create >/dev/null 2>&1
occ saml:config:set 1 --general-uid_mapping=SM_SSO_USER

echo "[sm] user_saml environment-variable SSO ensured (uid=SM_SSO_USER)"

# Run background jobs on a schedule (a host cron calls cron.php every 5 min)
# rather than piggy-backing on page loads (ajax) — reliable, and no per-page job
# overhead. Same jobs, just scheduled. Idempotent; persists in the DB.
occ background:cron

# ── Fleet NAS as External Storage ─────────────────────────────────────────────
# Expose each user's NAS home (/nas/users/$user) as "My Files" with per-access
# change detection so files that apps (FreeCAD…) write directly over NFS show up
# in Nextcloud. Shared folders are NOT a single blanket "/nas/shared" mount (that
# would show everything to everyone); the Fleet page's shared-folder manager
# creates one External Storage mount per folder with its own applicable-users.
# Idempotent: only create a mount if that mount point doesn't already exist.
occ app:enable files_external
_have_mount() { occ files_external:list 2>/dev/null | grep -q " $1 "; }

if ! _have_mount "/My Files"; then
  ID=$(occ files_external:create "My Files" local null::null -c datadir='/nas/users/$user' 2>/dev/null | grep -oE '[0-9]+' | tail -1)
  [ -n "$ID" ] && occ files_external:option "$ID" filesystem_check_changes 1
  echo "[sm] external storage 'My Files' -> /nas/users/\$user (id $ID)"
fi

# Retire any legacy blanket "Shared" mount (datadir exactly /nas/shared) so it
# doesn't override the per-folder shares the manager creates.
LEGACY=$(occ files_external:list --all --output=json 2>/dev/null \
  | php -r '$m=json_decode(file_get_contents("php://stdin"),true)?:[]; foreach($m as $x){ if(($x["configuration"]["datadir"]??"")==="/nas/shared"){ echo $x["mount_id"],"\n"; } }' 2>/dev/null)
for mid in $LEGACY; do
  occ files_external:delete -y "$mid" >/dev/null 2>&1 && echo "[sm] retired legacy blanket Shared mount (id $mid)"
done

# ── Collabora Online (richdocuments) — Docs/Sheets/Slides in-browser ──────────
# richdocuments is a stock Nextcloud-appstore app (not bundled offline like
# user_saml), so this needs outbound internet on first boot; harmless no-op on
# every later boot (occ app:install is idempotent, wopi_url set is a plain
# config write). Collabora itself is the "collabora" sidecar on this same
# private network (registry.py), reachable at its network alias — no published
# host port, so nothing outside this stack can reach it directly.
occ app:install richdocuments >/dev/null 2>&1 || true
occ app:enable richdocuments >/dev/null 2>&1 || true
occ config:app:set richdocuments wopi_url --value="http://collabora:9980"
# WOPI callbacks are container-to-container by hostname, not a real public
# domain — Nextcloud otherwise refuses to call back to a "local" address.
occ config:system:set allow_local_remote_servers --value=true --type=boolean
echo "[sm] richdocuments (Collabora Online) pointed at http://collabora:9980"

exit 0
