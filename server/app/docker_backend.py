"""Docker backend — spawn / stop / inspect FreeCAD-streamer instances via the
docker CLI (no extra SDK dependency). Mirrors the proven run-lan.sh parameters,
but with per-instance ports so concurrent instances don't collide."""
from __future__ import annotations
import subprocess
import urllib.error
import urllib.request
from .models import AppDef, Instance
from . import config


def web_ready(port: int) -> bool:
    """True once the instance's web server answers (any HTTP status, incl. 401)."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True  # e.g. 401 auth challenge — the server is up
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


def published_web_port(name: str) -> int | None:
    """The host port mapped to the container's web port (8080), or None."""
    r = _docker(["inspect", "-f",
                 '{{with index .NetworkSettings.Ports "8080/tcp"}}{{(index . 0).HostPort}}{{end}}',
                 name], timeout=10)
    try:
        return int(r.stdout.strip())
    except (ValueError, AttributeError):
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


def spawn(inst: Instance, app: AppDef) -> subprocess.CompletedProcess:
    """Start a streamed instance. Returns the docker run CompletedProcess."""
    args = ["run", "--name", inst.name, "-d", "--rm"]
    if app.gpu:
        args += ["--device", "nvidia.com/gpu=all", "-e", "NVIDIA_DRIVER_CAPABILITIES=all"]
    args += [
        "-p", f"{inst.web_port}:8080",
        "-p", f"{inst.turn_port}:{inst.turn_port}/tcp",
        "-p", f"{inst.turn_port}:{inst.turn_port}/udp",
        "-p", f"{inst.relay_min}-{inst.relay_max}:{inst.relay_min}-{inst.relay_max}/udp",
        "--tmpfs", "/dev/shm:rw",
        "-e", "TZ=UTC",
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
        "-v", f"{inst.volume}:/mnt/freecad-projects",
        app.image,
    ]
    return _docker(args, timeout=90)
