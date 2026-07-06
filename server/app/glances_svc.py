"""Local Glances REST server management + a curated monitor snapshot.

We run `glances -w` (REST only, localhost) alongside the Server Manager so the
Hub's Fleet page can show a rich, btop-like live monitor (per-core CPU, memory,
load, network, and a full process list) without embedding a terminal. Glances
is started on demand and reused if an earlier instance is already listening
(so a hard SM restart doesn't spawn a duplicate).
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

GLANCES_PORT = 61208
_BASE = f"http://127.0.0.1:{GLANCES_PORT}/api/4"
_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def _glances_bin() -> str:
    # Installed in the same venv as this process.
    return str(Path(sys.executable).with_name("glances"))


def _reachable() -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", GLANCES_PORT))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        s.close()


def start() -> None:
    """Ensure a local Glances REST server is running (reuse if already up)."""
    global _proc
    with _lock:
        if _reachable():
            return
        if _proc and _proc.poll() is None:
            return
        try:
            _proc = subprocess.Popen(
                [_glances_bin(), "-w", "--disable-webui",
                 "-B", "127.0.0.1", "-p", str(GLANCES_PORT), "-t", "2"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:  # noqa: BLE001
            _proc = None


def stop() -> None:
    global _proc
    with _lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
        _proc = None


def _get(path: str):
    try:
        with urllib.request.urlopen(f"{_BASE}/{path}", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def _slim_proc(p: dict) -> dict:
    mi = p.get("memory_info") or {}
    name = p.get("name") or ((p.get("cmdline") or [""]) or [""])[0]
    return {
        "pid": p.get("pid"),
        "name": name,
        "user": p.get("username"),
        "cpu": round(p.get("cpu_percent") or 0.0, 1),
        "mem": round(p.get("memory_percent") or 0.0, 1),
        "rss": mi.get("rss") or 0,
        "threads": p.get("num_threads"),
        "status": p.get("status"),
    }


def monitor(limit: int = 60) -> dict | None:
    """A curated snapshot for the Fleet monitor panel, or None if not ready."""
    start()
    cpu = _get("cpu")
    if cpu is None:
        return None  # glances still warming up
    procs = _get("processlist") or []
    procs = sorted(procs, key=lambda p: p.get("cpu_percent") or 0.0, reverse=True)[:limit]
    mem = _get("mem") or {}
    net = []
    for n in (_get("network") or []):
        nm = n.get("interface_name")
        if not nm or nm == "lo":
            continue
        net.append({"name": nm,
                    "rx": n.get("bytes_recv_rate_per_sec") or 0,
                    "tx": n.get("bytes_sent_rate_per_sec") or 0})
    return {
        "cpu": {"total": cpu.get("total"), "user": cpu.get("user"),
                "system": cpu.get("system"), "iowait": cpu.get("iowait")},
        "percpu": [{"n": c.get("cpu_number"), "total": c.get("total")}
                   for c in (_get("percpu") or [])],
        "mem": {"percent": mem.get("percent"), "used": mem.get("used"),
                "total": mem.get("total")} if mem else None,
        "load": _get("load"),
        "network": net[:6],
        "processes": [_slim_proc(p) for p in procs],
    }
