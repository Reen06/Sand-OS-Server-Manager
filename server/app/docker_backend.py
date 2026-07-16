"""Docker backend — spawn / stop / inspect FreeCAD-streamer instances via the
docker CLI (no extra SDK dependency). Mirrors the proven run-lan.sh parameters,
but with per-instance ports so concurrent instances don't collide."""
from __future__ import annotations
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from .models import AppDef, Instance
from . import config


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


def stats(names: list[str], host: str | None = None) -> dict[str, dict]:
    """`docker stats` snapshot for the given (running) container names on ONE
    daemon -> {name: {cpu_percent, mem_used_mb, mem_limit_mb, mem_percent}}.
    Skipped entirely (returns {}) if `names` is empty — `docker stats` with
    no name args would otherwise snapshot EVERY container on that daemon."""
    if not names:
        return {}
    r = _docker(["stats", "--no-stream", "--format", "{{json .}}", *names], timeout=15, host=host)
    if r.returncode != 0:
        return {}
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
        out[name] = {
            "cpu_percent": _parse_percent(d.get("CPUPerc")),
            "mem_used_mb": mem_used,
            "mem_limit_mb": mem_limit,
            "mem_percent": _parse_percent(d.get("MemPerc")),
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


def nfs_volume_name(user: str, m) -> str:
    return _nfs_target(user, m)[1]


def ensure_nfs_volume(user: str, m, host: str | None = None) -> str:
    """Public wrapper — app_storage.py resolves/creates NFS-backed volumes the
    same way spawn()'s _mount_args does internally."""
    return _ensure_nfs(user, m, host=host)


def _nfs_target(user: str, m) -> tuple[str, str]:
    """(export subpath, docker volume name) for an NFS mount. per-user → the
    user's NAS home (same files across ALL their apps); shared → shared/{name};
    root → the whole export (Nextcloud mounts this + scopes per user itself)."""
    if m.scope == "root":
        return "", "sm-nfs-root"
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


def _ensure_nfs(user: str, m, host: str | None = None) -> str:
    """Ensure the NAS dir exists + an NFS-backed docker volume for it, in the
    given daemon; return the volume name. The dir is created via a throwaway
    mount of the NFS root, so this works from ANY node/daemon (the app node
    need not be the NAS)."""
    subpath, vol = _nfs_target(user, m)
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


def _mount_args(app_id: str, user: str, mounts, host: str | None = None) -> list[str]:
    from . import registry, app_storage
    out: list[str] = []
    for m in mounts:
        # app_storage's per-(app,user,mount) override takes precedence over the
        # Mount's own declared default — this is what makes "move this app's
        # data onto a USB drive" actually change where it runs from. `host` here
        # is about where the APP'S IMAGE runs (app_images.py) — independent of
        # this, but the volume must be created in that SAME daemon's storage.
        mode, usb_uuid = app_storage.effective_storage(app_id, user, m)
        if mode == "usb" and usb_uuid:
            vol = ensure_usb_volume(usb_uuid, app_id, user, m, host=host)
        elif mode == "nfs" and config.NAS_ENABLED:
            vol = _ensure_nfs(user, m, host=host)           # fleet NAS over NFSv4
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
    # by the image's own VOLUME declarations so the host tree isn't injected.
    for host_path, container_path in app.binds:
        args += ["-v", f"{host_path}:{container_path}"]

    # App-specific extra env (declared on the App Definition).
    for k, v in app.env.items():
        args += ["-e", f"{k}={v}"]

    args += getattr(app, "docker_args", [])

    from . import app_variants  # deferred: avoids a circular import at load time
    args.append(app_variants.active_image(app))
    return _docker(args, timeout=120, host=host)
