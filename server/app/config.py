"""Server Manager configuration (env-overridable)."""
import os
import shutil
import socket
import subprocess


def _detect_lan_ip() -> str:
    """This host's primary LAN IP (the egress interface), so a fleet of SM nodes
    each report their OWN address without hardcoding. No packets are sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


def _detect_gpu() -> bool:
    """Whether this node can run GPU/streamed apps (NVIDIA present)."""
    if shutil.which("nvidia-smi"):
        try:
            return subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                                  timeout=5).returncode == 0
        except Exception:  # noqa: BLE001
            return False
    return os.path.exists("/proc/driver/nvidia")


# This host's LAN IP — used to point each instance's internal TURN at a reachable
# address so other LAN devices can connect. Auto-detected per node (fleet-ready);
# override with SM_LAN_IP.
LAN_IP = os.environ.get("SM_LAN_IP") or _detect_lan_ip()

# Whether this node advertises GPU capability (streamed apps like FreeCAD). Auto-
# detected; override with SM_GPU=true/false.
_gpu_env = os.environ.get("SM_GPU")
HAS_GPU = (_gpu_env.lower() in ("1", "true", "yes")) if _gpu_env else _detect_gpu()

# ── Fleet NAS (network storage) ────────────────────────────────────────────────
# The NAS node's reachable address — the NFSv4 server that holds every user's
# files. All fleet nodes point here so an app mounts the SAME user files no
# matter which node it runs on (no duplication). Defaults to THIS node (single-
# box / self-NAS); on other app nodes set SM_NAS_HOST to the NAS's IP (or its
# WireGuard IP for an off-LAN NAS — NFSv4 is single-port and tunnels cleanly).
NAS_HOST = os.environ.get("SM_NAS_HOST", LAN_IP)
NAS_ENABLED = os.environ.get("SM_NAS_ENABLED", "true").lower() in ("1", "true", "yes")
# Sub-paths under the NFS export root (server exports .../sandos-nas as the v4 root).
NAS_USERS_SUBPATH = os.environ.get("SM_NAS_USERS_SUBPATH", "users")     # users/{username}
NAS_SHARED_SUBPATH = os.environ.get("SM_NAS_SHARED_SUBPATH", "shared")  # shared/{name}
# LOCAL path of the export root on the NAS host — the SM runs there (control-owned
# tree) so the shared-folder manager creates/removes folders directly. Matches
# NAS_ROOT in containers/nfs-server/run-nas.sh. anon uid/gid = the all_squash owner
# every app maps to, so a new shared folder is owned consistently across apps.
NAS_ROOT = os.environ.get("SM_NAS_ROOT", "/home/control/sandos-nas")
NAS_UID = int(os.environ.get("SM_NAS_UID", "1000"))
NAS_GID = int(os.environ.get("SM_NAS_GID", "1000"))
# The shared Nextcloud instance's container name — the manager drives its External
# Storage (per-folder mounts + applicable-users) via `occ`.
NC_CONTAINER = os.environ.get("SM_NC_CONTAINER", "sm-nextcloud")

# Human-friendly node name the Hub shows in the fleet list (defaults to hostname).
NODE_NAME = os.environ.get("SM_NODE_NAME") or socket.gethostname()

# Server Manager version (bumped as the fleet protocol evolves).
SM_VERSION = "0.2.0"

# The systemd unit name for THIS node's Server Manager — what the Fleet page's
# per-node "Restart Server Manager" button actually restarts (main.py's
# /api/sm/restart). Override if a node names its unit differently.
SM_SYSTEMD_UNIT = os.environ.get("SM_SYSTEMD_UNIT", "sandos-server-manager")

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

# WebCAD/CAM — the browser CAD/CAM app (a 'web' app). Dev image runs the app live
# from a bind-mounted source tree; build from containers/webcad.
WEBCAD_IMAGE = os.environ.get("SM_WEBCAD_IMAGE", "sm-webcad:dev")

# HeliX Motion — CNC controller for the Carvera Air (a 'web' app). Dev image runs
# live from the bind-mounted /home/control/CNC_Controller tree; build from its
# containers/helix. SCAN_SUBNETS: comma-separated CIDRs the app's machine
# discovery TCP-sweeps (:2222) — the hub's home LAN + any gateway-node LANs
# reachable over WireGuard (UDP broadcast can't cross the docker bridge).
HELIX_IMAGE = os.environ.get("SM_HELIX_IMAGE", "sm-helix:dev")
# Default the scan to this host's own /24: the SM host sits on the same LAN as
# the Carvera, but the app's UDP :3333 discovery can't cross the docker bridge,
# so the TCP sweep is how the container finds the machine. Env still overrides
# (e.g. to add gateway-node subnets: "10.0.0.0/24,192.168.50.0/24").
HELIX_SCAN_SUBNETS = os.environ.get(
    "SM_HELIX_SCAN_SUBNETS",
    ".".join(_detect_lan_ip().split(".")[:3]) + ".0/24",
)

# OpenMapper — browser-native lighting/projection controller. Build from
# /home/control/OpenMapper/app/Dockerfile.
OPENMAPPER_IMAGE = os.environ.get("SM_OPENMAPPER_IMAGE", "sm-openmapper:dev")

# Ray Optics — 2D geometric optics simulator (a 'web' app). Static build served
# by nginx; build from containers/rayoptics.
RAYOPTICS_IMAGE = os.environ.get("SM_RAYOPTICS_IMAGE", "sm-rayoptics:latest")

# Stirling-PDF — FOSS PDF toolkit (merge/split/convert/OCR/etc.), a 'web' app.
# Official upstream image, no build needed.
STIRLINGPDF_IMAGE = os.environ.get("SM_STIRLINGPDF_IMAGE", "stirlingtools/stirling-pdf:latest")

# Renode — real open-source microcontroller simulator (Wokwi itself is closed-
# source SaaS and not self-hostable). Exposed as a browser terminal onto its
# monitor console via ttyd; build from containers/renode-web.
RENODE_IMAGE = os.environ.get("SM_RENODE_IMAGE", "sm-renode-web:latest")

# OnlyOffice Document Server — catalogued as an alternative to Collabora, but
# NOT deployed by default (needs ~4GB RAM headroom the box may not have —
# nothing is pulled/built until someone actually presses Start on it).
ONLYOFFICE_IMAGE = os.environ.get("SM_ONLYOFFICE_IMAGE", "onlyoffice/documentserver:latest")
ONLYOFFICE_POSTGRES_IMAGE = os.environ.get("SM_OO_POSTGRES_IMAGE", "postgres:15-alpine")
ONLYOFFICE_RABBITMQ_IMAGE = os.environ.get("SM_OO_RABBITMQ_IMAGE", "rabbitmq:3-alpine")
ONLYOFFICE_REDIS_IMAGE = os.environ.get("SM_OO_REDIS_IMAGE", "redis:7-alpine")
ONLYOFFICE_DB_PASSWORD = os.environ.get("SM_OO_DB_PASSWORD", "oo-db-pass")
ONLYOFFICE_JWT_SECRET = os.environ.get("SM_OO_JWT_SECRET", "change-me-oo-jwt-secret")

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

# Collabora Online — Nextcloud's real-time Docs/Sheets/Slides editor, run as a
# sidecar Service of the nextcloud AppDef (never its own app card — see
# registry.py). It has no published host port (same as the db/redis sidecars);
# Nextcloud's richdocuments app reaches it container-to-container at
# http://collabora:9980 (wired up in containers/nextcloud/20-sm-saml.sh).
# "domain" is the WOPI-host allowlist regex — matches Nextcloud's own trusted
# domains so the only caller (Nextcloud itself) is accepted.
COLLABORA_IMAGE = os.environ.get("SM_COLLABORA_IMAGE", "collabora/code:latest")
COLLABORA_DOMAIN_REGEX = os.environ.get(
    "SM_COLLABORA_DOMAIN_REGEX",
    "|".join(h.replace(".", r"\.") for h in (NC_TRUSTED_DOMAINS.split() + ["nextcloud"]) if h))

# Per-instance port allocation. Each instance gets a slot; from the slot we derive
# a unique web port, TURN port, and a small UDP relay range so concurrent
# instances never collide.
SLOT_COUNT = int(os.environ.get("SM_SLOT_COUNT", "16"))
WEB_PORT_BASE = int(os.environ.get("SM_WEB_PORT_BASE", "8100"))   # 8100, 8101, ...
TURN_PORT_BASE = int(os.environ.get("SM_TURN_PORT_BASE", "13478")) # 13478, 13479, ...
RELAY_BASE = int(os.environ.get("SM_RELAY_BASE", "40000"))
# 8 ports/slot was too tight: coturn only frees a stale TURN allocation on its
# periodic watchdog sweep, not the instant a client disconnects, so a burst of
# WebRTC reconnect attempts (e.g. during a rocky first negotiation) can each
# grab a couple of relay ports faster than old ones get reclaimed — the pool
# empties, every further attempt then fails outright with coturn's own
# "no available ports" / error 508 "Cannot create socket", and the client's
# retry loop just keeps failing until enough allocations time out on their
# own. 32 gives real headroom to absorb that burst instead of amplifying it.
RELAY_PER_SLOT = int(os.environ.get("SM_RELAY_PER_SLOT", "32"))
# Extra TURN host injected alongside the LAN IP in the /turn ICE-server response.
# Set to the WireGuard IP (e.g. 10.79.114.1) so VPN/mobile clients — who can't
# reach the LAN IP directly — also get a TURN candidate they can use.
# The TURN port is published on 0.0.0.0 so it already listens on the WG interface.
TURN_EXTRA_HOST = os.environ.get("SM_TURN_EXTRA_HOST", "")

# Instance basic-auth (internal; the Hub proxy will own real auth later).
INSTANCE_USER = os.environ.get("SM_INSTANCE_USER", "user")
INSTANCE_PASSWD = os.environ.get("SM_INSTANCE_PASSWD", "freecad")

# ── Hub SSO ───────────────────────────────────────────────────────────────────
# When SM_HUB_URL is set, the Server Manager trusts the Hub's session: it reads
# the hub_session cookie and validates it against {HUB_URL}/api/auth/me, using the
# returned username as the identity (per-user instances key off it). Empty =
# standalone dev mode (an anonymous 'sm_user' cookie).
HUB_URL = os.environ.get("SM_HUB_URL", "").rstrip("/")
# Where the SM reaches the Hub for the *internal* per-request session check
# (/api/auth/me). Defaults to HUB_URL — the public HTTPS address — which works for
# every node, including an off-LAN node reaching the Hub over its WireGuard tunnel.
# A fully-trusted co-located node MAY point this at a plain-HTTP LAN address to
# skip the TLS handshake on cache-miss auth calls — but note the hub_session token
# then travels the LAN in cleartext, so only set it on a network you trust and
# where the Hub actually exposes that HTTP port. Per-node: set SM_HUB_INTERNAL_URL
# only on the nodes where you want it; leave unset everywhere else (secure default).
HUB_INTERNAL_URL = os.environ.get("SM_HUB_INTERNAL_URL", "").rstrip("/") or HUB_URL
HUB_SESSION_COOKIE = os.environ.get("SM_HUB_SESSION_COOKIE", "hub_session")
# Verify the Hub's TLS cert. Off by default for LAN-internal calls to the Hub's
# Caddy 'internal' CA; set true once the SM trusts the Hub CA / uses the real cert.
HUB_VERIFY_TLS = os.environ.get("SM_HUB_VERIFY_TLS", "false").lower() == "true"
# Where to send unauthenticated users to log in (the Hub dashboard).
HUB_LOGIN_URL = os.environ.get("SM_HUB_LOGIN_URL", HUB_URL or "")
