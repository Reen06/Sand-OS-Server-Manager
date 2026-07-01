"""Server Manager configuration (env-overridable)."""
import os

# This host's LAN IP — used to point each instance's internal TURN at a reachable
# address so other LAN devices can connect. (Behind the Hub TLS proxy later this
# becomes the Hub hostname.)
LAN_IP = os.environ.get("SM_LAN_IP", "10.0.0.164")

# Where the Server Manager UI/API itself listens.
SM_HOST = os.environ.get("SM_HOST", "0.0.0.0")
SM_PORT = int(os.environ.get("SM_PORT", "8170"))

# The external path the Hub (Caddy) mounts the Server Manager under. Web apps are
# served at {EXTERNAL_BASE}/stream/{app}/… in the browser; the proxy prepends this
# base when talking upstream so an app's baseURL/asset links line up. (Streamed
# Selkies apps don't need it — their client uses path-relative URLs.)
EXTERNAL_BASE = os.environ.get("SM_EXTERNAL_BASE", "/apps").rstrip("/")

# Default image for the FreeCAD app.
FREECAD_IMAGE = os.environ.get("SM_FREECAD_IMAGE", "freecad-streamer:dev")

# Filebrowser — the NAS file UI (a 'web' app). Thin wrapper image that provisions
# noauth on boot; build from containers/filebrowser.
FILEBROWSER_IMAGE = os.environ.get("SM_FILEBROWSER_IMAGE", "sm-filebrowser:latest")

# Nextcloud — the flagship cloud/NAS app (a 'web' app + MariaDB/Redis stack).
NEXTCLOUD_IMAGE = os.environ.get("SM_NEXTCLOUD_IMAGE", "sm-nextcloud:latest")
MARIADB_IMAGE = os.environ.get("SM_MARIADB_IMAGE", "mariadb:11")
REDIS_IMAGE = os.environ.get("SM_REDIS_IMAGE", "redis:7-alpine")
# Internal DB credentials. The DB is never published to the host (sidecar on the
# app's private network), but these are still overridable.
NC_DB_NAME = os.environ.get("SM_NC_DB_NAME", "nextcloud")
NC_DB_USER = os.environ.get("SM_NC_DB_USER", "nextcloud")
NC_DB_PASSWORD = os.environ.get("SM_NC_DB_PASSWORD", "nc-db-pass")
NC_DB_ROOT_PASSWORD = os.environ.get("SM_NC_DB_ROOT_PASSWORD", "nc-db-root")
# Nextcloud's initial admin account (SSO users are auto-provisioned separately).
NC_ADMIN_USER = os.environ.get("SM_NC_ADMIN_USER", "admin")
NC_ADMIN_PASSWORD = os.environ.get("SM_NC_ADMIN_PASSWORD", "change-me-admin-1234")
# Hostnames the Hub is reached at (for Nextcloud's trusted_domains). Space-list.
NC_TRUSTED_DOMAINS = os.environ.get(
    "SM_NC_TRUSTED_DOMAINS", "127.0.0.1 localhost 10.0.0.177 vpn1603.duckdns.org")
# Force the public host in generated URLs. Empty = derive from the (forwarded)
# Host header — works once Caddy forwards Host on the /apps route. Set to your
# Hub host (e.g. vpn1603.duckdns.org) if login redirects point at the wrong host.
NC_OVERWRITE_HOST = os.environ.get("SM_NC_OVERWRITE_HOST", "")

# Per-instance port allocation. Each instance gets a slot; from the slot we derive
# a unique web port, TURN port, and a small UDP relay range so concurrent
# instances never collide.
SLOT_COUNT = int(os.environ.get("SM_SLOT_COUNT", "16"))
WEB_PORT_BASE = int(os.environ.get("SM_WEB_PORT_BASE", "8100"))   # 8100, 8101, ...
TURN_PORT_BASE = int(os.environ.get("SM_TURN_PORT_BASE", "13478")) # 13478, 13479, ...
RELAY_BASE = int(os.environ.get("SM_RELAY_BASE", "40000"))         # 8 ports per slot
RELAY_PER_SLOT = 8

# Instance basic-auth (internal; the Hub proxy will own real auth later).
INSTANCE_USER = os.environ.get("SM_INSTANCE_USER", "user")
INSTANCE_PASSWD = os.environ.get("SM_INSTANCE_PASSWD", "freecad")

# ── Hub SSO ───────────────────────────────────────────────────────────────────
# When SM_HUB_URL is set, the Server Manager trusts the Hub's session: it reads
# the hub_session cookie and validates it against {HUB_URL}/api/auth/me, using the
# returned username as the identity (per-user instances key off it). Empty =
# standalone dev mode (an anonymous 'sm_user' cookie).
HUB_URL = os.environ.get("SM_HUB_URL", "").rstrip("/")
HUB_SESSION_COOKIE = os.environ.get("SM_HUB_SESSION_COOKIE", "hub_session")
# Verify the Hub's TLS cert. Off by default for LAN-internal calls to the Hub's
# Caddy 'internal' CA; set true once the SM trusts the Hub CA / uses the real cert.
HUB_VERIFY_TLS = os.environ.get("SM_HUB_VERIFY_TLS", "false").lower() == "true"
# Where to send unauthenticated users to log in (the Hub dashboard).
HUB_LOGIN_URL = os.environ.get("SM_HUB_LOGIN_URL", HUB_URL or "")
