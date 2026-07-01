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
exit 0
