"""App catalogue + instance lifecycle.

Holds the App Definitions, allocates per-instance ports/volumes, and resolves
launch/stop/status against the Docker backend. State is in-memory and
reconciled from Docker on startup (single-node MVP)."""
from __future__ import annotations
import re
from .models import AppDef, Instance
from . import config, docker_backend

# ── App catalogue (MVP: FreeCAD; add more App Definitions here) ────────────────
APPS: dict[str, AppDef] = {
    "freecad": AppDef(
        id="freecad",
        label="FreeCAD",
        icon="cpu",
        color="blue",
        desc="Full FreeCAD 1.1.1, streamed — your own GPU instance.",
        image=config.FREECAD_IMAGE,
        mode="per-user",
        gpu=True,
        encoder="nvh264enc",
        keepalive_seconds=600,
    ),
}

# slot -> (app_id, user)  ;  (app_id, user) -> Instance
_slots: dict[int, tuple[str, str]] = {}
_instances: dict[tuple[str, str], Instance] = {}


def _safe(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def instance_name(app_id: str, user: str) -> str:
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
    return _instances.get((app_id, user))


def url_for(inst: Instance) -> str:
    return f"http://{config.LAN_IP}:{inst.web_port}"


def status(app_id: str, user: str) -> str:
    """stopped | starting (running, web not ready) | active (connected) | idle."""
    inst = _instances.get((app_id, user))
    if not inst or not docker_backend.running(inst.name):
        return "stopped"
    if not docker_backend.web_ready(inst.web_port):
        return "starting"
    return "active" if docker_backend.active_connections(inst.web_port) > 0 else "idle"


def launch(app_id: str, user: str) -> Instance:
    if app_id not in APPS:
        raise KeyError(app_id)
    inst = _instance_for(app_id, user)
    if not docker_backend.running(inst.name):
        res = docker_backend.spawn(inst, APPS[app_id])
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "docker run failed")
    return inst


def stop(app_id: str, user: str) -> None:
    inst = _instances.get((app_id, user))
    name = inst.name if inst else instance_name(app_id, user)
    docker_backend.stop(name)


def list_for_user(user: str) -> list[dict]:
    """App catalogue with this user's per-app status + URL (if running)."""
    out = []
    for app in APPS.values():
        st = status(app.id, user)
        inst = _instances.get((app.id, user))
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
        m = re.match(r"^sm-([a-z0-9-]+?)-(.+)$", name)
        if not m:
            continue
        app_id, user = m.group(1), m.group(2)
        if app_id not in APPS:
            continue
        web_port = docker_backend.published_web_port(name)
        if web_port is None:
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
