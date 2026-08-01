"""Docker backend — spawn / stop / inspect FreeCAD-streamer instances via the
docker CLI (no extra SDK dependency). Mirrors the proven run-lan.sh parameters,
but with per-instance ports so concurrent instances don't collide."""
from __future__ import annotations
import calendar
import json
import os
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


def stop(name: str, host: str | None = None) -> None:
    # A container with an NFS-backed volume can take well over 30s to remove
    # if the mount is momentarily slow (observed live: sm-nextcloud-db's
    # removal alone timed out at 30s during ordinary use, no error on the NFS
    # side at all — just NFS occasionally being slower than a short timeout
    # allows for). Same reasoning as _ensure_nfs's mkdir timeout above.
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


def _mesh_path(user: str, m, app_id: str = "") -> str | None:
    """Where this mount lives inside the mesh NAS, or None if it has no place."""
    if m.scope == "root":
        return MESH_MOUNT
    if m.scope == "user-view":
        return os.path.join(MESH_MOUNT, config.NAS_VIEWS_SUBPATH, _safe(user))
    if m.scope == "share":
        # m.name is the share's slug (set by _expand_shares, not by an AppDef).
        return os.path.join(MESH_MOUNT, config.NAS_SHARES_SUBPATH, _safe(m.name))
    if m.scope == "shared":
        return os.path.join(MESH_MOUNT, config.NAS_SHARED_SUBPATH, _safe(m.name))
    if m.name != "home":
        # A named per-user mount is this app's settings for this user, kept in a
        # private corner of their home so it follows them between machines.
        return os.path.join(MESH_MOUNT, config.NAS_USERS_SUBPATH, _safe(user),
                            ".appdata", _safe(m.name))
    return os.path.join(MESH_MOUNT, config.NAS_USERS_SUBPATH, _safe(user))


def _mesh_target(user: str, m, app_id: str = "") -> str | None:
    """Create and return the host path to bind-mount, or None if unavailable."""
    if not _mesh_available():
        return None
    path = _mesh_path(user, m, app_id)
    if not path:
        return None
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    return path


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
        if m.scope in ("user-view", "share"):
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


def _nfs_volume_create(vol: str, device: str, host: str | None = None) -> None:
    if _docker(["volume", "inspect", vol], timeout=10, host=host).returncode != 0:
        _docker(["volume", "create", "--driver", "local", "--opt", "type=nfs",
                 "--opt", f"o=addr={config.NAS_HOST},rw,nfsvers=4",
                 "--opt", f"device={device}", vol], timeout=15, host=host)


def _ensure_nfs(user: str, m, host: str | None = None, app_id: str = "") -> str:
    """Ensure the NAS dir exists + an NFS-backed docker volume for it, in the
    given daemon; return the volume name. The dir is created via a throwaway
    mount of the NFS root, so this works from ANY node/daemon (the app node
    need not be the NAS)."""
    subpath, vol = _nfs_target(user, m, app_id=app_id)
    if vol == _SKIP_MOUNT:
        return _SKIP_MOUNT     # do not attach this mount at all
    if not vol:
        return ""              # app-only node: caller falls back to a local volume
    _nfs_volume_create("sm-nfs-root", ":/", host=host)       # NFSv4 pseudo-root
    # A brand-new NFSv4 client establishing its first session/lease against the
    # NAS can occasionally take well over 45s under load (observed live, not
    # theoretical) — a fixed 45s Python subprocess timeout can't actually kill
    # a process stuck on NFS I/O (uninterruptible D-state), so it just gives up
    # and reports failure while the real `docker run` keeps going orphaned in
    # the background, competing with the NEXT retry's own attempt at the exact
    # same path. That pile-up of orphaned concurrent mkdirs is what made this
    # look like a hard deadlock rather than occasional slowness. Give it real
    # room instead of walking away early.
    _docker(["run", "--rm", "-v", "sm-nfs-root:/r", "alpine",
             "mkdir", "-p", f"/r/{subpath}"], timeout=120, host=host)   # dir on the NAS
    _nfs_volume_create(vol, f":/{subpath}", host=host)
    return vol


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
    from . import nas_shares

    out = []
    for m in mounts:
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
        if mode == "usb" and usb_uuid:
            vol = ensure_usb_volume(usb_uuid, app_id, user, m, host=host)
        elif mode == "nfs" and config.NAS_ENABLED and _mesh_target(user, m, app_id):
            # Mesh NAS: bind the local mount. Preferred over NFS wherever it is
            # present, so apps read from the whole cluster rather than through
            # one server.
            vol = _mesh_target(user, m, app_id)
        elif mode == "nfs" and config.NAS_ENABLED:
            vol = _ensure_nfs(user, m, host=host, app_id=app_id)  # legacy single-server NFS
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
    _docker(["rm", "-f", name], timeout=90, host=host)
    for svc in app.services:
        _docker(["rm", "-f", f"{name}-{svc.name}"], timeout=90, host=host)
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
    _extra = getattr(app, "docker_args", [])
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

    args = ["run", "--name", inst.name, "-d", "--rm", "-e", "TZ=UTC"]
    if getattr(app, "mem_limit", ""):
        args += ["--memory", app.mem_limit]
    if net:
        args += ["--network", net]
    if app.gpu:
        args += ["--device", "nvidia.com/gpu=all", "-e", "NVIDIA_DRIVER_CAPABILITIES=all"]

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

    args += getattr(app, "docker_args", [])

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
