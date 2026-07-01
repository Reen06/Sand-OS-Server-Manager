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
    # keep-alive after disconnect before the instance is stopped; 0 = close right away
    keepalive_seconds: int = 600

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
