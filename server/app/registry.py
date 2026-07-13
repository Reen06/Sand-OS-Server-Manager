"""App catalogue + instance lifecycle.

Holds the App Definitions, allocates per-instance ports/volumes, and resolves
launch/stop/status against the Docker backend. State is in-memory and
reconciled from Docker on startup (single-node MVP)."""
from __future__ import annotations
import re
import threading
from .models import AppDef, Instance, Mount, Service
from . import config, docker_backend

# ── App catalogue (add more App Definitions here) ──────────────────────────────
APPS: dict[str, AppDef] = {
    "freecad": AppDef(
        id="freecad",
        label="FreeCAD",
        icon="cpu",
        color="blue",
        desc="Full FreeCAD 1.1.1, streamed — your own GPU instance.",
        image=config.FREECAD_IMAGE,
        kind="streamed",
        mode="per-user",
        internal_port=8080,
        gpu=True,
        mem_limit="3g",
        encoder="nvh264enc",
        keepalive_seconds=600,
        # The user's NAS home over NFS — the SAME files they see in Nextcloud,
        # network-mounted here (no duplication) so saves persist on the NAS even
        # when FreeCAD runs on a different box. Also bound at /mnt/freecad-projects
        # for the legacy path.
        mounts=[
            Mount(name="home", path="/home/ubuntu/NAS", scope="per-user", storage="nfs"),
            Mount(name="home", path="/mnt/freecad-projects", scope="per-user", storage="nfs"),
            # Persistent per-user app settings (NAS .appdata/*): preferences,
            # toolbars, macros survive relaunches and follow the user across
            # nodes. Also what "snapshot" and "factory reset" operate on.
            Mount(name="freecad-config", path="/home/ubuntu/.config", scope="per-user", storage="nfs"),
            Mount(name="freecad-share", path="/home/ubuntu/.local/share", scope="per-user", storage="nfs"),
        ],
    ),
    "filebrowser": AppDef(
        id="filebrowser",
        label="Files",
        icon="database",   # whitelisted NAS/storage glyph in the dashboard
        color="amber",
        desc="Browse & manage your files — private home + the shared library.",
        image=config.FILEBROWSER_IMAGE,
        kind="web",
        mode="per-user",
        internal_port=8080,
        gpu=False,
        mem_limit="512m",
        # The NAS made visible: a private home (per-user) + a library every app
        # and user shares. 'media' resolves to sm-shared-media (also mounted by a
        # future Jellyfin) — proving shared-across-apps data.
        # Same NAS home as FreeCAD/Nextcloud (users/{user}) + the shared library,
        # over NFS — one set of files across every app, no duplication.
        mounts=[
            Mount(name="home", path="/srv/home", scope="per-user", storage="nfs"),
            Mount(name="media", path="/srv/media", scope="shared", storage="nfs"),
        ],
        # The wrapper image's entrypoint provisions noauth (the Hub session is the
        # real gate) and binds 0.0.0.0:8080 serving /srv. We only inject the
        # baseURL so its SPA assets resolve under the proxy subpath.
        env={"FB_BASEURL": f"{config.EXTERNAL_BASE}/stream/filebrowser"},
    ),
    "webcad": AppDef(
        id="webcad",
        label="WebCAD/CAM",
        icon="cpu",         # whitelisted CAD/compute glyph in the dashboard
        color="blue",
        desc="Browser CAD/CAM for the Carvera — model right in your dashboard.",
        image=config.WEBCAD_IMAGE,
        kind="web",
        mode="shared",              # one host; per-connection sessions isolate users
        internal_port=8137,         # the Node host serves client + WebSocket here
        gpu=False,
        mem_limit="2g",
        # The host serves the client bundle at root and the client uses relative asset
        # URLs (vite base "./"), so the proxy strips the /apps/stream/webcad prefix.
        proxy_subpath="root",
        # DEV: run live from the bind-mounted source tree so edits on the host rebuild
        # (vite build --watch) and reload (tsx watch) without a redeploy. The image's
        # node_modules VOLUMEs keep the host tree from shadowing container installs.
        binds=[("/home/control/webcadcam", "/app")],
        # Persistent pnpm store: the anonymous node_modules volumes die with the
        # --rm container, so every (re)launch cold-installed for minutes — a warm
        # store makes that a fast relink (the post-SM-restart "not found" window).
        mounts=[Mount(name="webcad-pnpm-store", path="/pnpm-store", scope="shared")],
    ),
    "helix": AppDef(
        id="helix",
        label="HeliX Motion",
        icon="cpu",         # whitelisted CAD/compute glyph in the dashboard
        color="green",
        desc="CNC control for the Carvera Air — jog, run jobs, resume, 3D view.",
        image=config.HELIX_IMAGE,
        kind="web",
        mode="shared",              # one machine, one controller connection
        internal_port=8556,         # FastAPI serves dashboard + API + WebSocket
        gpu=False,
        mem_limit="2g",
        # The bundle uses relative URLs (vite base "./"), so the proxy strips the
        # /apps/stream/helix prefix like WebCAD.
        proxy_subpath="root",
        # DEV: run live from the bind-mounted source tree (uvicorn --reload +
        # vite build --watch) — edit on the host, validate in the dashboard.
        binds=[("/home/control/CNC_Controller", "/app")],
        # Named volumes so pip/pnpm installs persist across launches — with the
        # image's anonymous volumes + --rm, EVERY start was a minutes-long cold
        # install. First launch still installs; later ones are incremental.
        mounts=[
            Mount(name="helix-venv", path="/venv", scope="shared"),
            Mount(name="helix-node-modules", path="/app/web/node_modules", scope="shared"),
            Mount(name="helix-pnpm-store", path="/pnpm-store", scope="shared"),
        ],
        # Subnets its Carvera discovery TCP-sweeps (home LAN + gateway-node LANs).
        env={"HELIX_SCAN_SUBNETS": config.HELIX_SCAN_SUBNETS},
    ),
    "rayoptics": AppDef(
        id="rayoptics",
        label="Ray Optics",
        icon="cpu",          # whitelisted; closest sim/compute glyph the Hub ships
        color="cyan",
        desc="2D geometric optics simulator — draw rays, lenses and mirrors.",
        image=config.RAYOPTICS_IMAGE,
        kind="web",
        mode="shared",       # one static site for everyone; saving is per-user via /api/files
        internal_port=80,
        gpu=False,
        mem_limit="256m",
        # A plain static build (nginx) served at container root — no baseURL
        # awareness, so the proxy strips to root like Nextcloud/WebCAD.
        proxy_subpath="root",
    ),
    "nextcloud": AppDef(
        id="nextcloud",
        label="Nextcloud",
        icon="globe",       # whitelisted; the closest "cloud" glyph the Hub ships
        color="blue",
        desc="Your private cloud — files, Photos, sharing. One account, SSO'd.",
        image=config.NEXTCLOUD_IMAGE,
        kind="web",
        mode="shared",              # ONE Nextcloud, per-user accounts inside it
        internal_port=80,
        gpu=False,
        mem_limit="1536m",
        # Nextcloud serves at root and rewrites its own links via OVERWRITEWEBROOT,
        # so the proxy strips the subpath; and it SSOs via a trusted Remote-User
        # header (user_saml environment-variable backend) → no second login.
        proxy_subpath="root",
        sso_header="Remote-User",
        # One shared data volume holds the whole install (code+config+user data);
        # the fleet NAS is mounted at /nas so Nextcloud can expose each user's home
        # (users/$user) + shared folders as External Storage — the SAME files apps
        # read/write over NFS.
        mounts=[
            Mount(name="nextcloud-data", path="/var/www/html", scope="shared"),
            Mount(name="nas", path="/nas", scope="root", storage="nfs"),
        ],
        env={
            "MYSQL_HOST": "db",
            "MYSQL_DATABASE": config.NC_DB_NAME,
            "MYSQL_USER": config.NC_DB_USER,
            "MYSQL_PASSWORD": config.NC_DB_PASSWORD,
            "REDIS_HOST": "redis",
            "NEXTCLOUD_ADMIN_USER": config.NC_ADMIN_USER,
            "NEXTCLOUD_ADMIN_PASSWORD": config.NC_ADMIN_PASSWORD,
            "NEXTCLOUD_TRUSTED_DOMAINS": config.NC_TRUSTED_DOMAINS,
            "OVERWRITEWEBROOT": f"{config.EXTERNAL_BASE}/stream/nextcloud",
            "OVERWRITEPROTOCOL": "https",
            "OVERWRITEHOST": config.NC_OVERWRITE_HOST,      # empty = derive from Host
            "APACHE_DISABLE_REWRITE_IP": "1",
            "TRUSTED_PROXIES": "127.0.0.1 172.16.0.0/12 10.0.0.0/8",
        },
        services=[
            Service(
                name="db",
                image=config.MARIADB_IMAGE,
                cmd=["--transaction-isolation=READ-COMMITTED",
                     "--log-bin=binlog", "--binlog-format=ROW"],
                env={
                    "MARIADB_ROOT_PASSWORD": config.NC_DB_ROOT_PASSWORD,
                    "MARIADB_DATABASE": config.NC_DB_NAME,
                    "MARIADB_USER": config.NC_DB_USER,
                    "MARIADB_PASSWORD": config.NC_DB_PASSWORD,
                },
                mounts=[Mount(name="nextcloud-db", path="/var/lib/mysql", scope="shared")],
                ready_cmd=["sh", "-c", "mariadb-admin ping -p$MARIADB_ROOT_PASSWORD --silent"],
            ),
            Service(name="redis", image=config.REDIS_IMAGE),
        ],
    ),
}


def resolve_volume(app_id: str, user: str, m: Mount) -> str:
    """Logical mount → real docker volume name. Per-user volumes are private to
    one user; shared volumes are one library many apps/users can mount."""
    if m.scope == "shared":
        return f"sm-shared-{_safe(m.name)}"
    return f"sm-{_safe(app_id)}-{_safe(user)}-{_safe(m.name)}"

# slot -> (app_id, user)  ;  (app_id, user) -> Instance
_slots: dict[int, tuple[str, str]] = {}
_instances: dict[tuple[str, str], Instance] = {}

# Per-instance launch locks. FastAPI runs sync endpoints in a threadpool, so two
# overlapping launch requests for the SAME (app_id, user) — e.g. a mobile
# double-tap, or the same account open in both a PWA and a browser tab — can
# both see "not running yet" and both invoke `docker run --name X` at once.
# That races Docker's port-publish/iptables setup and fails with a networking
# error on one of them. Serializing launch() per key turns the second call into
# a wait-then-reuse instead of a duplicate `docker run`.
_launch_locks: dict[tuple[str, str], threading.Lock] = {}
_launch_locks_meta = threading.Lock()


def _lock_for(key: tuple[str, str]) -> threading.Lock:
    with _launch_locks_meta:
        lock = _launch_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _launch_locks[key] = lock
        return lock


def _safe(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


# Effective-user key for mode=shared apps: everyone maps to one instance.
_SHARED = "_shared"


def _eff(app_id: str, user: str) -> str:
    app = APPS.get(app_id)
    return _SHARED if (app and app.mode == "shared") else user


def instance_name(app_id: str, user: str) -> str:
    app = APPS.get(app_id)
    if app and app.mode == "shared":
        return f"sm-{_safe(app_id)}"           # one shared instance, no user suffix
    return f"sm-{_safe(app_id)}-{_safe(user)}"


def _alloc_slot(app_id: str, user: str) -> int:
    key = (app_id, user)
    for s, owner in _slots.items():
        if owner == key:
            return s
    for s in range(config.SLOT_COUNT):
        if s not in _slots:
            _slots[s] = key
            return s
    raise RuntimeError("no free instance slots")


def _instance_for(app_id: str, user: str) -> Instance:
    user = _eff(app_id, user)
    key = (app_id, user)
    inst = _instances.get(key)
    if inst:
        return inst
    slot = _alloc_slot(app_id, user)
    relay_min = config.RELAY_BASE + slot * config.RELAY_PER_SLOT
    inst = Instance(
        app_id=app_id, user=user, slot=slot,
        name=instance_name(app_id, user),
        web_port=config.WEB_PORT_BASE + slot,
        turn_port=config.TURN_PORT_BASE + slot,
        relay_min=relay_min,
        relay_max=relay_min + config.RELAY_PER_SLOT - 1,
        volume=f"{instance_name(app_id, user)}-projects",
    )
    _instances[key] = inst
    return inst


def get_instance(app_id: str, user: str) -> Instance | None:
    return _instances.get((app_id, _eff(app_id, user)))


def url_for(inst: Instance) -> str:
    return f"http://{config.LAN_IP}:{inst.web_port}"


def status(app_id: str, user: str) -> str:
    """stopped | starting (running, web not ready) | active (connected) | idle."""
    inst = _instances.get((app_id, _eff(app_id, user)))
    if not inst or not docker_backend.running(inst.name):
        return "stopped"
    if not docker_backend.web_ready(inst.web_port):
        return "starting"
    return "active" if docker_backend.active_connections(inst.web_port) > 0 else "idle"


def launch(app_id: str, user: str) -> Instance:
    if app_id not in APPS:
        raise KeyError(app_id)
    app = APPS[app_id]
    with _lock_for((app_id, _eff(app_id, user))):
        inst = _instance_for(app_id, user)
        if not docker_backend.running(inst.name):
            res = docker_backend.spawn(inst, app)
            if res.returncode != 0:
                if app.services:
                    docker_backend.teardown(inst.name, app)  # clean a partial stack
                raise RuntimeError(res.stderr.strip() or "docker run failed")
        return inst


def stop(app_id: str, user: str) -> None:
    name = instance_name(app_id, user)   # shared-aware
    app = APPS.get(app_id)
    if app:
        docker_backend.teardown(name, app)   # primary + any sidecars + network
    else:
        docker_backend.stop(name)


def instances_summary() -> list[dict]:
    """Every currently-tracked instance across all apps, with its container name
    and running state — used by /api/sm/apps/stats (Fleet page's per-app
    breakdown) to join registry state against `docker stats`."""
    return [
        {"app_id": app_id, "user": user, "name": inst.name,
         "running": docker_backend.running(inst.name)}
        for (app_id, user), inst in _instances.items()
    ]


def list_for_user(user: str) -> list[dict]:
    """App catalogue with this user's per-app status + URL (if running)."""
    out = []
    for app in APPS.values():
        st = status(app.id, user)
        inst = get_instance(app.id, user)
        out.append({
            "id": app.id, "label": app.label, "icon": app.icon,
            "color": app.color, "desc": app.desc, "kind": app.kind,
            "status": st,
            "url": url_for(inst) if (inst and st != "stopped") else None,
        })
    return out


def reconcile_from_docker() -> None:
    """On startup, re-adopt any existing sm- containers using their ACTUAL
    published ports (not a fresh slot) so the in-memory map matches reality and
    new launches don't collide with a running instance's ports."""
    for name in docker_backend.list_sm_containers():
        web_port = docker_backend.published_web_port(name)
        if web_port is None:
            continue   # sidecars (DB/cache) publish no web port — skip them
        # Identify (app_id, user). Shared apps are a bare `sm-{app}`; per-user
        # apps are `sm-{app}-{user}`.
        bare = name[3:]  # strip 'sm-'
        app_id = user = None
        if bare in APPS and APPS[bare].mode == "shared":
            app_id, user = bare, _SHARED
        else:
            m = re.match(r"^sm-([a-z0-9-]+?)-(.+)$", name)
            if m and m.group(1) in APPS:
                app_id, user = m.group(1), m.group(2)
        if not app_id:
            continue
        slot = web_port - config.WEB_PORT_BASE
        if not (0 <= slot < config.SLOT_COUNT):
            continue
        relay_min = config.RELAY_BASE + slot * config.RELAY_PER_SLOT
        _slots[slot] = (app_id, user)
        _instances[(app_id, user)] = Instance(
            app_id=app_id, user=user, slot=slot, name=name,
            web_port=web_port, turn_port=config.TURN_PORT_BASE + slot,
            relay_min=relay_min, relay_max=relay_min + config.RELAY_PER_SLOT - 1,
            volume=f"{name}-projects",
        )
