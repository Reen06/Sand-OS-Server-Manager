"""Move an app's finished output off an untrusted node as it is produced.

Only runs on an APP-ONLY node. There, an app's output mount is a subdirectory
of its staging area (docker_backend._mesh_path), and this sweeper moves each
completed file out of the part the container mounts (`work/<leaf>`) into a
sibling it does not (`collected/<leaf>`). nas_staging.collect() then copies the
whole staging directory into the owner's home when access is revoked.

What this does and does not buy:

  - The app cannot read back what it produced. A picture is written once and is
    gone from the app's view a moment later, so a compromised or nosy app
    cannot re-read an hour of someone's work.
  - A root administrator on the node can still read the file. It is on their
    disk (or their mount) either way. This narrows the WINDOW, not the reach —
    the same honest caveat nas_staging carries, and it must not be described as
    isolation it is not.

The cost is real and was chosen deliberately: on such a node ComfyUI cannot
show you your own generations, because by the time the browser asks for the
preview the file is no longer somewhere ComfyUI can see. On a trusted node
nothing here runs and the app behaves normally.

WRITES ARE NOT ATOMIC. ComfyUI saves a PNG straight to its final name rather
than writing a temporary file and renaming, so a file that merely EXISTS may
still be half-written. Moving one then would truncate someone's picture and
there would be no second copy. So a file is only moved once its size and mtime
have been unchanged across two consecutive polls — the cheapest check that
distinguishes "finished" from "still being written" without cooperation from
the app.
"""
from __future__ import annotations

import os
import shutil
import threading
import time

from . import config, nas_scope

# Poll interval. Short enough that a finished picture leaves promptly, long
# enough that a stat() walk over a handful of directories costs nothing.
_INTERVAL = 2.0

_thread: threading.Thread | None = None
_stop = threading.Event()

# path -> (size, mtime) as of the previous poll. A file is moved when it is seen
# twice running with identical numbers.
_seen: dict[str, tuple[int, float]] = {}

# Names an app writes as scaffolding rather than output. Moving these would
# make the app recreate them endlessly.
_IGNORE = {".sandos-staging.json", "_output_images_will_be_put_here"}


def _targets() -> list[tuple[str, str]]:
    """(work_dir, collected_dir) for every running instance whose app declares a
    hide_outputs mount. Resolved fresh each pass: instances start and stop, and
    a stale path would sweep a directory that is no longer anybody's."""
    from . import docker_backend, registry
    out: list[tuple[str, str]] = []
    for (app_id, user), _inst in list(registry._instances.items()):
        app = registry.APPS.get(app_id)
        if not app:
            continue
        for m in app.mounts:
            if not getattr(m, "hide_outputs", False):
                continue
            work = docker_backend._mesh_target(user, m, app_id)
            if not work:
                continue
            # collected/ sits beside work/, one level up from this mount's own
            # directory: <instance>/work/<leaf> -> <instance>/collected/<leaf>
            leaf = os.path.basename(work.rstrip("/"))
            inst_dir = os.path.dirname(os.path.dirname(work.rstrip("/")))
            out.append((work, os.path.join(inst_dir, "collected", leaf)))
    return out


def _move_stable(work: str, collected: str) -> list[str]:
    """Move every file that finished being written, preserving subdirectories."""
    moved: list[str] = []
    for root, _dirs, files in os.walk(work):
        for name in files:
            if name in _IGNORE or name.endswith(".part") or name.startswith("."):
                continue
            src = os.path.join(root, name)
            try:
                st = os.stat(src)
            except OSError:
                continue                      # vanished between walk and stat
            key = (st.st_size, st.st_mtime)
            if _seen.get(src) != key:
                _seen[src] = key              # first sighting, or still growing
                continue
            rel = os.path.relpath(src, work)
            dst = os.path.join(collected, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                # Never overwrite: two runs can produce the same filename, and
                # silently replacing the older one loses work with no trace.
                if os.path.exists(dst):
                    stem, ext = os.path.splitext(dst)
                    dst = f"{stem}-{int(time.time())}{ext}"
                shutil.move(src, dst)
                moved.append(rel)
            except OSError:
                continue                      # try again next pass
            _seen.pop(src, None)
    return moved


def sweep_once() -> int:
    """One pass. Returns how many files were moved. Safe to call directly —
    the tests and the loop use the same entry point."""
    total = 0
    for work, collected in _targets():
        total += len(_move_stable(work, collected))
    # Forget files that no longer exist, so _seen cannot grow without bound on
    # a long-running node.
    for path in [p for p in _seen if not os.path.exists(p)]:
        _seen.pop(path, None)
    return total


def _loop() -> None:
    while not _stop.wait(_INTERVAL):
        try:
            sweep_once()
        except Exception:  # noqa: BLE001
            # A sweep failing must never take the service down with it; the
            # next pass retries, and the files stay where they are meanwhile.
            pass


def start() -> bool:
    """Start the sweeper if this node is app-only. Returns whether it started.

    Asked once at startup rather than every pass: a node's trust level does
    change, but it changes by an administrator's action, and both directions
    already require the apps to be restarted for their mounts to move.
    """
    global _thread
    if _thread is not None:
        return True
    if not config.NAS_ENABLED or not nas_scope.is_app_only():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="output-sweep", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    global _thread
    _stop.set()
    _thread = None
