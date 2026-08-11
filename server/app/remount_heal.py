"""Recreate containers whose view of the mesh NAS died with a mount restart.

A FUSE mount is a live kernel connection, and a container's bind mount points at
that specific connection. Restart the mount — an upgrade, a crash, a flag change
— and every running container keeps the OLD one: paths under it answer ENOTCONN
("Transport endpoint is not connected") for as long as the container lives.
`docker restart` does not help, because the bind descriptor is fixed when the
container is CREATED. It has to be recreated.

Nothing reports this. Docker's healthcheck does not touch the mount, so the
container sits there looking fine while serving an empty directory. Found live
on 2026-08-11: Open WebUI had been in exactly this state for SEVEN DAYS, listed
as "Up 7 days (unhealthy)", with nothing in any log saying why.

This lives inside the Server Manager rather than in a standalone script for one
concrete reason: the instance registry is IN-PROCESS state. A separate process
importing `registry` sees an empty `_instances` and cannot map a container back
to the (app, user) needed to relaunch it — tried, and it reports every stale
container as unknown.

DETECTION, NOT ASSUMPTION. Each container's actual view is probed rather than
assuming everything bound to the mount is broken:

  * at boot the mount comes up before the apps, so nothing is stale and a
    blanket relaunch would restart every app for nothing;
  * staleness is judged on the ENOTCONN error, never on a directory being
    empty — an empty directory is a normal thing for a container to see.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

from . import config

log = logging.getLogger("sm.remount_heal")

MOUNT = config.NAS_MESH_MOUNT
_PROBE_TIMEOUT = 8
# Long enough that this is never mistaken for a health poll, short enough that a
# mount restart is repaired before someone opens the app and finds it empty.
_INTERVAL = 300
_STARTUP_DELAY = 60


def _sh(*args: str, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.TimeoutExpired, OSError) as e:  # noqa: BLE001
        return 1, str(e)


def _bound_containers() -> list[tuple[str, str]]:
    """(container, a destination under the mount) for running containers that
    bind-mount the mesh NAS."""
    rc, out = _sh("docker", "ps", "--format", "{{.Names}}")
    if rc != 0:
        return []
    found = []
    for name in out.split():
        rc, insp = _sh("docker", "inspect", name, "--format",
                       "{{range .Mounts}}{{.Type}}|{{.Source}}|{{.Destination}}\n{{end}}")
        if rc != 0:
            continue
        for line in insp.splitlines():
            parts = line.split("|")
            if len(parts) == 3 and parts[0] == "bind" and parts[1].startswith(MOUNT):
                found.append((name, parts[2]))
                break
    return found


def _is_stale(container: str, dest: str) -> bool:
    rc, out = _sh("docker", "exec", container, "ls", dest, timeout=_PROBE_TIMEOUT)
    if rc == 0:
        return False
    low = out.lower()
    return ("not connected" in low or "transport endpoint" in low)


def scan(repair: bool = True) -> dict:
    """Find — and by default repair — containers holding a dead mount."""
    from . import registry

    if not MOUNT:
        return {"checked": 0, "stale": [], "repaired": [], "failed": []}
    rc, _ = _sh("mountpoint", "-q", MOUNT)
    if rc != 0:
        return {"checked": 0, "stale": [], "repaired": [], "failed": [],
                "note": f"{MOUNT} is not mounted"}

    bound = _bound_containers()
    stale = [(n, d) for n, d in bound if _is_stale(n, d)]
    out = {"checked": len(bound), "stale": [n for n, _ in stale],
           "repaired": [], "failed": []}
    if not stale or not repair:
        return out

    by_name = {i["name"]: (i["app_id"], i["user"])
               for i in registry.instances_summary()}
    for name, dest in stale:
        entry = by_name.get(name)
        if not entry:
            log.warning("remount_heal: %s holds a dead mount but is not in the "
                        "registry — relaunch it from the dashboard", name)
            out["failed"].append(name)
            continue
        app_id, user = entry
        log.warning("remount_heal: recreating %s (%s/%s) — its view of %s is dead",
                    name, app_id, user, dest)
        try:
            registry.stop(app_id, user)
            time.sleep(3)
            registry.launch(app_id, user)
            time.sleep(4)
            if _is_stale(name, dest):
                log.error("remount_heal: %s still stale after relaunch", name)
                out["failed"].append(name)
            else:
                log.info("remount_heal: %s recovered", name)
                out["repaired"].append(name)
        except Exception as e:  # noqa: BLE001
            log.exception("remount_heal: %s failed: %s", name, e)
            out["failed"].append(name)
    return out


def start() -> None:
    """Background loop. Deliberately quiet: it logs only when it finds
    something, so its silence in the journal means 'nothing was broken'."""
    def _run() -> None:
        time.sleep(_STARTUP_DELAY)
        while True:
            try:
                r = scan(repair=True)
                if r.get("stale"):
                    log.warning("remount_heal: stale=%s repaired=%s failed=%s",
                                r["stale"], r["repaired"], r["failed"])
            except Exception:  # noqa: BLE001
                log.exception("remount_heal: scan failed")
            time.sleep(_INTERVAL)

    threading.Thread(target=_run, daemon=True, name="sm-remount-heal").start()
