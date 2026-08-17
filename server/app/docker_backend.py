"""Docker backend — spawn / stop / inspect FreeCAD-streamer instances via the
docker CLI (no extra SDK dependency). Mirrors the proven run-lan.sh parameters,
but with per-instance ports so concurrent instances don't collide."""
from __future__ import annotations
import calendar
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from .models import AppDef, Instance
from . import config, nas_scope


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't follow redirects during readiness — a 3xx means the server is up.
    (Nextcloud 302-redirects to https; following it into TLS on the plain port
    would spuriously read as 'not ready'.)"""
    def redirect_request(self, *a, **k):
        return None


_ready_opener = urllib.request.build_opener(_NoRedirect)


def web_ready(port: int, strict: bool = False, path: str = "",
              bad_status: frozenset[int] = frozenset()) -> bool:
    """True once the instance's web server answers ANY HTTP status (200/302/401…)
    — that's enough for most apps (Nextcloud legitimately 401s/302s at "/" once
    genuinely up). `strict=True` (AppDef.strict_ready) additionally requires a
    real 2xx: a live-dev app (vite/webpack --watch) binds its port instantly
    but serves a 4xx placeholder until its first build finishes, which the
    lenient check above would wrongly call "ready".

    `path` (AppDef.ready_path) overrides which path gets probed — for an app
    whose root is served instantly by a fast front-end web server sitting in
    front of a slower-starting real backend (ParaView's Apache vs. its
    wslink launcher), root alone reports ready long before the app can
    actually do anything. Any response at all still counts as ready here
    (same as the root-path case) — only a connection failure (nothing
    listening yet) counts as not-ready.

    `bad_status` (AppDef.ready_bad_status) is for the awkward case neither
    `strict` handles: a genuinely-ready endpoint whose steady-state
    "answered" response ISN'T a 2xx (ParaView's launcher correctly 400s a
    plain GET — wrong method — once it's really listening), but which also
    has a SPECIFIC error status meaning "truly not ready yet" that must not
    be waved through by the lenient default (its 503, from Apache's own
    mod_proxy failing to reach the backend at all). `strict` can't express
    this (it demands 2xx, which this endpoint never legitimately returns);
    list the specific not-ready status(es) here instead."""
    try:
        resp = _ready_opener.open(f"http://127.0.0.1:{port}/{path}", timeout=2)
        if resp.status in bad_status:
            return False
        return not strict or 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code in bad_status:
            return False
        if strict:
            return False  # the whole point: a 404/500 mid-build must NOT count as ready
        return True  # 3xx redirect, 401 auth challenge, etc. — the server is up
    except Exception:
        return False


def _docker(args: list[str], timeout: int = 60, host: str | None = None) -> subprocess.CompletedProcess:
    prefix = ["-H", host] if host else []
    return subprocess.run(["docker", *prefix, *args], capture_output=True, text=True, timeout=timeout)


def running(name: str, host: str | None = None) -> bool:
    r = _docker(["inspect", "-f", "{{.State.Running}}", name], timeout=10, host=host)
    return r.returncode == 0 and r.stdout.strip() == "true"


def exists(name: str, host: str | None = None) -> bool:
    return _docker(["inspect", name], timeout=10, host=host).returncode == 0


def stop(name: str, host: str | None = None, grace: int = 10) -> None:
    """Ask the container to shut down, then remove it.

    `docker rm -f` alone is an instant SIGKILL. That is a hard power-cut on a
    database sidecar, and it means an app that writes its state on exit never
    gets to. `docker stop` sends SIGTERM, waits `grace` seconds, and only then
    kills — so a clean shutdown happens when the app is capable of one, and a
    hung container still goes away.

    Not every app benefits: FreeCAD was measured exiting on SIGTERM without
    writing its preferences at all, which is why it also restores them from its
    own backups at launch. The graceful stop is the correct default; that
    workaround remains its fallback.

    A container with an NFS-backed volume can take well over 30s to REMOVE if
    the mount is momentarily slow (observed live: sm-nextcloud-db's removal
    alone timed out at 30s during ordinary use, no error on the NFS side at
    all). The removal timeout keeps that headroom on top of the grace period.
    """
    _docker(["stop", "-t", str(max(0, grace)), name], timeout=max(0, grace) + 30, host=host)
    _docker(["rm", "-f", name], timeout=90, host=host)


def list_sm_containers(host: str | None = None) -> list[str]:
    """All Server-Manager-owned containers (running or not) on ONE daemon."""
    r = _docker(["ps", "-a", "--filter", "name=^sm-", "--format", "{{.Names}}"], timeout=10, host=host)
    return [n for n in r.stdout.split() if n.startswith("sm-")]


def all_docker_hosts() -> list[str | None]:
    """Every daemon that might be running SM containers right now: the
    default local one (None) plus every currently app-hosting-enabled,
    mounted USB drive's secondary dockerd. reconcile_from_docker() and the
    Fleet page's stats need to check ALL of these, not just the default."""
    from . import usb_storage
    hosts: list[str | None] = [None]
    for d in usb_storage.list_devices():
        if d.get("app_hosting") and d.get("mountpoint"):
            host = usb_storage.docker_host_for(d["uuid"])
            if host:
                hosts.append(host)
    return hosts


# ── per-container resource stats (Fleet page's per-app breakdown) ─────────────
_MEM_UNITS = {  # docker's human units -> MB multiplier, longest suffix first
    "tib": 1024 * 1024, "tb": 1024 * 1024,
    "gib": 1024, "gb": 1024,
    "mib": 1, "mb": 1,
    "kib": 1 / 1024, "kb": 1 / 1024,
    "b": 1 / (1024 * 1024),
}


def _parse_percent(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.strip().rstrip("%"))
    except ValueError:
        return None


def _parse_mem_value_mb(token: str) -> float | None:
    token = token.strip()
    for suffix in sorted(_MEM_UNITS, key=len, reverse=True):
        if token.lower().endswith(suffix):
            try:
                return float(token[:-len(suffix)].strip()) * _MEM_UNITS[suffix]
            except ValueError:
                return None
    return None


def _parse_mem_usage(s: str | None) -> tuple[float | None, float | None]:
    if not s or "/" not in s:
        return None, None
    used_s, limit_s = s.split("/", 1)
    return _parse_mem_value_mb(used_s), _parse_mem_value_mb(limit_s)


_STATS_SETTLE_SECONDS = 20  # see _started_at()'s docstring


def _started_at(names: list[str], host: str | None = None) -> dict[str, float]:
    """Each container's start time as epoch seconds, batched into one `docker
    inspect` call. Used to hold back a just-launched container's CPU% until
    it's had a few seconds to settle: `docker stats --no-stream`'s own
    reading is genuinely misleading right after start — dependency installs/
    builds briefly peg a core, and that one-off burst reads no differently
    from sustained load, showing e.g. 114% CPU for an app that's actually
    idle a few seconds later."""
    if not names:
        return {}
    r = _docker(["inspect", "--format", "{{.Name}}|{{.State.StartedAt}}", *names],
               timeout=10, host=host)
    if r.returncode != 0:
        return {}
    out: dict[str, float] = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        name, started = line.split("|", 1)
        name = name.lstrip("/")
        try:
            # Docker's RFC3339 timestamp has nanosecond precision; strptime
            # only handles microseconds, so truncate to the first 26 chars
            # ("YYYY-MM-DDTHH:MM:SS.dddddd") — plenty of precision for an
            # age-in-seconds check.
            ts = time.strptime(started[:26], "%Y-%m-%dT%H:%M:%S.%f")
            out[name] = calendar.timegm(ts)  # ts is UTC; timegm (not mktime) treats it as such
        except ValueError:
            continue
    return out


def stats(names: list[str], host: str | None = None) -> dict[str, dict]:
    """`docker stats` snapshot for the given (running) container names on ONE
    daemon -> {name: {cpu_percent, mem_used_mb, mem_limit_mb, mem_percent,
    settling}}. Skipped entirely (returns {}) if `names` is empty — `docker
    stats` with no name args would otherwise snapshot EVERY container on
    that daemon. `cpu_percent` is None (settling=True) for the first
    _STATS_SETTLE_SECONDS after a container starts — see _started_at()."""
    if not names:
        return {}
    r = _docker(["stats", "--no-stream", "--format", "{{json .}}", *names], timeout=15, host=host)
    if r.returncode != 0:
        return {}
    started = _started_at(names, host=host)
    now = time.time()
    out: dict[str, dict] = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        name = d.get("Name") or d.get("Container")
        if not name:
            continue
        mem_used, mem_limit = _parse_mem_usage(d.get("MemUsage"))
        start = started.get(name)
        settling = start is not None and (now - start) < _STATS_SETTLE_SECONDS
        out[name] = {
            "cpu_percent": None if settling else _parse_percent(d.get("CPUPerc")),
            "mem_used_mb": mem_used,
            "mem_limit_mb": mem_limit,
            "mem_percent": _parse_percent(d.get("MemPerc")),
            "settling": settling,
        }
    return out


def published_web_port(name: str, host: str | None = None) -> int | None:
    """The SM-assigned localhost web port for a container, independent of its
    internal port (8080 for Selkies/Filebrowser, 80 for Nextcloud). Finds the
    127.0.0.1 binding whose host port is in the SM web range. Returns None for
    sidecars (DB/cache) — they publish nothing — so reconcile skips them."""
    r = _docker(["inspect", "-f", "{{json .NetworkSettings.Ports}}", name], timeout=10, host=host)
    try:
        ports = json.loads(r.stdout or "null")
    except (ValueError, TypeError):
        return None
    if not ports:
        return None
    lo = config.WEB_PORT_BASE
    hi = config.WEB_PORT_BASE + config.SLOT_COUNT
    for binds in ports.values():
        for b in (binds or []):
            if b.get("HostIp") in ("127.0.0.1", "::1"):
                try:
                    hp = int(b["HostPort"])
                except (ValueError, KeyError, TypeError):
                    continue
                if lo <= hp < hi:
                    return hp
    return None


def active_connections(web_port: int) -> int:
    """Heuristic for Active vs Idle: count established TCP connections to the
    instance's published web port on the host."""
    try:
        r = subprocess.run(["ss", "-Htn", "state", "established",
                            f"( sport = :{web_port} )"],
                           capture_output=True, text=True, timeout=5)
        return len([ln for ln in r.stdout.splitlines() if ln.strip()])
    except Exception:
        return 0


def network_name(name: str) -> str:
    """Private network for an app's stack (primary + sidecars)."""
    return f"{name}-net"


def _resolve_docker_args(app) -> list[str]:
    """An app's extra docker flags, allowing entries to be computed at launch.

    Most are constants and stay plain strings. A few depend on a setting that
    can change while the SM is running — Ollama's port binding follows its LAN
    toggle — and the App Definitions are built once at import, so a literal
    list there would freeze the value read at startup and quietly ignore every
    later change. Any callable in the list is invoked here instead, at the
    moment the container is actually created, and may return one flag or
    several. A callable that raises is skipped rather than taking the whole
    launch down with it: a setting that cannot be read should cost that flag,
    not the app.
    """
    out: list[str] = []
    for item in getattr(app, "docker_args", []) or []:
        if callable(item):
            try:
                produced = item()
            except Exception:  # noqa: BLE001
                continue
            if produced:
                out.extend(produced if isinstance(produced, (list, tuple)) else [produced])
        else:
            out.append(item)
    return out


def _ensure_network(net: str, host: str | None = None) -> None:
    if _docker(["network", "inspect", net], timeout=10, host=host).returncode != 0:
        _docker(["network", "create", net], timeout=15, host=host)


def _safe(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def _usb_target(app_id: str, user: str, m) -> str:
    """Subpath under a USB drive's own root for this (app, user-or-shared,
    mount) — parallel to _nfs_target's naming, but on the drive itself rather
    than the fleet NAS export. Nested under the same visible "SandOS/" folder
    every SandOS-managed thing on a drive lives in (see usb_storage.py /
    app_images.py) — personal files elsewhere on the drive are never touched."""
    return f"SandOS/data-mounts/{_safe(app_id)}/{_safe(user)}/{_safe(m.name)}"


def usb_volume_name(uuid: str, app_id: str, user: str, m) -> str:
    return f"sm-usb-{_safe(uuid)}-{_safe(app_id)}-{_safe(user)}-{_safe(m.name)}"


def ensure_usb_volume(uuid: str, app_id: str, user: str, m, host: str | None = None) -> str:
    """Ensure a bind-backed docker volume onto an assigned USB drive's own
    filesystem, in the given daemon (host=None = default local — the normal
    case; a non-None host is used when the APP'S IMAGE also lives on USB, so
    its data volume must be created in that same daemon's storage). Fails
    loudly if the drive isn't mounted right now — deliberate: an app
    depending on a USB mount must refuse to start pointed at nothing, not
    silently spawn against an empty local volume."""
    from . import usb_storage
    mountpoint = usb_storage.mountpoint_for(uuid)
    if not mountpoint:
        raise RuntimeError(
            "that USB drive isn't plugged in / mounted right now — plug it in "
            "(or re-assign this mount to local/NFS storage)")
    usb_storage.ensure_sandos_readme(mountpoint)
    subpath = _usb_target(app_id, user, m)
    abs_path = os.path.join(mountpoint, subpath)
    os.makedirs(abs_path, exist_ok=True)
    vol = usb_volume_name(uuid, app_id, user, m)
    if _docker(["volume", "inspect", vol], timeout=10, host=host).returncode != 0:
        _docker(["volume", "create", "--driver", "local", "--opt", "type=none",
                 "--opt", "o=bind", "--opt", f"device={abs_path}", vol], timeout=15, host=host)
    return vol


def nfs_volume_name(user: str, m, app_id: str = "") -> str:
    return _nfs_target(user, m, app_id=app_id)[1]


def ensure_nfs_volume(user: str, m, host: str | None = None, app_id: str = "") -> str:
    """Public wrapper — app_storage.py resolves/creates NFS-backed volumes the
    same way spawn()'s _mount_args does internally."""
    return _ensure_nfs(user, m, host=host, app_id=app_id)


# Returned as the volume name when a mount must not be attached at all, as
# distinct from "" which means "fall back to a node-local volume".
_SKIP_MOUNT = "\x00skip"


# ── Mesh NAS (SeaweedFS) ─────────────────────────────────────────────────────
# Every node mounts the mesh NAS locally, so an app's NAS-backed mount is now a
# plain bind of a local path rather than an NFS volume pointed at one server.
#
# That is not merely simpler. The NFS volume embedded ONE server's address, so
# every app on every machine read through that single box — and moving the NAS
# meant recreating every volume in the fleet. A local mount reads chunks from
# whichever volume servers hold them, so the data path is direct and moving
# storage no longer touches the apps at all.
MESH_MOUNT = config.NAS_MESH_MOUNT     # kept as a name: widely referenced below


def _mesh_available() -> bool:
    """Is the mesh NAS actually mounted here? (see config.mesh_mounted)"""
    return config.mesh_mounted()


def _mesh_is_scoped() -> bool:
    """Is this node's mesh mount rooted at its own staging directory?

    A brokered node should mount `/nas/staging/<node>` rather than `/nas`, so
    the rest of the NAS is absent from its filesystem entirely — not merely
    unmounted by policy, but genuinely not there for anyone who logs into the
    box. The absence of the fleet tree's own top-level directories is what
    distinguishes the two, and it is observed rather than declared: a setting
    saying "I am scoped" could be wrong, whereas a missing `users/` cannot be.
    """
    # A node with no mount at all is not "scoped" — it has nothing. Without
    # this the absent users/ directory read as evidence of scoping, so a node
    # holding pushed files was told its files lived under a mount it does not
    # have, and every path then failed the mount check. Presence of the mount
    # is the precondition, not an afterthought.
    if not _mesh_available():
        return False
    try:
        return not os.path.isdir(os.path.join(MESH_MOUNT, config.NAS_USERS_SUBPATH))
    except OSError:
        return False        # unreadable mount: assume full, which is the safe
                            # assumption here (it keeps paths under staging/)


def _mesh_path(user: str, m, app_id: str = "") -> str | None:
    """Where this mount lives inside the mesh NAS, or None if it has no place.

    BROKERED (app-only) NODES ARE HANDLED FIRST, and that ordering is the whole
    point. Trust used to be enforced by what a node could physically reach: an
    app-only node had no mount, so asking for `users/<name>` simply failed. Once
    every node gained the mesh mount that stopped being true — the path resolves
    perfectly well now — and this function, which knows nothing about trust,
    would happily hand back the user's whole home, other people's shares, and
    the NAS root. Confirmed live on a node the Hub had marked app-only.

    So the scope has to be applied here, in code, rather than left to the
    filesystem. An app-only node gets exactly one thing: the files staged for
    THIS app instance. Everything else returns None, which the caller reads as
    "no NAS home for this mount".
    """
    staging = nas_scope.staging_name()
    if staging:
        # The user's files mount becomes the staging directory for this
        # instance — the files someone deliberately handed to this app, and
        # nothing else. Note the instance, not just the app: two apps on one
        # node cannot see each other's, and neither can a second user's.
        if m.scope == "per-user" and m.name == "home":
            return _staging_instance_dir(staging, app_id, user)
        # An app's own WORK on an app-only node (pictures it generates, files
        # you hand it) also belongs in staging rather than a node-local volume.
        # A local volume would strand it: nothing collects it when access is
        # revoked, so the results would sit on an untrusted machine indefinitely
        # and never reach the person who asked for them. Each mount gets its own
        # subdirectory under `work/`, which is the part the container sees —
        # `collected/`, its sibling, is deliberately not mounted (see
        # output_sweep).
        if m.scope == "per-user" and getattr(m, "nas_path", ""):
            base = _staging_instance_dir(staging, app_id, user)
            if not base:
                return None
            return os.path.join(base, "work", _staging_leaf(m))
        # A named per-user mount is this app's own settings. Those stay
        # node-local (None sends the caller to a local volume), because they
        # are not the user's files and must survive the staging directory
        # being cleared when the app stops.
        if m.scope == "per-user":
            return None
        # Everything else — the whole tree, another user's home, a shared
        # folder, a share this node was never given — is precisely what
        # brokered access exists to withhold.
        return None
    if m.scope == "root":
        return MESH_MOUNT
    if m.scope == "user-view":
        return os.path.join(MESH_MOUNT, config.NAS_VIEWS_SUBPATH, _safe(user))
    if m.scope == "user-home":
        # ANOTHER named user's home (m.name is their username, not the viewer's)
        # — see _expand_mounts' household branch, which only ever emits this for
        # an admin and always read-only.
        return os.path.join(MESH_MOUNT, config.NAS_USERS_SUBPATH, _safe(m.name))
    if m.scope == "share":
        # m.name is the share's slug (set by _expand_shares, not by an AppDef).
        return os.path.join(MESH_MOUNT, config.NAS_SHARES_SUBPATH, _safe(m.name))
    return _mesh_path_trusted(user, m)


def _staging_leaf(m) -> str:
    """The subdirectory name a mount gets inside an instance's staging area.

    The LAST segment of nas_path, so `ComfyUI/output` and `ComfyUI/input` stay
    distinguishable as `output` and `input` rather than colliding on the app
    name."""
    parts = [p for p in (getattr(m, "nas_path", "") or "").split("/")
             if p not in ("", ".", "..")]
    return _safe(parts[-1]) if parts else _safe(m.name)


def _staging_instance_dir(staging: str, app_id: str, user: str) -> str | None:
    """This instance's staging directory on an app-only node."""
    if not app_id:
        return None
    from . import registry
    inst = registry.instance_name(app_id, user)
    # Where that instance's directory sits depends on how much of the
    # cluster this node mounts, and both arrangements are legitimate:
    #
    #   scoped  — the node mounts ONLY its own staging directory, so
    #             that directory IS the root it sees. This is the one
    #             that actually isolates: the machine cannot read
    #             anyone's files even from a shell, because they are
    #             not in its filesystem at all.
    #   full    — the node mounts the whole tree and is kept to its
    #             staging directory by this function. Correct for
    #             apps, but only for apps.
    #
    # Detected rather than configured: a scoped mount has none of the
    # fleet tree's landmarks, which is a fact about the mount itself
    # and cannot drift out of sync the way a per-node setting would.
    if _mesh_is_scoped():
        return os.path.join(MESH_MOUNT, _safe(inst))
    if not _mesh_available():
        # No mount at all, and none possible: a node behind someone
        # else's firewall cannot reach the filer. Its files are pushed
        # to its own disk instead, so bind that.
        from . import nas_staging
        return nas_staging.pushed_dir(inst)
    return os.path.join(MESH_MOUNT, "staging", _safe(staging), _safe(inst))


def _mesh_path_trusted(user: str, m) -> str | None:
    """Placement on a node that mounts the whole tree."""
    if m.scope == "shared":
        # An app may group its mounts under one folder (Mount.nas_path) so a
        # person browsing the NAS finds "comfyui/output" rather than a row of
        # sibling directories. Each segment is sanitised individually — joining
        # first would let _safe flatten the separators back into the name.
        rel = getattr(m, "nas_path", "")
        if rel:
            parts = [_safe(p) for p in rel.split("/") if p not in ("", ".", "..")]
            if parts:
                return os.path.join(MESH_MOUNT, config.NAS_SHARED_SUBPATH, *parts)
        return os.path.join(MESH_MOUNT, config.NAS_SHARED_SUBPATH, _safe(m.name))
    if m.name != "home":
        # A per-user mount carrying nas_path is the app's WORK, not its
        # settings — pictures someone made, workflows they saved — so it goes
        # somewhere they can actually find it: a plainly-named folder in their
        # home, beside their other files. That placement is what makes the
        # existing sharing rules apply to it for free: an admin browsing
        # Household sees users/<name>/ComfyUI the same way they see anything
        # else of that person's, with no second permission model to keep in
        # step. Settings, below, stay hidden in .appdata where they belong.
        rel = getattr(m, "nas_path", "")
        if rel:
            parts = [_safe(p) for p in rel.split("/") if p not in ("", ".", "..")]
            if parts:
                return os.path.join(MESH_MOUNT, config.NAS_USERS_SUBPATH,
                                    _safe(user), *parts)
        # A named per-user mount is this app's settings for this user, kept in a
        # private corner of their home so it follows them between machines.
        return os.path.join(MESH_MOUNT, config.NAS_USERS_SUBPATH, _safe(user),
                            ".appdata", _safe(m.name))
    return os.path.join(MESH_MOUNT, config.NAS_USERS_SUBPATH, _safe(user))


def _mesh_target(user: str, m, app_id: str = "", host: str | None = None) -> str | None:
    """Create and return the host path to bind-mount, or None if unavailable.

    The mesh-mounted check cannot come first. A brokered node that cannot reach
    the filer has no mount and never will, yet it does have files — pushed to
    its own disk — and refusing here would make that path unreachable, which is
    exactly what it did: every mount resolved to None on such a node and the
    launch failed as though nothing had been sent. Ask _mesh_path what this
    mount resolves to, and only then require a mount if the answer needs one.
    """
    path = _mesh_path(user, m, app_id)
    if not path:
        return None
    if path.startswith(MESH_MOUNT) and not _mesh_available():
        return None
    # A named per-user mount (m.name != "home") is this app's OWN settings for
    # this user — e.g. a streamed app's .config, which is where its baked-in
    # KDE autostart entry (the thing that actually launches the app) lives.
    # Bind mounts, unlike Docker named volumes, never get Docker's own
    # populate-from-image behaviour: the first time this directory is created
    # for a brand-new user/app pairing it is genuinely empty, permanently
    # shadowing whatever defaults the image shipped at that path — with no
    # error anywhere. This bit KiCad's very first launch (2026-08-17): the
    # container came up, nginx and the desktop were healthy, but KiCad itself
    # never started and login 500'd, because the fix-up script that also
    # repairs nginx's basic-auth permissions only runs from inside the
    # autostart entry that was silently missing. FreeCAD never hit this only
    # because its per-user directories already had months of accumulated
    # state from before this gap was understood. `was_empty` is checked
    # BEFORE makedirs so an already-populated directory (the normal case,
    # every launch after the first) is never touched.
    was_empty = m.scope == "per-user" and m.name != "home" and (
        not os.path.isdir(path) or not os.listdir(path))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    if was_empty:
        _seed_from_image(app_id, m.path, path, host=host)
    return path


def _seed_from_image(app_id: str, container_path: str, host_path: str,
                      host: str | None = None) -> None:
    """Best-effort: copy the image's own baked-in defaults at `container_path`
    into a just-created, empty per-user settings directory before anything
    binds over it. See _mesh_target's comment for why this exists.

    Never raises — a brand-new, empty directory is the pre-existing (buggy
    but non-fatal) fallback, so any failure here just leaves that in place
    rather than blocking the launch over a settings seed.
    """
    try:
        from . import registry, app_variants
        app = registry.APPS.get(app_id)
        if app is None:
            return
        image = app_variants.active_image(app)
        if not image:
            return
        tmp = f"sm-seed-{_safe(app_id)}-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        created = _docker(["create", "--name", tmp, image], timeout=60, host=host)
        if created.returncode != 0:
            return
        try:
            # Trailing "/." copies the CONTENTS of container_path, not the
            # directory itself — host_path already exists (created above) and
            # must end up holding what was AT that path, not a path nested
            # one level too deep inside it.
            _docker(["cp", f"{tmp}:{container_path.rstrip('/')}/.", host_path],
                    timeout=60, host=host)
        finally:
            _docker(["rm", "-f", tmp], timeout=30, host=host)
        # Match the NFS export's anonuid (all_squash -> 1000) the same way
        # every streamed app's own Dockerfile already chowns its baked-in
        # config for — otherwise the copied files are root-owned and the
        # container's own uid-1000 processes can create new files beside them
        # but can never rewrite the seeded ones.
        subprocess.run(["chown", "-R", "1000:1000", host_path], check=False)
    except Exception:
        pass


def _nfs_target(user: str, m, app_id: str = "") -> tuple[str, str]:
    """(export subpath, docker volume name) for an NFS mount. per-user → the
    user's NAS home (same files across ALL their apps); shared → shared/{name};
    root → the whole export (Nextcloud mounts this + scopes per user itself).

    On an app-only node none of those paths exist. Its export is fsid=0 on its
    own staging directory, so that directory is the root of everything it can
    see, and the user's home mount resolves to the files staged for THIS app
    instance instead — which is the whole point of brokered access: the app gets
    the files the job needs and nothing else.
    """
    staging = nas_scope.staging_name()
    if staging:
        if m.scope in ("user-view", "share", "user-home"):
            # An app-only node sees a staging directory, not the NAS tree, so
            # there is no per-user view to root a file manager at and no shared
            # folder it is allowed to open. Skipping leaves the file manager
            # rooted at its own image volume showing only the staged files —
            # which is exactly what brokered access is meant to show.
            return "", _SKIP_MOUNT
        if m.scope == "root":
            # Nextcloud and friends mount the entire tree and scope it
            # internally. There is nothing to scope here, and quietly handing
            # over a staging directory would present an empty NAS as if it were
            # the real one. Refuse and say why.
            raise RuntimeError(
                f"{app_id or 'this app'} mounts the whole NAS, which an app-only "
                f"node cannot see — run it on a trusted node, or raise this "
                f"node's NAS trust")
        if m.scope == "per-user" and m.name == "home":
            from . import registry
            return registry.instance_name(app_id, user), \
                f"sm-nfs-staged-{_safe(app_id)}-{_safe(user)}"
        if m.scope == "per-user":
            # A per-user, non-home NFS mount is this app's own settings for this
            # user (FreeCAD's config and share dirs). They must NOT live in
            # staging, which is wiped when the app stops — that would reset the
            # user's preferences on every run. A node-local volume keeps them,
            # at the cost of not following the user between nodes.
            return "", ""
        # A shared or root NFS mount is fleet-wide data this node is not allowed
        # to see. Substituting a node-local volume was worse than not mounting
        # it: the app showed a folder with the shared folder's NAME, backed by
        # empty scratch space on the node's own disk. Anything saved into it
        # looked shared, was visible nowhere else, and died with the node.
        # Skip it entirely — an absent folder is honest, a fake one is not.
        return "", _SKIP_MOUNT
    if m.scope == "root":
        return "", "sm-nfs-root"
    if m.scope == "user-view":
        return f"{config.NAS_VIEWS_SUBPATH}/{_safe(user)}", f"sm-nfs-view-{_safe(user)}"
    if m.scope == "user-home":
        return f"{config.NAS_USERS_SUBPATH}/{_safe(m.name)}", f"sm-nfs-users-{_safe(m.name)}"
    if m.scope == "share":
        return f"{config.NAS_SHARES_SUBPATH}/{_safe(m.name)}", f"sm-nfs-share-{_safe(m.name)}"
    if m.scope == "shared":
        return f"{config.NAS_SHARED_SUBPATH}/{_safe(m.name)}", f"sm-nfs-shared-{_safe(m.name)}"
    if m.name != "home":
        # named per-user mount → a private .appdata corner of the user's home:
        # app settings (e.g. FreeCAD ~/.config) persist across relaunches AND
        # follow the user to any node, and snapshots are plain file copies.
        sub = f"{config.NAS_USERS_SUBPATH}/{_safe(user)}/.appdata/{_safe(m.name)}"
        return sub, f"sm-nfs-users-{_safe(user)}-{_safe(m.name)}"
    return f"{config.NAS_USERS_SUBPATH}/{_safe(user)}", f"sm-nfs-users-{_safe(user)}"


# _nfs_volume_create removed with the NFS route (see _ensure_nfs). It only ever
# produced volumes that could not be mounted, and a volume that exists but
# cannot mount is worse than one never created: it fails at container start,
# far from the code that made it. _nfs_target is kept — naming volumes is pure
# string work, and app_storage uses it to find data written when this route was
# believed to work.


def _ensure_nfs(user: str, m, host: str | None = None, app_id: str = "") -> str:
    """RETIRED. Raises unless this mount has no NAS home in the first place.

    This used to reach the NAS over NFS when the mesh mount was absent. It
    never once succeeded: the NAS host re-exports a FUSE (SeaweedFS) mount over
    kernel NFS, and the Hub's own records show no NFSv4 client has ever
    completed a mount against it. What it did instead was hang — an incomplete
    mount is uninterruptible, so the 120s timeout could not even kill it, and
    the caller was left holding a timed-out `alpine mkdir` naming neither the
    NAS nor the real cause.

    That is worse than having no fallback. It cost a full debugging session:
    FreeCAD would not start on a node, reporting a timed-out mkdir, when the
    actual problem was that the node had never had `weed` installed and so had
    no mesh mount. An honest error would have said so in seconds.

    Both early returns are preserved exactly — a mount this node may not see,
    and an app-only node's node-local settings, are legitimate answers rather
    than failures. Only the genuinely-broken path now raises.
    """
    subpath, vol = _nfs_target(user, m, app_id=app_id)
    if vol == _SKIP_MOUNT:
        return _SKIP_MOUNT     # do not attach this mount at all
    if not vol:
        return ""              # app-only node: caller falls back to a local volume
    # Which of the three situations this is. "Mount the mesh" is useless advice
    # on a node where it is already mounted, and an error that misdescribes the
    # machine it came from is how the original timeout stayed misdiagnosed.
    if nas_scope.staging_name():
        raise RuntimeError(
            f"{config.NODE_NAME} has brokered (app-only) NAS access, which reaches "
            f"staged files over NFS from {config.NAS_HOST} — the route retired here, "
            f"because no client ever completed a mount through it. Staging for this "
            f"node is not currently exported on the NAS host either. Export it there, "
            f"or give this node the mesh mount and a trust level that suits it.")
    if not _mesh_available():
        raise RuntimeError(
            f"{config.NODE_NAME} has no mesh NAS mount at {config.NAS_MESH_MOUNT}, so "
            f"it cannot provide NAS storage for this app. Mount it on that node "
            f"(sandos-nas-mount.service, which needs the `weed` binary in "
            f"/usr/local/bin) and start the app again. The NFS route this used to fall "
            f"back to has been removed — it never completed a mount, and only turned a "
            f"missing mesh mount into a two-minute timeout.")
    raise RuntimeError(
        f"{config.NODE_NAME} has the mesh NAS mounted at {config.NAS_MESH_MOUNT}, but "
        f"this mount ({m.scope}/{m.name}) has no place inside it. That is a gap in the "
        f"mount's definition rather than a setup problem on this node — the NFS route "
        f"that used to absorb such cases has been removed, because it never completed "
        f"a mount.")


def _expand_mounts(user: str, mounts) -> list:
    """Turn an AppDef's declared mounts into the concrete ones for THIS user.

    Two expansions, both of which need a username the AppDef cannot know:
      - `{user}` in a mount path becomes the instance owner, so a file manager
        can show someone's files under a folder bearing their own name.
      - a `shares` mount is a placeholder: it becomes one mount per shared
        folder this user belongs to, each at `<path>/<label>` ("admin + braeden").
        Membership is read at launch, so adding someone to a folder needs only
        an app restart, not a redeploy.
    """
    import dataclasses
    from . import nas_roster, nas_shares

    out = []
    for m in mounts:
        if m.scope == "household":
            # Only an admin sees other people's folders, and only those whose
            # account is set to Household. Both facts come from the roster the
            # Hub pushes to the NAS, so this resolves with the Hub unreachable.
            try:
                if not nas_roster.is_admin(user):
                    continue
                people = nas_roster.household_users(exclude=user)
            except OSError:
                continue
            for name in people:
                out.append(dataclasses.replace(
                    m, name=name, scope="user-home", ro=True,
                    path=os.path.join(m.path, name)))
            continue
        if m.scope == "shares":
            try:
                shares = nas_shares.shares_for(user)
            except OSError:
                shares = []      # NAS unreachable: show their own files, not an error
            for s in shares:
                out.append(dataclasses.replace(
                    m, name=s["slug"], scope="share",
                    path=os.path.join(m.path, s["label"])))
            continue
        if "{user}" in m.path:
            m = dataclasses.replace(m, path=m.path.replace("{user}", user))
        out.append(m)
    return out


def _view_children(view_path: str, mounts) -> set[str]:
    """Top-level folder names a view root legitimately has right now.

    A mount nested deeper (/srv/Household/braeden) still contributes its FIRST
    segment (Household), because that directory is a real container for live
    mounts and must not be pruned.
    """
    out: set[str] = set()
    prefix = view_path.rstrip("/") + "/"
    for m in mounts:
        if m.path.startswith(prefix):
            out.add(m.path[len(prefix):].split("/", 1)[0])
    return out


def _view_rel_paths(view_path: str, mounts) -> list[str]:
    """Paths of every mount nested inside the view, relative to it."""
    prefix = view_path.rstrip("/") + "/"
    return [m.path[len(prefix):] for m in mounts if m.path.startswith(prefix)]


def _mount_args(app_id: str, user: str, mounts, host: str | None = None) -> list[str]:
    from . import registry, app_storage
    out: list[str] = []
    # Parents before children. A file manager's root is itself a mount now, with
    # its home and shares nested inside it — mount those first and the root
    # would be laid down on top, hiding every one of them.
    mounts = sorted(_expand_mounts(user, mounts),
                    key=lambda m: m.path.count("/"))
    for m in mounts:
        # app_storage's per-(app,user,mount) override takes precedence over the
        # Mount's own declared default — this is what makes "move this app's
        # data onto a USB drive" actually change where it runs from. `host` here
        # is about where the APP'S IMAGE runs (app_images.py) — independent of
        # this, but the volume must be created in that SAME daemon's storage.
        mode, usb_uuid = app_storage.effective_storage(app_id, user, m)
        _mesh = (_mesh_target(user, m, app_id, host=host)
                 if mode == "nfs" and config.NAS_ENABLED else None)
        if mode == "usb" and usb_uuid:
            vol = ensure_usb_volume(usb_uuid, app_id, user, m, host=host)
        elif mode == "nfs" and config.NAS_ENABLED and _mesh:
            # Mesh NAS: bind the local mount. Preferred over NFS wherever it is
            # present, so apps read from the whole cluster rather than through
            # one server.
            vol = _mesh
            if m.scope == "user-view":
                # The view root is this user's top level, so it is the one place
                # a plain text file is unmissable. Rewritten every launch so a
                # visibility change reaches the person it is about without
                # anyone remembering to update it.
                from . import nas_roster
                nas_roster.write_notice(vol, user)
                # Prune first (yesterday's leftovers), then create today's — so a
                # folder that is BOTH stale and current, such as Household when
                # its membership changed, is not removed after being recreated.
                nas_roster.prune_stale_mountpoints(vol, _view_children(m.path, mounts))
                nas_roster.ensure_view_children(vol, _view_rel_paths(m.path, mounts))
        elif mode == "nfs" and config.NAS_ENABLED:
            # No mesh path for this mount. Either it is one this node must not
            # attach, or it is node-local settings on an app-only node — both
            # answered inside. Anything else is a node that needs the NAS and
            # has no mount, which _ensure_nfs now says plainly instead of
            # timing out against an NFS route that never worked.
            vol = _ensure_nfs(user, m, host=host, app_id=app_id)
            if vol == _SKIP_MOUNT:
                continue          # fleet-shared data this node may not see
            if not vol:
                # App-only node, and this mount is not the user's files (it is
                # settings/logs/cache). Those stay node-local — putting them in
                # staging would delete the user's app configuration every time
                # the app stops.
                vol = registry.resolve_volume(app_id, user, m)
        else:
            vol = registry.resolve_volume(app_id, user, m)  # node-local docker volume
        out += ["-v", f"{vol}:{m.path}" + (":ro" if m.ro else "")]
    return out


def _spawn_service(inst: Instance, app: AppDef, svc, net: str, host: str | None = None) -> subprocess.CompletedProcess:
    """Start one sidecar on the app's network — internal only, no host ports."""
    name = f"{inst.name}-{svc.name}"
    # See stop()'s comment above — this exact call (removing a stale sidecar
    # like sm-nextcloud-db before respawning it) timed out live during ordinary
    # use with the old 30s value; NFS-backed volumes need more room.
    _docker(["rm", "-f", name], timeout=90, host=host)  # clear any stale copy
    args = ["run", "--name", name, "-d", "--rm", "-e", "TZ=UTC",
            "--network", net, "--network-alias", svc.name]
    for k, v in svc.env.items():
        args += ["-e", f"{k}={v}"]
    args += _mount_args(app.id, inst.user, svc.mounts, host=host)
    args += getattr(svc, "docker_args", [])
    args.append(svc.image)
    args += svc.cmd
    return _docker(args, timeout=90, host=host)


def _wait_service(inst: Instance, svc, timeout: int = 90, host: str | None = None) -> bool:
    """Poll a service's readiness probe (docker exec) until it passes."""
    if not svc.ready_cmd:
        return True
    name = f"{inst.name}-{svc.name}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _docker(["exec", name, *svc.ready_cmd], timeout=15, host=host).returncode == 0:
            return True
        time.sleep(2)
    return False


def teardown(name: str, app: AppDef, host: str | None = None) -> None:
    """Remove the primary, all sidecars, and the private network (if any),
    all on the daemon the app's image actually runs from."""
    # The primary goes first, and gracefully. Order is the point: the app has
    # to finish writing to its database before the database is taken away, so
    # killing them together — or the sidecar first — is how a shutdown corrupts
    # something that a clean one would not.
    grace = getattr(app, "stop_grace", 10)
    stop(name, host=host, grace=grace)
    for svc in app.services:
        stop(f"{name}-{svc.name}", host=host, grace=grace)
    if app.services:
        _docker(["network", "rm", network_name(name)], timeout=15, host=host)


def spawn(inst: Instance, app: AppDef) -> subprocess.CompletedProcess:
    """Start an instance from its App Definition. Streamed apps (Selkies GPU
    desktop) get TURN/relay + encoder env; web apps just get their one localhost
    port. Apps with sidecars (DB/cache) get a private network + those services
    started and waited-on first. The primary binds web to 127.0.0.1 so the ONLY
    way in is the session-gated SM proxy. Returns the primary's run process.

    Everything — image, sidecars, network, volumes — runs against WHICHEVER
    daemon app_images.py says this app's image currently lives in (None =
    the node's own default daemon, the normal case; a USB socket if the
    image was moved/mirrored there — see app_images.move_to_usb)."""
    # deferred imports avoid circular imports (registry/app_images → docker_backend)
    from . import registry, app_images
    host = app_images.active_docker_host(app.id)

    # For auto_pull apps, pre-pull the image separately with a generous timeout
    # so the pull doesn't eat into the 120s docker-run timeout. docker run would
    # pull implicitly, but a 2-4 GB image easily exceeds that window.
    if getattr(app, "auto_pull", False):
        from . import app_variants as _av
        _img = _av.active_image(app)
        if not app_images._image_exists(_img, host):
            _docker(["pull", _img], timeout=600, host=host)

    # Pre-create any custom shared networks declared in docker_args (e.g. sm-llm-net).
    _extra = _resolve_docker_args(app)
    _skip_nets = {"bridge", "host", "none"}
    for _flag, _val in zip(_extra, _extra[1:]):
        if _flag == "--network" and _val not in _skip_nets and not _val.startswith("container:"):
            _ensure_network(_val, host=host)

    net = None
    if app.services:
        net = network_name(inst.name)
        _ensure_network(net, host=host)
        for svc in app.services:
            res = _spawn_service(inst, app, svc, net, host=host)
            if res.returncode != 0:
                return res  # surface the sidecar failure
        for svc in app.services:
            if not _wait_service(inst, svc, host=host):
                return subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="",
                    stderr=f"service '{svc.name}' not ready in time")

    # Stable hostname, per (app, user, NODE).
    #
    # Containers run --rm and Docker otherwise hands out the container ID as the
    # hostname, so every relaunch looks like a DIFFERENT machine. An app that
    # records who holds a file then strands a lock naming a host that no longer
    # exists — KiCad: "Project … is already open by 'root' at 'd9be3f3844ba'",
    # seen after any recreate (idle reap, or remount_heal clearing a dead FUSE
    # handle). The app cannot tell a stale lock from a live one on another
    # machine, so it refuses rather than reclaiming.
    #
    # A hostname that is stable across relaunches lets the app recognise its own
    # previous lock and reclaim it. The NODE has to be in there too: a per-user
    # app can be multi_node, so two nodes may hold the same user's NAS home at
    # once — naming them identically would make each one mistake the OTHER's
    # LIVE lock for its own stale one and steal a project that is genuinely
    # open elsewhere. Same name on the same box, different across boxes, which
    # is exactly what a hostname is supposed to mean.
    _host = re.sub(r"[^a-z0-9-]+", "-", f"{inst.name}-{config.NODE_NAME}".lower()).strip("-")
    args = ["run", "--name", inst.name, "--hostname", _host[:63],
            "-d", "--rm", "-e", "TZ=UTC"]
    if getattr(app, "mem_limit", ""):
        args += ["--memory", app.mem_limit]
    if net:
        args += ["--network", net]
    if app.gpu:
        args += ["--device", "nvidia.com/gpu=all", "-e", "NVIDIA_DRIVER_CAPABILITIES=all"]
        # On WSL the GPU is visible but the VIDEO CODEC libraries are not.
        # WSL keeps its NVIDIA userspace in /usr/lib/wsl/lib, and the CDI/gpus
        # injection does not carry it, so libnvcuvid.so.1 is missing inside
        # the container: GStreamer cannot create nvh264enc, returns None, and
        # selkies dies with "'NoneType' object has no attribute set_property".
        # Supervisor respawns it, the client re-handshakes, and the stream
        # flickers between connecting and a new peer id forever.
        if os.path.isdir("/usr/lib/wsl/lib"):
            args += ["-v", "/usr/lib/wsl/lib:/usr/lib/wsl/lib:ro",
                     "-e", "LD_LIBRARY_PATH=/usr/lib/wsl/lib"]

    # Web/UI port — localhost only (reachable solely via the SM proxy).
    args += ["-p", f"127.0.0.1:{inst.web_port}:{app.internal_port}"]

    if app.streamed:
        # WebRTC media path: TURN + a small UDP relay range on the LAN so the
        # browser can reach it directly (it bypasses the proxy).
        args += [
            "-p", f"{inst.turn_port}:{inst.turn_port}/tcp",
            "-p", f"{inst.turn_port}:{inst.turn_port}/udp",
            "-p", f"{inst.relay_min}-{inst.relay_max}:{inst.relay_min}-{inst.relay_max}/udp",
            "--tmpfs", "/dev/shm:rw",
            "-e", "DISPLAY_SIZEW=1920", "-e", "DISPLAY_SIZEH=1080", "-e", "DISPLAY_REFRESH=60",
            "-e", f"SELKIES_ENABLE_RESIZE={'true' if app.resize else 'false'}",
            "-e", f"SELKIES_ENCODER={app.encoder}",
            "-e", "SELKIES_VIDEO_BITRATE=16000", "-e", "SELKIES_FRAMERATE=60",
            # internal TURN, pinned to this host's LAN IP + this instance's ports
            "-e", f"SELKIES_TURN_HOST={config.LAN_IP}", "-e", f"TURN_EXTERNAL_IP={config.LAN_IP}",
            "-e", f"SELKIES_TURN_PORT={inst.turn_port}", "-e", "SELKIES_TURN_PROTOCOL=tcp",
            "-e", f"TURN_MIN_PORT={inst.relay_min}", "-e", f"TURN_MAX_PORT={inst.relay_max}",
            "-e", f"SELKIES_BASIC_AUTH_USER={config.INSTANCE_USER}",
            "-e", f"PASSWD={config.INSTANCE_PASSWD}",
            "-e", f"SELKIES_BASIC_AUTH_PASSWORD={config.INSTANCE_PASSWD}",
        ]
        if config.TURN_EXTRA_HOST:
            # SM_TURN_EXTRA_HOST also rewrites the /turn HTTP response for
            # browser-side JS (proxy.py's _inject_extra_turn) — but that never
            # reaches the SERVER-SIDE relay candidate baked into the WebRTC
            # offer, because selkies-gstreamer fetches /turn from its own
            # local nginx directly, bypassing SM's proxy entirely. Passing the
            # same host in here lets write-rtc-config.sh (containers/
            # kicad-streamer, and any other streamed app that copies it) feed
            # it to Selkies' own multi-TURN-server config file instead, which
            # is what the offer actually reads from. Confirmed live
            # 2026-08-17: without this, a remote/VPN client's SDP offer only
            # ever contained the LAN relay candidate, unreachable off-LAN.
            #
            # write-rtc-config.sh runs a SECOND, local-only turnserver rather
            # than pointing selkies-gstreamer straight at TURN_EXTRA_HOST,
            # because that address is only reachable via the Hub's WireGuard
            # tunnel — a node that isn't itself a WG mesh peer (Vortex-
            # Eclipse, CortexPC today) has no route to it, and the allocation
            # attempt just fails silently with no second candidate ever
            # appearing (also confirmed live). The second turnserver is
            # reached over loopback instead (always works, no routing needed)
            # and reports TURN_EXTRA_HOST as ITS external address regardless
            # — standard TURN NAT-traversal behavior. It needs its own
            # control port and its own relay range, offset past every slot's
            # own primary range so the two can never collide.
            extra_port = inst.turn_port + 1
            extra_relay_min = inst.relay_min + config.EXTRA_RELAY_OFFSET
            extra_relay_max = inst.relay_max + config.EXTRA_RELAY_OFFSET
            args += [
                "-p", f"{extra_relay_min}-{extra_relay_max}:{extra_relay_min}-{extra_relay_max}/udp",
                "-e", f"SELKIES_TURN_EXTRA_HOST={config.TURN_EXTRA_HOST}",
                "-e", f"SELKIES_TURN_EXTRA_PORT={extra_port}",
                "-e", f"TURN_EXTRA_RELAY_MIN={extra_relay_min}",
                "-e", f"TURN_EXTRA_RELAY_MAX={extra_relay_max}",
            ]

    # Data volumes — the NAS layer. Per-user volumes are private; shared volumes
    # are one library many apps/users mount (optionally read-only).
    args += _mount_args(app.id, inst.user, app.mounts, host=host)

    # Dev bind mounts — bind a real host dir straight into the container (a DEV app
    # that runs live from a bind-mounted source tree). node_modules etc. are shadowed
    # by the image's own VOLUME declarations so the host tree isn't injected. Only
    # applied when the source tree actually exists on THIS host — binds is a fixed
    # AppDef fact, not a per-node one (see App Definition Standard §8.1), so on a
    # node that doesn't have the checkout, applying it anyway would have Docker
    # silently create and mount an EMPTY directory over the image's own baked-in
    # code (see app_variants.py's packaged-image build) — the exact failure mode
    # the Fleet page's red "dev source missing" badge exists to warn about.
    if app.binds:
        from . import registry  # deferred: avoids a circular import at load time
        if registry.source_tree_ready(app):
            for host_path, container_path in app.binds:
                args += ["-v", f"{host_path}:{container_path}"]

    # App-specific extra env (declared on the App Definition).
    for k, v in app.env.items():
        args += ["-e", f"{k}={v}"]

    args += _resolve_docker_args(app)

    from . import app_variants  # deferred: avoids a circular import at load time
    args.append(app_variants.active_image(app))
    res = _docker(args, timeout=120, host=host)

    # Docker Desktop (Windows/Mac) exposes GPU passthrough through its own
    # bundled nvidia-container-runtime via the legacy `--gpus` flag, but —
    # unlike native Linux + nvidia-container-toolkit — never generates an
    # NVIDIA CDI spec, so `--device nvidia.com/gpu=all` has nothing to
    # resolve there and every GPU app launch fails with "CDI device
    # injection failed: unresolvable CDI devices nvidia.com/gpu=all"
    # (confirmed live on a Docker-Desktop/WSL2 node with a real GPU and
    # working `nvidia` runtime — just not CDI-registered). Retry once with
    # the flag Docker Desktop actually supports instead of permanently
    # failing GPU apps on those hosts.
    if app.gpu and res.returncode != 0 and "CDI device injection failed" in (res.stderr or ""):
        try:
            i = args.index("--device")
            args[i:i + 2] = ["--gpus", "all"]
            res = _docker(args, timeout=120, host=host)
        except ValueError:
            pass
    return res
