"""Docker backend — spawn / stop / inspect FreeCAD-streamer instances via the
docker CLI (no extra SDK dependency). Mirrors the proven run-lan.sh parameters,
but with per-instance ports so concurrent instances don't collide."""
from __future__ import annotations
import json
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


def web_ready(port: int) -> bool:
    """True once the instance's web server answers ANY HTTP status (200/302/401…)."""
    try:
        _ready_opener.open(f"http://127.0.0.1:{port}/", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True  # 3xx redirect, 401 auth challenge, etc. — the server is up
    except Exception:
        return False


def _docker(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def running(name: str) -> bool:
    r = _docker(["inspect", "-f", "{{.State.Running}}", name], timeout=10)
    return r.returncode == 0 and r.stdout.strip() == "true"


def exists(name: str) -> bool:
    return _docker(["inspect", name], timeout=10).returncode == 0


def stop(name: str) -> None:
    _docker(["rm", "-f", name], timeout=30)


def list_sm_containers() -> list[str]:
    """All Server-Manager-owned containers (running or not)."""
    r = _docker(["ps", "-a", "--filter", "name=^sm-", "--format", "{{.Names}}"], timeout=10)
    return [n for n in r.stdout.split() if n.startswith("sm-")]


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


def stats(names: list[str]) -> dict[str, dict]:
    """`docker stats` snapshot for the given (running) container names ->
    {name: {cpu_percent, mem_used_mb, mem_limit_mb, mem_percent}}. Skipped
    entirely (returns {}) if `names` is empty — `docker stats` with no name
    args would otherwise snapshot EVERY container on the host."""
    if not names:
        return {}
    r = _docker(["stats", "--no-stream", "--format", "{{json .}}", *names], timeout=15)
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


def published_web_port(name: str) -> int | None:
    """The SM-assigned localhost web port for a container, independent of its
    internal port (8080 for Selkies/Filebrowser, 80 for Nextcloud). Finds the
    127.0.0.1 binding whose host port is in the SM web range. Returns None for
    sidecars (DB/cache) — they publish nothing — so reconcile skips them."""
    r = _docker(["inspect", "-f", "{{json .NetworkSettings.Ports}}", name], timeout=10)
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


def _ensure_network(net: str) -> None:
    if _docker(["network", "inspect", net], timeout=10).returncode != 0:
        _docker(["network", "create", net], timeout=15)


def _safe(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


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


def _nfs_volume_create(vol: str, device: str) -> None:
    if _docker(["volume", "inspect", vol], timeout=10).returncode != 0:
        _docker(["volume", "create", "--driver", "local", "--opt", "type=nfs",
                 "--opt", f"o=addr={config.NAS_HOST},rw,nfsvers=4",
                 "--opt", f"device={device}", vol], timeout=15)


def _ensure_nfs(user: str, m) -> str:
    """Ensure the NAS dir exists + an NFS-backed docker volume for it; return the
    volume name. The dir is created via a throwaway mount of the NFS root, so this
    works from ANY node (the app node need not be the NAS)."""
    subpath, vol = _nfs_target(user, m)
    _nfs_volume_create("sm-nfs-root", ":/")                 # NFSv4 pseudo-root
    _docker(["run", "--rm", "-v", "sm-nfs-root:/r", "alpine",
             "mkdir", "-p", f"/r/{subpath}"], timeout=45)   # create the dir on the NAS
    _nfs_volume_create(vol, f":/{subpath}")
    return vol


def _mount_args(app_id: str, user: str, mounts) -> list[str]:
    from . import registry
    out: list[str] = []
    for m in mounts:
        if getattr(m, "storage", "local") == "nfs" and config.NAS_ENABLED:
            vol = _ensure_nfs(user, m)                      # fleet NAS over NFSv4
        else:
            vol = registry.resolve_volume(app_id, user, m)  # node-local docker volume
        out += ["-v", f"{vol}:{m.path}" + (":ro" if m.ro else "")]
    return out


def _spawn_service(inst: Instance, app: AppDef, svc, net: str) -> subprocess.CompletedProcess:
    """Start one sidecar on the app's network — internal only, no host ports."""
    name = f"{inst.name}-{svc.name}"
    _docker(["rm", "-f", name], timeout=30)  # clear any stale copy
    args = ["run", "--name", name, "-d", "--rm", "-e", "TZ=UTC",
            "--network", net, "--network-alias", svc.name]
    for k, v in svc.env.items():
        args += ["-e", f"{k}={v}"]
    args += _mount_args(app.id, inst.user, svc.mounts)
    args.append(svc.image)
    args += svc.cmd
    return _docker(args, timeout=90)


def _wait_service(inst: Instance, svc, timeout: int = 90) -> bool:
    """Poll a service's readiness probe (docker exec) until it passes."""
    if not svc.ready_cmd:
        return True
    name = f"{inst.name}-{svc.name}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _docker(["exec", name, *svc.ready_cmd], timeout=15).returncode == 0:
            return True
        time.sleep(2)
    return False


def teardown(name: str, app: AppDef) -> None:
    """Remove the primary, all sidecars, and the private network (if any)."""
    _docker(["rm", "-f", name], timeout=30)
    for svc in app.services:
        _docker(["rm", "-f", f"{name}-{svc.name}"], timeout=30)
    if app.services:
        _docker(["network", "rm", network_name(name)], timeout=15)


def spawn(inst: Instance, app: AppDef) -> subprocess.CompletedProcess:
    """Start an instance from its App Definition. Streamed apps (Selkies GPU
    desktop) get TURN/relay + encoder env; web apps just get their one localhost
    port. Apps with sidecars (DB/cache) get a private network + those services
    started and waited-on first. The primary binds web to 127.0.0.1 so the ONLY
    way in is the session-gated SM proxy. Returns the primary's run process."""
    # deferred import avoids a circular import (registry → docker_backend)
    from . import registry

    net = None
    if app.services:
        net = network_name(inst.name)
        _ensure_network(net)
        for svc in app.services:
            res = _spawn_service(inst, app, svc, net)
            if res.returncode != 0:
                return res  # surface the sidecar failure
        for svc in app.services:
            if not _wait_service(inst, svc):
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
    args += _mount_args(app.id, inst.user, app.mounts)

    # Dev bind mounts — bind a real host dir straight into the container (a DEV app
    # that runs live from a bind-mounted source tree). node_modules etc. are shadowed
    # by the image's own VOLUME declarations so the host tree isn't injected.
    for host_path, container_path in app.binds:
        args += ["-v", f"{host_path}:{container_path}"]

    # App-specific extra env (declared on the App Definition).
    for k, v in app.env.items():
        args += ["-e", f"{k}={v}"]

    from . import app_variants  # deferred: avoids a circular import at load time
    args.append(app_variants.active_image(app))
    return _docker(args, timeout=120)
