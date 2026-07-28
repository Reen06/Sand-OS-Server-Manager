"""App catalogue + instance lifecycle.

Holds the App Definitions, allocates per-instance ports/volumes, and resolves
launch/stop/status against the Docker backend. State is in-memory and
reconciled from Docker on startup (single-node MVP)."""
from __future__ import annotations
import json
import os
import re
import subprocess
import threading
import time
from .models import AppDef, AppVariant, Instance, Mount, Service
from . import config, docker_backend

# Cache for list_for_user()'s per-app "is the image installed" check — it only
# ever changes right after an explicit build/pull/move, but list_for_user()
# itself is polled every 5s by the dashboard, so a subprocess `docker image
# inspect` per app per poll would be needless, recurring overhead.
_INSTALLED_CACHE: dict[str, tuple[float, bool]] = {}
_INSTALLED_TTL = 30.0

# ── App catalogue (add more App Definitions here) ──────────────────────────────
# CATALOG = every app definition this SM build SHIPS. APPS (built below) =
# the subset actually enabled on THIS node, plus dynamically-registered apps
# (Docker Hub installs, pending USB imports). A fresh install starts with an
# EMPTY library — only apps the owner adds ever appear on their dashboard.
CATALOG: dict[str, AppDef] = {
    "freecad": AppDef(
        id="freecad",
        label="FreeCAD",
        icon="cpu",
        color="blue",
        desc="Full FreeCAD 1.1.1, streamed — your own GPU instance.",
        image=config.FREECAD_IMAGE,
        dockerhub_repo="reen16/freecad-streamer",   # §11 publish target (fork of an upstream app, SM-tailored)
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
        # Installable versions — Manage version… in the app tile's gear menu.
        # "dev" installs whatever FreeCAD/FreeCAD's current weekly build is at
        # install time (rolling; the resolver re-checks GitHub each install).
        build_context="containers/freecad-streamer",
        image_family="freecad-streamer",
        variants=[
            AppVariant(
                id="stable", label="Stable 1.1.1", channel="stable",
                image_tag="freecad-streamer:dev",
                build_args={"FREECAD_APPIMAGE_URL":
                    "https://github.com/FreeCAD/FreeCAD/releases/download/1.1.1/"
                    "FreeCAD_1.1.1-Linux-x86_64-py311.AppImage"},
            ),
            AppVariant(
                id="weekly-dev", label="Latest weekly dev build", channel="dev",
                image_tag="freecad-streamer:weekly-dev",
                resolver="freecad-weekly",
            ),
        ],
    ),
    "filebrowser": AppDef(
        id="filebrowser",
        label="Files",
        icon="database",   # whitelisted NAS/storage glyph in the dashboard
        color="amber",
        desc="Browse & manage your files — private home + the shared library.",
        image=config.FILEBROWSER_IMAGE,
        dockerhub_repo="reen16/filebrowser",   # §11 publish target (fork of an upstream app, SM-tailored)
        build_context="containers/filebrowser",
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
        # FB_PORT: the image's own /healthcheck.sh derives PORT/ADDRESS by
        # parsing /config/settings.json — which never exists here (no config
        # file is mounted; noauth mode runs off CLI flags only), so Docker
        # reported this container permanently "unhealthy" (harmless — nothing
        # in SM reads Docker's health status, and no restart policy reacts to
        # it — but genuinely wrong). Setting FB_PORT makes the script skip the
        # settings.json read entirely (its own `${FB_PORT:-...}` fallback).
        env={"FB_BASEURL": f"{config.EXTERNAL_BASE}/stream/filebrowser",
             "FB_PORT": "8080"},
    ),
    "webcad": AppDef(
        id="webcad",
        label="WebCAD/CAM",
        icon="cpu",         # whitelisted CAD/compute glyph in the dashboard
        color="blue",
        desc="Browser CAD/CAM for the Carvera — model right in your dashboard.",
        image=config.WEBCAD_IMAGE,
        packaged_image=config.WEBCAD_PACKAGED_IMAGE,
        packaged_build_context="/home/control/webcadcam",
        packaged_dockerfile="containers/webcad/Dockerfile.packaged",
        dockerhub_repo="reen16/webcad",
        # Dockerfile lives INSIDE the sibling source repo (self-contained —
        # entrypoint.sh is right there too), not in this repo. Absolute path
        # is fine here (app_variants.py's os.path.join keeps an absolute
        # build_context as-is), even though this app has no variants today.
        build_context="/home/control/webcadcam/containers/webcad",
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
        # vite build --watch binds the port instantly but serves a 404 until its
        # first build finishes — a plain "responded at all" check falsely marks
        # this ready mid-build, dropping the dashboard into that "not found"
        # page (the exact window the comment above already named).
        strict_ready=True,
    ),
    "helix": AppDef(
        id="helix",
        label="HeliX Motion",
        icon="cpu",         # whitelisted CAD/compute glyph in the dashboard
        color="green",
        desc="CNC control for the Carvera Air — jog, run jobs, resume, 3D view.",
        image=config.HELIX_IMAGE,
        packaged_image=config.HELIX_PACKAGED_IMAGE,
        packaged_build_context="/home/control/CNC_Controller",
        packaged_dockerfile="containers/helix/Dockerfile.packaged",
        dockerhub_repo="reen16/helix",
        # Same "Dockerfile lives inside the sibling repo" shape as webcad.
        build_context="/home/control/CNC_Controller/containers/helix",
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
        # Same live-dev rebuild-on-launch window as WebCAD (vite build --watch) —
        # see strict_ready's docstring in models.py.
        strict_ready=True,
    ),
    "openmapper": AppDef(
        id="openmapper",
        label="OpenMapper",
        icon="zap",
        color="cyan",
        desc="Touch-first lighting and projection controller with simulated lights.",
        # Build from /home/control/OpenMapper/app/Dockerfile (source is COPYed
        # in at build time here, not bind-mounted — no `binds` on this app).
        image=config.OPENMAPPER_IMAGE,
        dockerhub_repo="reen16/openmapper",   # §11 publish target (fork of an upstream app, SM-tailored)
        build_context="/home/control/OpenMapper/app",
        kind="web",
        mode="shared",
        internal_port=8080,
        gpu=False,
        mem_limit="512m",
        proxy_subpath="root",
        mounts=[Mount(name="openmapper-projects", path="/data", scope="shared", storage="nfs")],
    ),
    "rayoptics": AppDef(
        id="rayoptics",
        label="Ray Optics",
        icon="cpu",          # whitelisted; closest sim/compute glyph the Hub ships
        color="cyan",
        desc="2D geometric optics simulator — draw rays, lenses and mirrors.",
        image=config.RAYOPTICS_IMAGE,
        dockerhub_repo="reen16/rayoptics",   # §11 publish target (fork of an upstream app, SM-tailored)
        # The REAL build context is the ray-optics source checkout — the
        # Dockerfile's `COPY . .` copies the app source, and build.sh always
        # built it that way (`-f containers/rayoptics/Dockerfile <checkout>`).
        # The old value here ("containers/rayoptics", just the Dockerfile's
        # own dir) never produced a working build; it only looked plausible
        # because nothing re-ran the build from the AppDef until the §11
        # publish flow tried to. Same context-lives-in-the-source-clone shape
        # as engineeringpaper.
        build_context="/home/control/ray-optics",
        build_dockerfile="containers/rayoptics/Dockerfile",
        kind="web",
        mode="shared",       # one static site for everyone; saving is per-user via /api/files
        internal_port=80,
        gpu=False,
        mem_limit="256m",
        # A plain static build (nginx) served at container root — no baseURL
        # awareness, so the proxy strips to root like Nextcloud/WebCAD.
        proxy_subpath="root",
    ),
    "renode": AppDef(
        id="renode",
        label="Renode",
        icon="cpu",
        color="green",
        desc="Open-source microcontroller simulator — a real terminal onto "
             "Renode's monitor console (load .resc scripts, simulate boards, "
             "read UART). Not a Wokwi-style breadboard GUI — Wokwi itself "
             "isn't self-hostable.",
        # `docker build -t sm-renode-web:latest containers/renode-web` once,
        # same manual-build pattern as WebCAD/HeliX/OpenMapper (no variants).
        image=config.RENODE_IMAGE,
        dockerhub_repo="reen16/renode-web",   # §11 publish target (fork of an upstream app, SM-tailored)
        build_context="containers/renode-web",
        kind="web",
        mode="shared",
        internal_port=8080,
        gpu=False,
        mem_limit="1g",
        proxy_subpath="root",     # ttyd serves its own root UI
        mounts=[Mount(name="renode-projects", path="/root/projects", scope="shared", storage="nfs")],
    ),
    "engineeringpaper": AppDef(
        id="engineeringpaper",
        label="EngineeringPaper.xyz",
        icon="cpu",          # whitelisted; closest sim/compute glyph the Hub ships
        color="cyan",
        desc="Browser math-sheet editor — live SymPy/numeric calculation as you type.",
        # `docker build -f containers/engineeringpaper/Dockerfile -t
        # sm-engineeringpaper:latest /home/control/EngineeringPaper.xyz` once,
        # same manual-build pattern as Ray Optics/Renode (no variants).
        image=config.ENGINEERINGPAPER_IMAGE,
        dockerhub_repo="reen16/engineeringpaper",   # §11 publish target (fork of an upstream app, SM-tailored)
        build_context="/home/control/EngineeringPaper.xyz",
        build_dockerfile="containers/engineeringpaper/Dockerfile",
        kind="web",
        # Was "shared" (one static site for everyone, no accounts) until the
        # NAS save/open feature needed to know whose NAS home to read/write.
        # Rather than invent an identity-passing scheme for a plain nginx
        # static site with no login of its own, switched to the same
        # per-user-container pattern as FreeCAD/ParaView: one container per
        # logged-in Hub user, with exactly that user's NAS home mounted at a
        # fixed path — the container's sidecar API (sandos-nas-api/server.js
        # in the EngineeringPaper.xyz checkout) never needs to check who's
        # asking, because the mount itself is already scoped.
        mode="per-user",
        internal_port=80,
        gpu=False,
        mem_limit="256m",
        proxy_subpath="root",     # plain nginx static build, no baseURL awareness
        # Vite build emits absolute-path asset tags (src="/assets/index-*.js",
        # no leading "./") — a leading-slash URL always resolves against the
        # browser's origin root, ignoring whatever subpath the page itself
        # was served under. Confirmed live (2026-07-16): 404s against SM/Hub's
        # own routes under /apps/stream/, surfacing as their literal
        # {"detail":"Not Found"}. Same class of bug, same fix, as Stirling
        # PDF — see own_subdomain's docstring in models.py.
        own_subdomain=True,       # served at calc.<domain>
        mounts=[Mount(name="home", path="/nas", scope="per-user", storage="nfs")],
    ),
    "openfoamgui": AppDef(
        id="openfoamgui",
        label="OpenFOAM GUI",
        icon="cpu",
        color="blue",
        desc="Case manager + web UI for OpenFOAM CFD simulations (propeller, wind tunnel).",
        # `docker build -f containers/openfoam-gui/Dockerfile -t
        # sm-openfoam-gui:latest containers/openfoam-gui` once, run directly
        # against the USB app-hosting drive's dockerd (-H <usb-socket>) — the
        # opencfd/openfoam-run base is several GB, never touches local disk.
        image=config.OPENFOAM_GUI_IMAGE,
        build_context="containers/openfoam-gui",
        kind="web",
        mode="shared",
        internal_port=6060,   # entrypoint.sh's own uvicorn bind, confirmed
        gpu=False,
        mem_limit="4g",       # CFD solves can be memory-hungry
        proxy_subpath="root",
        # Even more absolute-path usage than Stirling PDF: hardcoded CSS/JS
        # tags (/static/landing/...), a fetch('/api/lan-info') call, AND a
        # service worker registered at absolute scope '/' (navigator.
        # serviceWorker.register('/sw.js', {scope: '/'})) — a SW claiming
        # root scope from inside a subpath either fails outright or, worse,
        # tries to control the WHOLE origin (every other app under
        # /apps/stream/) depending on the Service-Worker-Allowed header.
        # Confirmed live (2026-07-16): 404s under /apps/stream/ the same way
        # Stirling PDF did. Same fix — own_subdomain, see models.py.
        own_subdomain=True,       # served at cfd.<domain>
        # DEV: run live from the bind-mounted source tree (mirrors webcad/
        # helix) — the entrypoint pip-installs from it on every start and the
        # app's own case_manager.py resolves its case registry relative to its
        # own script dir (i.e. /gui/cases), so this is a plain bind, not an
        # NFS Mount — no host-editing workflow needed here, but the app was
        # already built expecting /gui to be the live repo, not baked-in code.
        binds=[("/home/control/OpenFOAM_GUI", "/gui")],
        packaged_image=config.OPENFOAM_GUI_PACKAGED_IMAGE,
        # Unlike webcad/helix (Dockerfile.packaged under containers/<app>/ in
        # their repos), this one sits at the repo ROOT — the dev Dockerfile
        # lives in THIS repo (containers/openfoam-gui), not the app's, so the
        # app repo had no containers/ dir to mirror.
        packaged_build_context="/home/control/OpenFOAM_GUI",
        packaged_dockerfile="Dockerfile.packaged",
        dockerhub_repo="reen16/openfoamgui",
    ),
    "paraview": AppDef(
        id="paraview",
        label="ParaView",
        icon="globe",         # whitelisted; visualizer glyph
        color="green",
        desc="Scientific data visualizer (ParaViewWeb) — view and analyze simulation results.",
        # `docker build -t sandos-paraview:latest containers/paraview` once,
        # directly against the USB app-hosting drive's dockerd (-H <usb-socket>)
        # — the OSMesa software-rendering stack is large, never touches local
        # disk. Thin layer on the official Kitware image; see config.py's
        # PARAVIEW_IMAGE comment for why the custom build exists (retry=0 on
        # the launcher's ProxyPass — an Apache mod_proxy circuit-breaker bug,
        # not anything GPU/rendering-related).
        image=config.PARAVIEW_IMAGE,
        dockerhub_repo="reen16/paraview",   # §11 publish target (fork of an upstream app, SM-tailored)
        build_context="containers/paraview",
        kind="web",
        # per-user, not shared: each user gets their OWN instance/data — a
        # single shared ParaView would mean every user's files landed in the
        # same /data directory with no isolation at all. Matches FreeCAD's
        # own per-user pattern for the same reason (a personal analysis tool,
        # not a always-on shared service like Nextcloud).
        mode="per-user",
        internal_port=80,     # confirmed via upstream Dockerfile's own ENTRYPOINT
        gpu=False,
        mem_limit="2g",
        proxy_subpath="root",
        # The launcher's own launcher.json hardcodes "dataDir": "/data" (used
        # as pvw-visualizer.py's --data argument) — without a real mount
        # there, that path doesn't exist at all, so the app's file browser
        # panel has nowhere to open/save anything (confirmed live
        # 2026-07-16: the toolbar/panels render fine — session, WS, and
        # rendering pipeline all genuinely work — the 3D view is just
        # legitimately blank until a dataset is loaded, which was
        # impossible with no /data). Same NAS home (users/{user}) already
        # shared across FreeCAD/Filebrowser/Nextcloud.
        mounts=[Mount(name="home", path="/data", scope="per-user", storage="nfs")],
        # ParaViewWeb's own JS bundle builds its wslink WebSocket URL as
        # (location.protocol==='https:'?'wss':'ws') + '://' + host + ':' + port
        # + '/ws' — an ABSOLUTE path with no awareness of any subpath prefix.
        # Symptom: page renders once (a "quick flash"), then the WS connect to
        # a bare "/ws" (instead of "/apps/stream/paraview/ws") 404s/never
        # matches any route, and the app's own error handling blanks the page.
        # Same fix as Stirling PDF/EngineeringPaper/OpenFOAM GUI — own
        # subdomain, where the Caddy rewrite (`rewrite * /stream/paraview{uri}`)
        # prepends the app scope to EVERY request including the WS upgrade, so
        # the bare "/ws" correctly becomes "/stream/paraview/ws" before it ever
        # reaches the Hub's routing.
        own_subdomain=True,       # served at pv.<domain>
        # Apache (fronting the container) answers "/" in ~1s, but ParaViewWeb's
        # own JS immediately POSTs to "/paraview/" on page load — proxied by
        # Apache to a SEPARATE wslink launcher process. If that launcher isn't
        # listening yet when the one-shot POST fires (no retry in the app's
        # own code), Apache's mod_proxy 503s and the page goes blank right
        # after its first paint. Checking readiness against "/paraview/"
        # itself (not root) means the dashboard won't report "ready" — and
        # won't load the iframe — until the actual dependency is up.
        ready_path="paraview/",
        # A GET to "/paraview/" correctly 400s (wrong method) once the
        # launcher is genuinely listening — never a 2xx, so strict_ready
        # can't be used here. 503 specifically means Apache's mod_proxy
        # couldn't reach the backend at all — confirmed live: without this,
        # the lenient check waved a 503 through as "answered, therefore
        # ready", defeating the whole point of ready_path.
        ready_bad_status=(503,),
    ),
    "stirlingpdf": AppDef(
        id="stirlingpdf",
        label="Stirling PDF",
        icon="pencil",       # whitelisted; closest "edit a document" glyph the Hub ships
        color="amber",
        desc="FOSS PDF toolkit — merge, split, convert, OCR, sign, and more.",
        image=config.STIRLINGPDF_IMAGE,
        # Pure upstream image with no build context: nothing can build it
        # locally, so the ONLY way it reaches a node is a pull. Without this,
        # spawn() skipped its pre-pull and left `docker run` to fetch the
        # image inside its own 120s cap — which a ~1GB image never makes, so
        # the first launch on any fresh node failed and rolled the pull back.
        auto_pull=True,   # public image — pre-pulled with a long timeout
        kind="web",
        mode="shared",       # one shared tool instance; no per-user accounts of its own
        internal_port=8080,
        gpu=False,
        # 1g used to crash reliably ~20s into boot: java.lang.OutOfMemoryError:
        # Metaspace. The image's own entrypoint (init-without-ocr.sh) scales
        # -XX:MaxMetaspaceSize by detected container memory — 128m at <=1024MB,
        # 192m at <=2048MB — and its own comments say the 1024MB tier is already
        # tight (heap% + metaspace + ~200MB native/LibreOffice overhead). 2.14.2
        # loads enough classes (VeraPDF, FontForge, PdfJson, cluster backplane)
        # to blow the 128m cap in practice. 2g puts it in the tier upstream
        # actually designed for; confirmed live this boots clean and stays up.
        mem_limit="2g",
        proxy_subpath="root",     # not baseURL-aware, like WebCAD/Ray Optics
        own_subdomain=True,       # served at pdf.<domain> — see models.py for why
        # Stirling ships its own login (security.enableLogin: true by default) —
        # separate from and unaware of our SM/Hub SSO. Direct requests correctly
        # 401 "Full authentication is required", but its React frontend then
        # redirects to Stirling's OWN /login using an absolute path, which
        # escapes our /stream/stirlingpdf/ proxy prefix entirely and 404s
        # against SM/Hub's own routes instead — the literal {"detail":"Not
        # Found"} the user saw is FastAPI's own default 404, not anything
        # Stirling returned. Since our proxy already gates access to this app,
        # disable Stirling's redundant native login rather than try to wire up
        # a second SSO integration for it.
        env={"SECURITY_ENABLELOGIN": "false"},
        mounts=[
            Mount(name="stirlingpdf-config", path="/configs", scope="shared"),
            Mount(name="stirlingpdf-logs", path="/logs", scope="shared"),
        ],
    ),
    "nextcloud": AppDef(
        id="nextcloud",
        label="Nextcloud",
        icon="globe",       # whitelisted; the closest "cloud" glyph the Hub ships
        color="blue",
        desc="Your private cloud — files, Photos, sharing. One account, SSO'd.",
        image=config.NEXTCLOUD_IMAGE,
        dockerhub_repo="reen16/nextcloud",   # §11 publish target (fork of an upstream app, SM-tailored)
        build_context="containers/nextcloud",
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
            # Collabora Online — Docs/Sheets/Slides, wired in via the richdocuments
            # app (containers/nextcloud/20-sm-saml.sh installs + points it at
            # http://collabora:9980). No published host port — same as db/redis,
            # reached only container-to-container by Nextcloud itself.
            Service(
                name="collabora",
                image=config.COLLABORA_IMAGE,
                env={
                    "domain": config.COLLABORA_DOMAIN_REGEX,
                    "extra_params": "--o:ssl.enable=false --o:ssl.termination=true --o:net.proto=IPv4",
                    "dictionaries": "en_US",
                },
                # Collabora's entrypoint wants /dev/mqueue writable + (on some
                # kernels) CAP_MKNOD for its loolwsd sandbox setup.
                docker_args=["--cap-add", "MKNOD"],
            ),
        ],
    ),
    "ollama": AppDef(
        id="ollama",
        label="Ollama",
        icon="cpu",
        color="violet",
        desc="Local LLM runner — pull and serve models via OpenAI-compatible API.",
        image=config.OLLAMA_IMAGE,
        kind="web",
        mode="shared",
        internal_port=11434,
        gpu=config.HAS_GPU,
        mem_limit="",   # no hard cap — GPU VRAM is the real constraint
        proxy_subpath="root",
        auto_pull=True,   # public image — Docker pulls on first start
        # Both Ollama and OpenWebUI join sm-llm-net so OpenWebUI can reach Ollama
        # by container name (sm-ollama:11434) without a fixed host-port binding.
        # The SM proxy still reaches Ollama via the slot port on 127.0.0.1.
        # RESERVED FLEET PORT: 11434 is also published on the host so OTHER nodes'
        # Open WebUI can reach this Ollama at the predictable http://<node-ip>:11434
        # (slot ports change every launch; this one never does). Ports 11434-11443
        # are reserved fleet-wide for Ollama — one per extra instance if a node
        # ever runs more than one. NOTE: this exposes the unauthenticated Ollama
        # API to the LAN/WireGuard — fine on the home network, do not port-forward.
        docker_args=["--network", "sm-llm-net", "-p", "11434:11434"],
        mounts=[Mount(name="ollama-models", path="/root/.ollama", scope="shared")],
        env={"OLLAMA_HOST": "0.0.0.0"},
    ),
    "open-webui": AppDef(
        id="open-webui",
        label="Open WebUI",
        icon="globe",
        color="purple",
        desc="Browser chat interface for your local AI models — SSO'd, auto-connects to Ollama.",
        # `docker build -t sandos-open-webui:latest containers/open-webui`
        # once — a thin layer on the upstream image patching the code-
        # interpreter's sandboxed iframe (see config.py's OPEN_WEBUI_IMAGE
        # comment for why).
        image=config.OPEN_WEBUI_IMAGE,
        dockerhub_repo="reen16/open-webui",   # §11 publish target (fork of an upstream app, SM-tailored)
        build_context="containers/open-webui",
        kind="web",
        mode="shared",
        internal_port=8080,
        gpu=False,
        # Bumped 1g→2g: rebuilding containers/open-webui/Dockerfile pulled a
        # fresh ":main" (v0.10.2, notably newer than whatever was previously
        # cached/running) — confirmed live 2026-07-16 this version gets
        # stuck at ~92% of a 1g limit during startup with 0% CPU (a genuine
        # cgroup-memory-pressure stall, not slow work: verified across
        # multiple samples with unchanging memory and no progress in the
        # logs past the initial log-level line), same class of issue as
        # Stirling PDF's Metaspace OOM. Same fix: give it the memory upstream
        # actually needs now.
        mem_limit="2g",
        proxy_subpath="root",
        auto_pull=True,   # public image — Docker pulls on first start
        # Inject Hub username → trusted-header auto-login (no separate login screen).
        sso_header="X-Forwarded-User",
        # Open WebUI creates every FIRST-seen trusted-header user in a locked
        # "pending" state needing a second, separate per-account approval inside
        # Open WebUI's own admin panel — on top of already being Hub-authenticated
        # to even reach this proxy. Force every new account straight to "user" so
        # anyone who can already reach this app can use it immediately, matching
        # every other app on the fleet (no second gate).
        sso_role_header="X-Forwarded-Role",
        sso_role_value="user",
        # Open WebUI has its own real, properly-branded PWA manifest+icons —
        # it lives on its own subdomain (ai.<domain>, via streamUrl() in the
        # Hub frontend), not the shared /apps/stream/ subpath every other web
        # app uses. Skip our synthetic PWA injection so those real assets are
        # served untouched instead of being replaced by a generic placeholder.
        native_pwa=True,
        # iOS Safari ignores the manifest.json icons above for "Add to Home
        # Screen" — it specifically wants an apple-touch-icon tag, which Open
        # WebUI's own HTML never emits. Point it at Open WebUI's own already-
        # real logo (no duplicate hosting needed).
        native_pwa_apple_icon="/static/logo.png",
        # Same shared network as Ollama — reach it by container name, no fixed port
        # needed. The --add-host pins the Hub's public hostname to its LAN IP so
        # the OpenAI connection below reaches the Hub router with a valid TLS cert
        # even though this node isn't a WireGuard peer.
        docker_args=["--network", "sm-llm-net"] + (
            ["--add-host", f"{config.HUB_HOST}:{config.HUB_INTERNAL_IP}"]
            if config.HUB_HOST and config.HUB_INTERNAL_IP
            and config.HUB_HOST != config.HUB_INTERNAL_IP else []),
        # NAS-backed: accounts/chats/uploads for ALL users live on the fleet NAS
        # (shared/open-webui-data), not this node's local Docker storage.
        # EXCEPT vector_db: Chroma hard-codes SQLite WAL mode, which deadlocks on
        # NFS (WAL needs shared-memory mmap coherency network filesystems can't
        # provide) — a local volume is nested over that one subdir. Docker mounts
        # sort by path depth, so the nested mount reliably lands on top.
        mounts=[Mount(name="open-webui-data", path="/app/backend/data", scope="shared",
                      storage="nfs"),
                Mount(name="open-webui-vectordb", path="/app/backend/data/vector_db",
                      scope="shared"),
                # The WHOLE fleet NAS export, same pattern Nextcloud already uses
                # (scope="root" — mount everything, let the app scope per-user
                # itself) — since this is one shared container for every Hub
                # user, there's no single "per-user" mount that would work here.
                # The NAS Files tool (containers/open-webui/tools/nas_files.py)
                # is what actually enforces per-user isolation at runtime, using
                # Open WebUI's own authenticated __user__ context — nothing about
                # this mount itself is user-scoped.
                Mount(name="nas", path="/nas", scope="root", storage="nfs")],
        env={
            # NOT "OLLAMA_BASE_URL": "http://sm-ollama:11434" any more. That
            # assumed Ollama always co-locates with Open WebUI on the same
            # node/docker daemon (reachable by container name over
            # sm-llm-net) — true when this was written, but Ollama is now a
            # genuine multi-node app (see App Definition Standard §2) that
            # can run on any capable node, not necessarily this one. Confirmed
            # live: with Ollama actually running on a different node, Open
            # WebUI's native Ollama connection tried "sm-ollama" and failed
            # DNS resolution outright, then (worse) still routed some chat
            # requests through that broken connection instead of the working
            # OpenAI one below — "Model 'x' was not found" even though the
            # Hub's own /v1/models correctly listed it. Disabling the native
            # Ollama integration entirely removes the ambiguity — the OpenAI
            # connection to the Hub's router is fleet-aware (any node, not
            # just this one) and already does everything the native
            # connection would, correctly.
            "ENABLE_OLLAMA_API": "false",
            "WEBUI_AUTH_TRUSTED_EMAIL_HEADER": "X-Forwarded-User",
            # Auto-activates every trusted-header account as role=user (never
            # left "pending") — see sso_role_header above.
            "WEBUI_AUTH_TRUSTED_ROLE_HEADER": "X-Forwarded-Role",
            "WEBUI_AUTH": "True",
            "WEBUI_SECRET_KEY": config.OPEN_WEBUI_SECRET_KEY,
            # webui.db lives on the NFS mount above: WAL mode would hang the app
            # at boot (observed: PID 1 asleep in wait_woken acquiring the shm
            # lock). DELETE journal mode uses plain POSIX locks, which NFSv4
            # handles correctly.
            "DATABASE_ENABLE_SQLITE_WAL": "false",
            # Hub LLM Router as an OpenAI connection: ONE endpoint that routes
            # each request to the best fleet node that has the model (online →
            # running → least loaded). New Ollama nodes join automatically.
            **({"OPENAI_API_BASE_URL": f"{config.HUB_URL}/api/fleet/llm/v1",
                "OPENAI_API_KEY": config.LLM_API_KEY}
               if config.HUB_URL and config.LLM_API_KEY else {}),
        },
    ),
    # OnlyOffice Document Server — catalogued as an alternative to Collabora for
    # Docs/Sheets/Slides, but intentionally NOT deployed yet: this stack needs
    # ~4GB+ free RAM (doc server + Postgres + RabbitMQ + Redis) the box doesn't
    # currently have headroom for. Nothing is pulled/built until someone
    # actually presses Start — same as every other app — so cataloguing it now
    # is free; just don't launch it until there's real headroom to spare.
    "onlyoffice": AppDef(
        id="onlyoffice",
        label="OnlyOffice",
        icon="globe",
        color="blue",
        desc="Docs/Sheets/Slides alternative to Collabora — needs ~4GB+ free "
             "RAM; verify headroom before starting.",
        image=config.ONLYOFFICE_IMAGE,
        # Pure upstream image with no build context: nothing can build it
        # locally, so the ONLY way it reaches a node is a pull. Without this,
        # spawn() skipped its pre-pull and left `docker run` to fetch the
        # image inside its own 120s cap — which a ~1GB image never makes, so
        # the first launch on any fresh node failed and rolled the pull back.
        auto_pull=True,   # public image — pre-pulled with a long timeout
        kind="web",
        mode="shared",        # one shared document server, like Collabora
        internal_port=80,
        gpu=False,
        mem_limit="4g",
        proxy_subpath="root",
        env={
            "DB_TYPE": "postgres",
            "DB_HOST": "db",
            "DB_PORT": "5432",
            "DB_NAME": "onlyoffice",
            "DB_USER": "onlyoffice",
            "DB_PWD": config.ONLYOFFICE_DB_PASSWORD,
            "AMQP_URI": "amqp://guest:guest@rabbitmq",
            "REDIS_SERVER_HOST": "redis",
            "JWT_ENABLED": "true",
            "JWT_SECRET": config.ONLYOFFICE_JWT_SECRET,
        },
        services=[
            Service(
                name="db",
                image=config.ONLYOFFICE_POSTGRES_IMAGE,
                env={
                    "POSTGRES_DB": "onlyoffice",
                    "POSTGRES_USER": "onlyoffice",
                    "POSTGRES_PASSWORD": config.ONLYOFFICE_DB_PASSWORD,
                },
                mounts=[Mount(name="onlyoffice-db", path="/var/lib/postgresql/data", scope="shared")],
                ready_cmd=["pg_isready", "-U", "onlyoffice"],
            ),
            Service(name="rabbitmq", image=config.ONLYOFFICE_RABBITMQ_IMAGE),
            Service(name="redis", image=config.ONLYOFFICE_REDIS_IMAGE),
        ],
    ),
}

# ── Per-node app library (which CATALOG apps are enabled here) ───────────────
_CATALOG_STATE = os.environ.get(
    "SM_CATALOG_STATE", os.path.expanduser("~/.sandos-sm/catalog.json"))


def _image_exists_any_daemon(tag: str) -> bool:
    for host in docker_backend.all_docker_hosts():
        args = ["docker"] + (["-H", host] if host else []) + ["image", "inspect", tag]
        try:
            if subprocess.run(args, capture_output=True, timeout=10).returncode == 0:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def _seed_enabled() -> list[str]:
    """First boot with no library state: enable exactly the built-ins whose
    image already exists on one of this node's daemons — an existing node
    keeps its working set across the upgrade, while a genuinely fresh
    install (no images yet) starts with an EMPTY library. Deliberately does
    NOT trust auto_pull/image_installed()'s always-true shortcut for public
    upstream images — that would seed ollama onto every fresh node."""
    out = []
    for app in CATALOG.values():
        for tag in filter(None, (app.image, app.packaged_image)):
            if _image_exists_any_daemon(tag):
                out.append(app.id)
                break
    return out


def _save_enabled() -> None:
    os.makedirs(os.path.dirname(_CATALOG_STATE), exist_ok=True)
    tmp = _CATALOG_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"enabled": [i for i in APPS if i in CATALOG]}, f, indent=2)
    os.replace(tmp, _CATALOG_STATE)


def _load_enabled() -> list[str]:
    try:
        with open(_CATALOG_STATE) as f:
            return [i for i in json.load(f).get("enabled", []) if i in CATALOG]
    except FileNotFoundError:
        return _seed_enabled()
    except Exception as e:  # noqa: BLE001
        # Corrupt state fails OPEN (full catalogue), never closed — a broken
        # file must not make every app vanish from a working node.
        print(f"[registry] catalog state unreadable, enabling all: {e}")
        return list(CATALOG)


APPS: dict[str, AppDef] = {i: CATALOG[i] for i in _load_enabled()}
if not os.path.exists(_CATALOG_STATE):
    _save_enabled()   # make the seed durable so later boots don't re-scan docker


def catalog_summary() -> list[dict]:
    """Every SHIPPED app + whether it's enabled on this node — the 'built-in
    app library' the Install-app modal offers."""
    return [{"id": a.id, "label": a.label, "icon": a.icon, "color": a.color,
             "desc": a.desc, "kind": a.kind, "gpu": a.gpu,
             "enabled": a.id in APPS}
            for a in CATALOG.values()]


def enable_app(app_id: str) -> dict:
    if app_id not in CATALOG:
        raise ValueError(f"{app_id!r} is not a built-in app")
    APPS[app_id] = CATALOG[app_id]
    _save_enabled()
    return {"ok": True, "enabled": True}


def disable_app(app_id: str) -> dict:
    """Hide a built-in app from this node's library. Refuses while an
    instance is running; the app's image and data volumes are untouched —
    re-enabling brings it back exactly as it was."""
    if app_id not in CATALOG:
        raise ValueError(f"{app_id!r} is not a built-in app")
    if app_id not in APPS:
        return {"ok": True, "enabled": False}
    if any(i["app_id"] == app_id and i["running"] for i in instances_summary()):
        raise ValueError("stop the app before removing it from the library")
    del APPS[app_id]
    _save_enabled()
    return {"ok": True, "enabled": False}


# Apps installed from Docker Hub on THIS node (App Definition Standard §11's
# user workflow — dockerhub_apps.py) join the catalogue at import time, which
# is deliberately BEFORE reconcile_from_docker() ever runs: a running
# hub-app container survives an SM restart and gets re-adopted exactly like
# a built-in app's would. dockerhub_apps must not import registry at module
# level (it defers via `from . import registry` inside functions) or this
# would be a circular import.
from . import dockerhub_apps as _dockerhub_apps  # noqa: E402
APPS.update(_dockerhub_apps.load_installed())


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
    from . import app_images
    inst = _instances.get((app_id, _eff(app_id, user)))
    if not inst or not docker_backend.running(inst.name, host=app_images.active_docker_host(app_id)):
        return "stopped"
    app = APPS.get(app_id)
    if not docker_backend.web_ready(inst.web_port, strict=bool(app and app.strict_ready),
                                     path=(app.ready_path if app else ""),
                                     bad_status=frozenset(app.ready_bad_status if app else ())):
        return "starting"
    return "active" if docker_backend.active_connections(inst.web_port) > 0 else "idle"


def launch(app_id: str, user: str) -> Instance:
    from . import app_images, busy
    if busy.is_busy():
        raise RuntimeError("this node is set to Busy — apps are paused to free up resources")
    if app_id not in APPS:
        raise KeyError(app_id)
    app = APPS[app_id]
    # OLLAMA_KEEP_ALIVE is read by Ollama at process start, so the owner's
    # saved setting has to be on the env at every launch — not only on the one
    # where they changed it (see ollama_mgr.set_keep_alive).
    if app_id == "ollama":
        from . import ollama_mgr
        app.env["OLLAMA_KEEP_ALIVE"] = ollama_mgr.get_keep_alive()
    host = app_images.active_docker_host(app_id)
    with _lock_for((app_id, _eff(app_id, user))):
        inst = _instance_for(app_id, user)
        if not docker_backend.running(inst.name, host=host):
            res = docker_backend.spawn(inst, app)
            if res.returncode != 0:
                if app.services:
                    docker_backend.teardown(inst.name, app, host=host)  # clean a partial stack
                raise RuntimeError(res.stderr.strip() or "docker run failed")
            # spawn() implicitly pulls a not-yet-local upstream image (e.g.
            # OnlyOffice's "Install" button) — bust the cache so the very
            # next poll reflects "installed" instead of waiting out the TTL.
            _INSTALLED_CACHE.pop(app_id, None)
        return inst


def stop(app_id: str, user: str) -> None:
    from . import app_images
    name = instance_name(app_id, user)   # shared-aware
    app = APPS.get(app_id)
    host = app_images.active_docker_host(app_id)
    if app:
        docker_backend.teardown(name, app, host=host)   # primary + any sidecars + network
    else:
        docker_backend.stop(name, host=host)
    _clear_staging_for(name, user)


def _clear_staging_for(instance_name: str, user: str) -> None:
    """Drop this instance's staged files as it stops.

    The value of staging is that exposure ENDS — files left behind after a job
    finishes are exactly the standing access the whole design exists to avoid,
    and "we meant to clean that up" is not a security property. Runs on the NAS
    host only, where the staging tree actually lives.

    Best effort and never raises: failing to tidy up must not turn a successful
    stop into an error the caller has to handle.
    """
    try:
        if not config.NAS_ENABLED or config.NAS_HOST != config.LAN_IP:
            return                       # not the NAS host — nothing local to clear
        from . import nas_staging
        # config.NODE_NAME, not the raw env var: config already falls back to
        # the hostname, whereas reading os.environ directly returns None in any
        # process that did not inherit the unit's EnvironmentFile and silently
        # cleared a DIFFERENT directory — leaving the real staged files in place,
        # which is the one outcome this cleanup exists to prevent.
        node = config.NODE_NAME
        if user:
            nas_staging.collect(node, instance_name, user)
        nas_staging.clear(node, instance_name)
    except Exception as e:   # noqa: BLE001
        print(f"[nas] staging cleanup for {instance_name} skipped: {e}")


def stop_all() -> dict:
    """Tear down every currently-running instance across every app/user —
    the Busy-mode "free up this machine right now" action. Best-effort: one
    instance failing to stop doesn't block the rest, and doesn't stop the
    node from being marked Busy — the errors are just reported back."""
    stopped, errors = [], []
    for entry in instances_summary():
        if not entry["running"]:
            continue
        try:
            stop(entry["app_id"], entry["user"])
            stopped.append(entry["name"])
        except Exception as e:  # noqa: BLE001
            errors.append({"name": entry["name"], "error": str(e)})
    return {"stopped": stopped, "errors": errors}


def stop_app(app_id: str) -> dict:
    """Tear down every currently-running instance of ONE app on this node,
    regardless of which user owns it. The plain stop(app_id, user) is scoped
    to a single (app_id, user) instance — correct for a user stopping their
    own copy, but wrong for the Fleet page's admin "stop whatever's running
    here" toggle: an admin's own per-user placement can easily have nothing
    to do with whichever user's instance is actually running (or the Hub's
    separate placement bookkeeping can simply be stale/never set for it),
    so the generic user-scoped stop silently finds nothing to act on even
    though the container is genuinely running. This instead goes straight to
    real docker state (instances_summary(), same source the Fleet page's own
    "is it running" toggle reads) and stops every match, independent of any
    placement pointer. Best-effort, same shape as stop_all()."""
    stopped, errors = [], []
    for entry in instances_summary():
        if entry["app_id"] != app_id or not entry["running"]:
            continue
        try:
            stop(app_id, entry["user"])
            stopped.append(entry["name"])
        except Exception as e:  # noqa: BLE001
            errors.append({"name": entry["name"], "error": str(e)})
    return {"stopped": stopped, "errors": errors}


def instances_summary() -> list[dict]:
    """Every currently-tracked instance across all apps, with its container name
    and running state — used by /api/sm/apps/stats (Fleet page's per-app
    breakdown) to join registry state against `docker stats`."""
    from . import app_images
    return [
        {"app_id": app_id, "user": user, "name": inst.name,
         "running": docker_backend.running(inst.name, host=app_images.active_docker_host(app_id))}
        for (app_id, user), inst in _instances.items()
    ]


def source_tree_ready(app: AppDef) -> bool:
    """True if this app has no binds, or every bind's host_path exists and is
    non-empty. False = the image alone isn't enough to actually run this app —
    a bind-mount 'live source tree' app (webcad/helix/openfoamgui) needs its
    real code checked out at that host path too, not baked into the image."""
    return all(os.path.isdir(p) and os.listdir(p) for p, _ in app.binds)


def dev_source_commit(app: AppDef) -> str | None:
    """Current git commit of this app's dev-source checkout, if THIS node
    actually has it (source_tree_ready()) and it's a git repo — None
    otherwise, including on a node that merely has the AppDef entry but not
    the real checkout (see App Definition Standard §8.1: binds is pinned to
    one specific machine, never assume it's present just because the entry
    exists). Used to compare against a packaged build's baked-in
    sandos.source_commit label (app_variants.image_source_commit()) so a
    node running the packaged image can tell whether it's behind the current
    dev source — a check, never an automatic rebuild."""
    if not app.binds or not source_tree_ready(app):
        return None
    path = app.binds[0][0]
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:  # noqa: BLE001
        return None


def dev_source_remote_status(app: AppDef) -> dict | None:
    """Compares this node's dev checkout against its GitHub `origin`
    upstream branch. `git fetch` only updates remote-tracking refs, never
    the working tree, so this is safe against a dirty/mid-edit repo. None if
    there's no checkout here, no `origin` remote (e.g. CNC_Controller/helix
    is local-only), or no upstream tracking branch configured.

    Returns {"remote_commit", "ahead", "behind"} — `behind` is what counts
    as staleness (GitHub has commits this checkout hasn't pulled); `ahead`
    (unpushed local work) is normal mid-development, not "outdated"."""
    if not app.binds or not source_tree_ready(app):
        return None
    path = app.binds[0][0]
    try:
        r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        f = subprocess.run(["git", "-C", path, "fetch", "origin"],
                           capture_output=True, text=True, timeout=20)
        if f.returncode != 0:
            return None
        ref = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True, text=True, timeout=5)
        upstream = ref.stdout.strip() if ref.returncode == 0 else None
        if not upstream:
            return None
        remote_commit = subprocess.run(
            ["git", "-C", path, "rev-parse", upstream],
            capture_output=True, text=True, timeout=5).stdout.strip() or None
        counts = subprocess.run(
            ["git", "-C", path, "rev-list", "--left-right", "--count", f"HEAD...{upstream}"],
            capture_output=True, text=True, timeout=10)
        ahead = behind = None
        if counts.returncode == 0 and counts.stdout.strip():
            parts = counts.stdout.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
        return {"remote_commit": remote_commit, "ahead": ahead, "behind": behind}
    except Exception:  # noqa: BLE001
        return None


def manual_install_hint(app: AppDef) -> dict:
    """The honest manual fallback shown when no peer node has this app yet —
    e.g. the very first node in the mesh to ever want it. Never a silent
    dead end: always names the exact command to run by hand."""
    from . import app_images
    hint: dict = {}
    if app.build_context:
        dockerfile = f"-f {app.build_dockerfile} " if app.build_dockerfile else ""
        hint["build_cmd"] = f"docker build {dockerfile}-t {app_images._image_tag(app)} {app.build_context}"
    elif not getattr(app, "auto_pull", False):
        hint["build_cmd"] = f"docker pull {app.image}"
    if app.binds:
        hint["source_note"] = (
            "Source tree must also exist at: " + ", ".join(p for p, _ in app.binds) +
            " — no automated fallback for this; copy it there manually.")
    return hint


def image_installed(app: AppDef) -> bool:
    """Whether this app's image has ever actually been pulled/built —
    distinguishes that from a normal ready-to-launch stopped app (both
    report status() == "stopped"). Lets the frontend show "Uninstalled"/
    "Install" instead of the misleading "Available"/"Start" for a
    catalogued-but-never-deployed app (e.g. OnlyOffice). Cached — this is
    called from endpoints polled every few seconds, and the underlying check
    is a subprocess `docker image inspect`, which only ever needs to be
    re-checked right after an explicit build/pull/move.

    Apps with auto_pull=True are public upstream images; Docker pulls them
    transparently on first `docker run`, so always report them as installed."""
    if getattr(app, "auto_pull", False):
        return True
    from . import app_images
    cached = _INSTALLED_CACHE.get(app.id)
    now = time.monotonic()
    if cached and now - cached[0] < _INSTALLED_TTL:
        return cached[1]
    installed = app_images._image_exists(
        app_images._image_tag(app), app_images.active_docker_host(app.id))
    _INSTALLED_CACHE[app.id] = (now, installed)
    return installed


def list_for_user(user: str) -> list[dict]:
    """App catalogue with this user's per-app status + URL (if running)."""
    out = []
    for app in APPS.values():
        st = status(app.id, user)
        inst = get_instance(app.id, user)
        out.append({
            "id": app.id, "label": app.label, "icon": app.icon,
            "color": app.color, "desc": app.desc, "kind": app.kind,
            "status": st, "image_installed": image_installed(app),
            "url": url_for(inst) if (inst and st != "stopped") else None,
        })
    return out


def reconcile_from_docker() -> None:
    """On startup, re-adopt any existing sm- containers using their ACTUAL
    published ports (not a fresh slot) so the in-memory map matches reality and
    new launches don't collide with a running instance's ports. Checks EVERY
    active daemon (the default one + any USB-hosting drive's secondary
    dockerd) — an app whose image lives on a USB drive is invisible to the
    default daemon entirely."""
    for docker_host in docker_backend.all_docker_hosts():
        for name in docker_backend.list_sm_containers(host=docker_host):
            web_port = docker_backend.published_web_port(name, host=docker_host)
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
