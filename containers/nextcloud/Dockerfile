# Server-Manager Nextcloud — the flagship NAS/cloud app (a 'web' app with a
# MariaDB + Redis stack). Thin wrapper over the official image adding just:
#   1. sm-sso.conf        — map the SM-proxy Remote-User header → REMOTE_USER
#   2. bundled user_saml   — so SSO setup needs no runtime appstore fetch
#   3. a before-starting hook that enables + configures env-variable SSO
# Everything else (DB/Redis hosts, OVERWRITE* subpath, data volume) is supplied
# by the Server Manager's App Definition at runtime.
FROM nextcloud:stable-apache

COPY sm-sso.conf /etc/apache2/conf-enabled/sm-sso.conf

# Bundle user_saml into the image's source tree so it lands in custom_apps on
# install with NO appstore download (the runtime fetch flaked on first boot).
COPY apps/user_saml /usr/src/nextcloud/custom_apps/user_saml

# Runs on every boot (after install/upgrade, before apache) — idempotent, so SSO
# is guaranteed configured even if a one-time hook had hiccuped.
COPY 20-sm-saml.sh /docker-entrypoint-hooks.d/before-starting/20-sm-saml.sh
RUN chmod +x /docker-entrypoint-hooks.d/before-starting/20-sm-saml.sh \
    && chown -R www-data:www-data /usr/src/nextcloud/custom_apps/user_saml
