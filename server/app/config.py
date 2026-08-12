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
# shares/{slug} — people-facing shared folders (nas_shares.py). Deliberately NOT
# the same tree as shared/, which holds app data (media, open-webui-data …): one
# is somewhere a person files things, the other is machinery.
NAS_SHARES_SUBPATH = os.environ.get("SM_NAS_SHARES_SUBPATH", "shares")
# .views/{user} — an empty per-user directory that becomes a file manager's ROOT,
# with their home and each of their shares mounted inside it. It exists purely so
# the root is ON the NAS: the upstream Filebrowser image declares VOLUME /srv, so
# an unbound root got an anonymous local-disk volume and the app reported the
# NODE's free space until you stepped into a subfolder. Hidden — nothing files here.
NAS_VIEWS_SUBPATH = os.environ.get("SM_NAS_VIEWS_SUBPATH", ".views")
# LOCAL path of the export root on the NAS host — the SM runs there (control-owned
# tree) so the shared-folder manager creates/removes folders directly. Matches
# NAS_ROOT in containers/nfs-server/run-nas.sh. anon uid/gid = the all_squash owner
# every app maps to, so a new shared folder is owned consistently across apps.
NAS_ROOT = os.environ.get("SM_NAS_ROOT", "/home/control/sandos-nas")
# Where the mesh NAS (SeaweedFS) is mounted. Since the mesh migration this — not
# NAS_ROOT — is where user files actually live; NAS_ROOT is now only the legacy
# single-server tree and this node's own state files.
NAS_MESH_MOUNT = os.environ.get("SM_MESH_NAS_MOUNT", "/mnt/sandos-nas")


def mesh_mounted() -> bool:
    """Is the mesh NAS actually mounted here?

    Checked rather than assumed: a node whose mount is down must fall back to
    the legacy tree instead of reading an empty directory, which would present
    every app with a NAS that looks intact and contains nothing.

    Emptiness is NOT the test, though it used to be. A brokered node mounts
    only its own staging directory, which is legitimately empty until somebody
    stages a file — treating that as "not mounted" sent it down the retired NFS
    path and made an app refuse to start on a node whose mount was perfectly
    healthy. `ismount` already distinguishes a live mount from a plain local
    directory, and a mount whose process died announces itself by raising on
    read, so both real failures are still caught.
    """
    if not os.path.ismount(NAS_MESH_MOUNT):
        return False
    try:
        os.listdir(NAS_MESH_MOUNT)
    except OSError:
        # A FUSE mount whose process died without unmounting: still a
        # mountpoint, but every read fails (ENOTCONN). That is the case worth
        # catching, and it announces itself by raising.
        return False
    return True


def nas_data_root() -> str:
    """Root for USER DATA — homes, shared folders, anything a person filed.

    NOT for this node's own state files (.usb-state.json and friends). Those are
    node-local by definition and must stay on NAS_ROOT: moving them to the mesh
    would have every node fight over one file, and orphan the existing ones.

    This exists because the mesh migration (retiring the single-server NAS) left
    several callers still composing paths from NAS_ROOT. They kept working in the
    sense that they returned a path and created it on demand — the Save/Open
    picker silently showed an EMPTY home while the user's real files sat on the
    mesh, which is worse than an error.
    """
    return NAS_MESH_MOUNT if mesh_mounted() else NAS_ROOT
NAS_UID = int(os.environ.get("SM_NAS_UID", "1000"))
NAS_GID = int(os.environ.get("SM_NAS_GID", "1000"))
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

# The port THIS node's own sshd listens on — reported to the Hub via
# /api/sm/ssh/authorize so peer-installs and auto-update connect to the
# right place. 22 on every normal Linux install. On WSL, Windows' own
# OpenSSH Server (not WSL's) answers port 22 on the LAN IP and only knows
# Windows accounts, so install.sh's WSL branch stands up a separate sshd
# inside WSL on an alternate port and sets SM_SSH_PORT to match.
SSH_PORT = int(os.environ.get("SM_SSH_PORT", "22"))

# The external path the Hub (Caddy) mounts the Server Manager under. Web apps are
# served at {EXTERNAL_BASE}/stream/{app}/… in the browser; the proxy prepends this
# base when talking upstream so an app's baseURL/asset links line up. (Streamed
# Selkies apps don't need it — their client uses path-relative URLs.)
EXTERNAL_BASE = os.environ.get("SM_EXTERNAL_BASE", "/apps").rstrip("/")

# Default image for the FreeCAD app.
FREECAD_IMAGE = os.environ.get("SM_FREECAD_IMAGE", "freecad-streamer:dev")
# Isaac Sim wrapped in the Selkies desktop (containers/isaac-sim/Dockerfile).
# Isaac's own livestream needs a native client and a direct UDP peer path, so
# it can never be a browser app; this is the streamed-desktop equivalent.
# ONE image backs both Isaac Sim and Isaac Lab. isaac-lab-base is Isaac Sim
# plus Lab -- Lab pip-installs into /isaac-sim's own python and adds a 16MB
# workspace -- so carrying both costs almost nothing over Sim alone, and
# SM_ISAAC_APP decides which one a container opens.
ISAAC_IMAGE = os.environ.get("SM_ISAAC_IMAGE", "sm-isaac:5.1.0")

# Filebrowser — the NAS file UI (a 'web' app). Thin wrapper image that provisions
# noauth on boot; build from containers/filebrowser.
FILEBROWSER_IMAGE = os.environ.get("SM_FILEBROWSER_IMAGE", "sm-filebrowser:latest")

# WebCAD/CAM — the browser CAD/CAM app (a 'web' app). Dev image runs the app live
# from a bind-mounted source tree; build from containers/webcad.
WEBCAD_IMAGE = os.environ.get("SM_WEBCAD_IMAGE", "sm-webcad:dev")
# Self-contained build (source COPYed + built in, no bind mount needed) — built
# from containers/webcad/Dockerfile.packaged, repo root as context. Used
# automatically on any node that doesn't have the live checkout.
WEBCAD_PACKAGED_IMAGE = os.environ.get("SM_WEBCAD_PACKAGED_IMAGE", "sm-webcad:packaged")

# HeliX Motion — CNC controller for the Carvera Air (a 'web' app). Dev image runs
# live from the bind-mounted /home/control/CNC_Controller tree; build from its
# containers/helix. SCAN_SUBNETS: comma-separated CIDRs the app's machine
# discovery TCP-sweeps (:2222) — the hub's home LAN + any gateway-node LANs
# reachable over WireGuard (UDP broadcast can't cross the docker bridge).
HELIX_IMAGE = os.environ.get("SM_HELIX_IMAGE", "sm-helix:dev")
# Self-contained build — same idea as WEBCAD_PACKAGED_IMAGE, built from
# containers/helix/Dockerfile.packaged, repo root as context.
HELIX_PACKAGED_IMAGE = os.environ.get("SM_HELIX_PACKAGED_IMAGE", "sm-helix:packaged")
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

# EngineeringPaper.xyz — browser math-sheet editor (client-side Pyodide/SymPy),
# a 'web' app. Static nginx build; build context is a local checkout of
# github.com/mgreminger/EngineeringPaper.xyz, Dockerfile at
# containers/engineeringpaper. Small — lives on local disk like most apps.
ENGINEERINGPAPER_IMAGE = os.environ.get("SM_ENGINEERINGPAPER_IMAGE", "sm-engineeringpaper:latest")

# OpenFOAM GUI — homegrown FastAPI case-manager UI for OpenFOAM (a 'web' app).
# Built FROM the official opencfd/openfoam-run:2506 image (a full CFD toolchain,
# several GB) + the app code from /home/control/OpenFOAM_GUI layered on top;
# build context/Dockerfile at containers/openfoam-gui. Large — built directly
# against the USB app-hosting drive's own dockerd, never touches local disk.
OPENFOAM_GUI_IMAGE = os.environ.get("SM_OPENFOAM_GUI_IMAGE", "sm-openfoam-gui:latest")
# Self-contained build (source COPYed + deps installed at image-build time) —
# from Dockerfile.packaged at the OpenFOAM_GUI repo root. Same idea as
# WEBCAD_PACKAGED_IMAGE; builds/pushes against the app's active daemon (the
# USB drive's dockerd here — the multi-GB base never touches local disk).
OPENFOAM_GUI_PACKAGED_IMAGE = os.environ.get("SM_OPENFOAM_GUI_PACKAGED_IMAGE", "sm-openfoam-gui:packaged")

# ParaView (ParaViewWeb) — browser-based scientific visualizer, a 'web' app.
# Official Kitware image (osmesa = software rendering, no GPU needed). This
# repo hasn't been updated in years; the newer "pvw-v5.7.0-rc2-osmesa" tag
# was confirmed BROKEN live (missing /opt/launcher/config-template.json,
# entrypoint crashes on start) — "pvw-visualizer-osmesa-5.5.0" is the one
# confirmed working end-to-end (serves the real Visualizer app at
# /visualizer/, not just Apache's default page).
#
# Thin custom layer (containers/paraview/Dockerfile) on top of the upstream
# image: patches Apache's ProxyPass for the wslink launcher to add
# `retry=0`. Without it, Apache's mod_proxy circuit breaker disables that
# backend for a 60s cooldown after the FIRST failed connection (a real race
# on container startup — Apache comes up before the launcher's own socket
# does) — every request in that window 503s immediately with no further
# connection attempt, regardless of the launcher being back up seconds
# later. Confirmed live 2026-07-16: `AH00940: HTTP: disabled connection for
# (localhost)` in the container's own error log, and a real POST 503ing for
# 40+ straight seconds. The backend is a same-container sibling process,
# not a flaky remote service, so the retry-delay protects against nothing
# real here.
PARAVIEW_IMAGE = os.environ.get("SM_PARAVIEW_IMAGE", "sandos-paraview:latest")

# ParaView DESKTOP — the full Qt application streamed over WebRTC, separate from
# the ParaViewWeb app above. Two different programs, not two skins: ParaViewWeb
# is a fixed browser visualizer rendering with OSMesa on the CPU; this is the
# real client with the full filter library, the Python shell, plugins and GPU
# rendering. Built locally (containers/paraview-desktop) — ~700MB of Kitware
# tarball on top of the Selkies desktop base, so there is no useful upstream
# image to pull.
PARAVIEW_DESKTOP_IMAGE = os.environ.get(
    "SM_PARAVIEW_DESKTOP_IMAGE", "sandos-paraview-desktop:latest")

# Forgejo — git forge + OCI container registry. Upstream image, pulled not built:
# there is nothing to fork here, and pinning the major track means security
# updates arrive without a rebuild.
FORGEJO_IMAGE = os.environ.get("SM_FORGEJO_IMAGE", "codeberg.org/forgejo/forgejo:12")

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

# Ollama — local LLM runner (OpenAI-compatible REST API at :11434).
OLLAMA_IMAGE = os.environ.get("SM_OLLAMA_IMAGE", "ollama/ollama:latest")

# Open WebUI — browser chat UI for Ollama (SSO via X-Forwarded-User header).
# Thin custom layer (containers/open-webui/Dockerfile) on the upstream image:
# patches the code-interpreter's sandboxed iframe to add allow-same-origin.
# Without it, running a code block fails with "loadPyodide is not defined"
# (desktop) / an explicit CORS-policy script-load denial (mobile Safari) —
# confirmed live 2026-07-16, not a proxy/CORS-header issue on our side (the
# pyodide.js asset itself already serves with Access-Control-Allow-Origin: *
# and loads fine directly; the failure is the sandboxed iframe's own
# opaque-origin restriction on loading it). See the Dockerfile's own
# comment for the accepted security tradeoff and why this is matched by
# string pattern rather than the (content-hashed, unstable across rebuilds)
# filename.
OPEN_WEBUI_IMAGE = os.environ.get("SM_OPEN_WEBUI_IMAGE", "sandos-open-webui:latest")

# ComfyUI — built locally from containers/comfyui (see its Dockerfile for why
# it starts from PyTorch's CUDA image rather than installing torch from PyPI).
COMFYUI_IMAGE = os.environ.get("SM_COMFYUI_IMAGE", "sandos-comfyui:latest")

# Hugging Face access token, for pulling models whose repo is GATED -- one that
# requires a licence to have been accepted on the account. Those cannot be
# fetched anonymously at all, by any tool, which is why a gated model shows up
# as "here is a link, go and get it yourself" instead of downloading. Empty by
# default and read from the node's env file, so no token is ever committed and
# a node without one simply keeps the anonymous behaviour.
HF_TOKEN = os.environ.get("SM_HF_TOKEN", "").strip()
# Secret used to sign Open WebUI JWT sessions — generate once, keep stable.
OPEN_WEBUI_SECRET_KEY = os.environ.get("SM_OPEN_WEBUI_SECRET_KEY", "change-me-owui-secret")

# NAS mount path for Ollama model transfers between nodes. Set this on each SM
# node to where the shared NAS is mounted (e.g. /mnt/nas). Empty = NAS
# transfer unavailable on this node.
OLLAMA_NAS_TRANSFER_PATH = os.environ.get("SM_OLLAMA_NAS_TRANSFER", "")

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

def _with_scheme(url: str) -> str:
    """A bare IP/hostname with no http(s):// scheme (an easy slip when typing
    just an address) can't be turned into a request later — urllib refuses it
    outright, and that failure surfaces as a confusing generic 500 deep in
    an unrelated request handler, not a clear error at startup. Assume https
    (what every other URL in this project already defaults to) rather than
    let a malformed value reach the rest of the app."""
    url = url.rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


# ── Hub SSO ───────────────────────────────────────────────────────────────────
# When SM_HUB_URL is set, the Server Manager trusts the Hub's session: it reads
# the hub_session cookie and validates it against {HUB_URL}/api/auth/me, using the
# returned username as the identity (per-user instances key off it). Empty =
# standalone dev mode (an anonymous 'sm_user' cookie).
HUB_URL = _with_scheme(os.environ.get("SM_HUB_URL", ""))
# Where the SM reaches the Hub for the *internal* per-request session check
# (/api/auth/me). Defaults to HUB_URL — the public HTTPS address — which works for
# every node, including an off-LAN node reaching the Hub over its WireGuard tunnel.
# A fully-trusted co-located node MAY point this at a plain-HTTP LAN address to
# skip the TLS handshake on cache-miss auth calls — but note the hub_session token
# then travels the LAN in cleartext, so only set it on a network you trust and
# where the Hub actually exposes that HTTP port. Per-node: set SM_HUB_INTERNAL_URL
# only on the nodes where you want it; leave unset everywhere else (secure default).
HUB_INTERNAL_URL = _with_scheme(os.environ.get("SM_HUB_INTERNAL_URL", "")) or HUB_URL
HUB_SESSION_COOKIE = os.environ.get("SM_HUB_SESSION_COOKIE", "hub_session")
# Verify the Hub's TLS cert. Off by default for LAN-internal calls to the Hub's
# Caddy 'internal' CA; set true once the SM trusts the Hub CA / uses the real cert.
HUB_VERIFY_TLS = os.environ.get("SM_HUB_VERIFY_TLS", "false").lower() == "true"
# Where to send unauthenticated users to log in (the Hub dashboard).
HUB_LOGIN_URL = os.environ.get("SM_HUB_LOGIN_URL", HUB_URL or "")

# The address people actually type. HUB_URL is how this NODE reaches the Hub —
# usually a LAN IP — which is not necessarily how a browser gets there, and some
# apps care about the difference. Forgejo builds its canonical URL from this and
# warns on every page when the browser's address does not match it.
#
# One value, because an app can only have one canonical URL: reaching the same
# app by a second name still warns. Split-horizon DNS (the same hostname
# resolving to the LAN address at home) is the way to make one name correct
# everywhere, rather than trying to make an app answer to several.
PUBLIC_BASE_URL = _with_scheme(os.environ.get("SM_PUBLIC_BASE_URL", "")) or HUB_URL

# Apps to auto-launch on SM startup (comma-separated ids). OPT-IN, empty by
# default — nothing starts on boot unless the operator explicitly lists apps
# via SM_AUTOSTART_APPS. Every app is on-demand: it launches when opened. This
# is only for someone who deliberately wants a specific app always-on.
AUTOSTART_APPS = [a.strip() for a in
                  os.environ.get("SM_AUTOSTART_APPS", "").split(",")
                  if a.strip()]

# ── Hub LLM Router ────────────────────────────────────────────────────────────
# The Hub's OpenAI-compatible routing endpoint picks the best fleet node per
# request. When the key is set, Open WebUI is seeded with the router as its
# OpenAI connection (OPENAI_API_BASE_URL/KEY env) so every fleet Ollama is
# reachable through ONE connection — no per-node Connections edits.
# The key value must match the Hub's `llm_api_key` setting (hub.db settings).
LLM_API_KEY = os.environ.get("SM_LLM_API_KEY", "")
# Hostname of HUB_URL + IP of HUB_INTERNAL_URL, for a container --add-host that
# lets Open WebUI resolve the Hub's public name to its LAN IP (correct TLS cert,
# no WireGuard hop) even when this node has no route to the Hub's WG address.
from urllib.parse import urlparse as _urlparse
HUB_HOST = _urlparse(HUB_URL).hostname or "" if HUB_URL else ""
HUB_INTERNAL_IP = _urlparse(HUB_INTERNAL_URL).hostname or "" if HUB_INTERNAL_URL else ""

# The hostname on the Hub's PUBLICLY-TRUSTED certificate, when that differs from
# however HUB_URL happens to be written.
#
# These two want opposite things and cannot be served by one value:
#
#   * HUB_URL is browser-facing — it is where a person gets redirected to log
#     in. On a LAN-only setup that has to be the address they can actually
#     reach, i.e. the bare IP.
#   * A container verifying TLS needs a NAME. The Hub answers its IP with a
#     certificate from its own internal CA, which nothing outside the Hub
#     trusts (curl exit 60); asked for its real hostname it presents a genuine
#     publicly-trusted one.
#
# Pointing HUB_URL at the name to satisfy the second breaks the first twice
# over: that name resolves to the PUBLIC address, and the Hub deliberately 404s
# anything arriving from the public internet (see the mesh-only note in
# main.py's NAS policy sync). Confirmed live: Open WebUI listed no models at
# all, its own log showing repeated 404s against the public hostname.
#
# So name the certificate host separately. The --add-host below pins it to the
# LAN IP, which gives a container both halves at once — a name TLS can verify,
# resolved to an address the Hub actually answers on — while HUB_URL stays on
# whatever people reach the dashboard by. Leave unset when the Hub is already
# configured by a name that resolves correctly; then HUB_HOST already is it.
HUB_PUBLIC_HOST = os.environ.get("SM_HUB_PUBLIC_HOST", "").strip()


def _is_ip_literal(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _discover_public_host() -> str:
    """Ask the Hub what hostname its certificate carries.

    Setting this per node is the same trap the value exists to escape: it is
    one fact about the HUB, so a copy of it in every node's env file is four
    chances to drift, and a new node silently gets it wrong until someone
    remembers. The Hub already knows the answer — so ask, and let every node
    run identical code with identical config.

    Unverified TLS here is not a weakening. This call is how a node LEARNS the
    name that makes verification possible, so requiring it first would be
    circular; the LAN address it dials answers only with the Hub's own
    untrusted internal certificate by construction. Nothing secret is read —
    just a public DNS name — and the name is put straight to work on a
    connection that IS fully verified, which is where a wrong answer would
    fail loudly rather than quietly downgrade anything.

    Every failure returns "", which means "carry on as before": unreachable
    Hub, old Hub without the endpoint, or a Hub with no public name at all.
    """
    base = (HUB_INTERNAL_URL or HUB_URL or "").rstrip("/")
    if not base:
        return ""
    try:
        import json as _json
        import ssl as _ssl
        import urllib.request as _url
        ctx = _ssl._create_unverified_context() if base.startswith("https") else None
        # Short timeout on purpose: this runs during startup, and a Hub that is
        # slow or down must not hold the node's own apps hostage over it.
        with _url.urlopen(f"{base}/api/hub/public-host", timeout=4, context=ctx) as r:
            return (_json.load(r).get("public_host") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# Only worth asking when it would change something. A Hub already addressed by
# a name needs no second one, and an explicit setting is always honoured — so
# the lookup happens exactly in the case it fixes: a Hub configured by IP,
# whose certificate therefore cannot match what containers are handed.
if not HUB_PUBLIC_HOST and _is_ip_literal(HUB_HOST):
    HUB_PUBLIC_HOST = _discover_public_host()

# The name pinned into a container's /etc/hosts, and used to build the URL those
# containers are handed. Prefers the certificate host; falls back to HUB_URL's
# own, which is correct whenever the two are the same thing.
CONTAINER_HUB_HOST = HUB_PUBLIC_HOST or HUB_HOST


# The --add-host trick only works when HUB_URL carries a NAME. /etc/hosts maps
# names to addresses; an entry for an IP literal is never consulted, because
# nothing resolves an address it already has. So when the Hub is configured by
# IP, a container told to reach it at that IP will do exactly that — and if the
# address is a WireGuard one this node cannot route to, the call simply hangs.
#
# Seen live: HUB_URL=https://10.79.114.1 on a node with no tunnel. The add-host
# was applied, /etc/hosts read "10.0.0.177  10.79.114.1", and Open WebUI still
# timed out reaching the model router — with nothing in any log to say why.
#
# In that case the container has to be handed the internal address outright —
# unless a certificate hostname was named explicitly, which is the one way to
# get a verifiable TLS name AND a routable address at the same time (see
# HUB_PUBLIC_HOST above). That wins when set, because the internal address on
# its own can only ever be reached with verification turned off.
HUB_URL_FOR_CONTAINERS = (
    f"https://{HUB_PUBLIC_HOST}"
    if HUB_PUBLIC_HOST
    else HUB_INTERNAL_URL
    if (_is_ip_literal(HUB_HOST) and HUB_INTERNAL_URL and HUB_INTERNAL_URL != HUB_URL)
    else HUB_URL
)

# Ceiling on a single save-to-NAS write. The whole body is held in memory while
# it is written, so this bounds what one request can cost the NAS host — which
# on this fleet is also the machine running most of the apps.
NAS_DEPOSIT_MAX_BYTES = int(os.environ.get("SM_NAS_DEPOSIT_MAX_BYTES") or 512 * 1024 * 1024)
