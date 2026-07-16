"""Core data models: App Definitions, Mounts and Instances.

An App Definition is the declarative 'wiring' for an app — what image to run,
whether it's a streamed GPU desktop or a native web UI, and (crucially) what
data volumes it mounts. A Mount is one such volume + where it lands in the
container and whether it's the user's private data or a shared library — this is
the NAS layer. An Instance is one running (or allocated) copy of an app.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class AppVariant:
    """One installable version of an app — a specific image build/pull the user
    can switch to. Presented in the app tile's 'Manage version' menu, grouped by
    channel (stable vs development)."""
    id: str                 # "stable" | "weekly-dev" | future custom ids
    label: str              # "Stable 1.1.1" | "Latest weekly dev build"
    channel: str = "stable" # "stable" | "dev" — dev entries only show once the
                            # per-app dev-channel toggle is on
    image_tag: str = ""     # local docker tag this variant installs/runs as
    kind: str = "build"     # "build" (docker build from build_context) | "pull"
    build_args: dict[str, str] = field(default_factory=dict)
    source: str = ""        # image ref to pull, when kind == "pull"
    resolver: str = ""      # name of a resolve_* fn in app_variants.py that
                            # computes build_args/source dynamically before
                            # install (e.g. "freecad-weekly" looks up the
                            # latest weekly release from GitHub each install)


@dataclass
class Service:
    """A sidecar container in an app's stack — the DB/cache/etc. behind a web app
    (e.g. MariaDB + Redis for Nextcloud). Runs on the app's private network,
    reachable by the primary container at `name` as a hostname, and is NOT
    published to the host."""
    name: str                                    # network alias / hostname (e.g. "db")
    image: str
    cmd: list[str] = field(default_factory=list)  # extra args appended to the image
    env: dict[str, str] = field(default_factory=dict)
    mounts: list["Mount"] = field(default_factory=list)
    # Optional readiness probe run via `docker exec` (returns 0 when ready). The
    # primary container isn't started until every service with a probe passes —
    # e.g. wait for MariaDB before Nextcloud installs against it.
    ready_cmd: list[str] = field(default_factory=list)
    # Escape hatch: extra raw `docker run` flags for this sidecar (e.g. Collabora's
    # `--cap-add MKNOD`). Empty for every existing service — opt-in only.
    docker_args: list[str] = field(default_factory=list)


@dataclass
class Mount:
    """A data volume attached to an app — the unit of the NAS layer.

    `name` is the logical volume name. It resolves to a real docker volume
    depending on `scope`:
      - per-user:  sm-{app}-{user}-{name}   (private to one user)
      - shared:    sm-shared-{name}         (one volume many apps/users can mount)
    `path` is where it mounts inside the container; `ro` mounts it read-only
    (e.g. a media app that reads, but does not write, the shared library)."""
    name: str
    path: str
    scope: str = "per-user"      # per-user | shared
    ro: bool = False
    # local  = a Docker volume on the node (fast, node-local, not shared).
    # nfs    = the fleet NAS over NFSv4 — the SAME bytes on every node, no
    #          duplication; per-user → users/{user}, shared → shared/{name}.
    # usb    = bind-mounted onto an assigned USB drive (by UUID) instead of the
    #          node's own disk — frees node space, and follows the drive if it's
    #          moved to another Server Manager node. This is a per-install
    #          RUNTIME override (see app_storage.py), not set here on the AppDef —
    #          `storage` above is just the mount's default/fallback.
    storage: str = "local"


@dataclass
class AppDef:
    id: str
    label: str
    icon: str
    color: str
    desc: str
    image: str
    # streamed = Selkies/WebRTC GPU desktop (FreeCAD).  web = native HTTP UI the
    # proxy simply reverse-proxies (Filebrowser, Jellyfin, Immich…).
    kind: str = "streamed"
    mode: str = "per-user"               # per-user | shared | ephemeral
    # Container port the proxy/readiness talks to (8080 Selkies, 80 Filebrowser…).
    internal_port: int = 8080
    gpu: bool = False
    # ── streamed-only tuning ──────────────────────────────────────────────────
    encoder: str = "nvh264enc"           # nvh264enc (NVENC) | x264enc (CPU)
    # Auto-resize the remote display to the browser window. Fills the viewport,
    # but breaks Selkies' client-rendered cursor at odd sizes — default off so the
    # cursor stays visible (the browser still scales the 1080p stream to fit).
    resize: bool = False
    # ── data + env ────────────────────────────────────────────────────────────
    mounts: list[Mount] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # Sidecar containers (DB, cache…) started with the app on a private network.
    services: list[Service] = field(default_factory=list)
    # How the proxy maps the subpath for a web app:
    #   "forward" — send the app the external-prefixed path; the app knows its
    #               baseURL and strips it (Filebrowser).
    #   "root"    — strip to the container root; the app serves at root and
    #               rewrites its own outgoing links (Nextcloud via OVERWRITEWEBROOT).
    proxy_subpath: str = "forward"
    # If set, the proxy injects this header (with the authenticated Hub username)
    # and strips any client-supplied copy — trusted-header SSO (Nextcloud:
    # "Remote-User" → user_saml environment-variable backend).
    sso_header: str | None = None
    # Paired with sso_header: some apps (Open WebUI) create every trusted-header
    # user in a locked "pending" state needing manual per-account admin approval
    # — anyone the Hub already let through this proxy should just be able to use
    # the app, not hit a second approval gate app-side. When set, the proxy also
    # injects sso_role_value under this header name so the app auto-activates
    # the account instead of leaving it pending.
    sso_role_header: str | None = None
    sso_role_value: str = "user"
    # Apps under the shared /apps/stream/{id}/ subpath get OUR synthetic
    # PWA manifest+icon injected (see proxy.py _inject_pwa) so "Open in
    # window" installs each as its OWN distinctly-iconed app — most apps have
    # no real manifest of their own at all under that subpath. An app given
    # its own real subdomain (Open WebUI, via streamUrl() in the Hub
    # frontend) already ships genuine, properly-branded manifest+icons at
    # that origin; overriding them replaced the real logo with a generic
    # placeholder. True here skips the injection so the app's native PWA
    # assets are served untouched.
    native_pwa: bool = False
    # keep-alive after disconnect before the instance is stopped; 0 = close right away
    keepalive_seconds: int = 600
    # docker --memory hard cap (e.g. "3g"); "" = uncapped. Prevents one runaway
    # app from OOMing the whole node.
    mem_limit: str = ""
    # Extra host→container bind mounts as (host_path, container_path). Unlike Mounts
    # (the NAS/volume layer), these bind a real host directory straight in — used for
    # a DEV app that runs live from a bind-mounted source tree (edit on the host →
    # rebuild/reload in the container). Empty for every normal app.
    binds: list[tuple[str, str]] = field(default_factory=list)
    # Installable versions this app offers (empty = no version manager UI; the
    # app just always runs `image` as today). build_context is the Dockerfile
    # directory for "build"-kind variants, relative to the SM repo root — an
    # ABSOLUTE path is also fine (app_variants.py's os.path.join keeps it
    # unchanged) for an app whose source lives outside this repo entirely.
    variants: list[AppVariant] = field(default_factory=list)
    build_context: str = ""
    # Only set when the Dockerfile does NOT live inside build_context itself
    # (e.g. EngineeringPaper.xyz: the Dockerfile lives in THIS repo's
    # containers/, but the build context is the separate source checkout) —
    # passed as `docker build -f <this>`. Empty = Dockerfile is the plain
    # "<build_context>/Dockerfile" default, which covers every other app.
    build_dockerfile: str = ""
    # docker repo name (no tag) covering all this app's variant images, for
    # disk-usage listing — e.g. "freecad-streamer" for both "dev" and
    # "weekly-dev" tags.
    image_family: str = ""
    # Escape hatch: extra raw `docker run` flags for the PRIMARY container.
    # Empty for every existing app — opt-in only.
    docker_args: list[str] = field(default_factory=list)
    # Apps with auto_pull=True are pure upstream Docker Hub / registry images
    # that don't need a local build step — Docker pulls them automatically on
    # first `docker run`. The frontend shows "Start" (not "Install") for these
    # even before the image is locally cached, since the pull is transparent.
    auto_pull: bool = False
    # True for a live-dev app whose process binds its port immediately but
    # keeps serving 4xx/5xx placeholders until its own first build/watch
    # cycle finishes (webcad/helix's vite/tsx --watch dev servers) — a plain
    # "did it answer at all" readiness check falsely reports ready during
    # that window, so the dashboard opens straight into a "not found" page.
    # False (default) for every app whose readiness genuinely IS "responded
    # at all" (e.g. Nextcloud legitimately 401s/302s at "/" once fully up).
    strict_ready: bool = False

    @property
    def streamed(self) -> bool:
        return self.kind == "streamed"


@dataclass
class Instance:
    """A port/volume allocation for one (app, user). 'status' is computed live
    from Docker, not stored here."""
    app_id: str
    user: str
    slot: int
    name: str
    web_port: int
    turn_port: int
    relay_min: int
    relay_max: int
    volume: str
