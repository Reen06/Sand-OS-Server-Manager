"""Lightweight node metrics for the fleet view — no psutil, just /proc + statvfs
+ nvidia-smi. Reported by /api/sm/info so the Hub's Fleet page can show each
node's CPU / RAM / disk / GPU usage and status."""
from __future__ import annotations
import os
import shutil
import subprocess
import time


def _cpu_percent() -> float | None:
    """Whole-node CPU utilisation over a short sample."""
    def read():
        with open("/proc/stat") as f:
            nums = list(map(int, f.readline().split()[1:9]))
        return nums[3] + nums[4], sum(nums)      # idle+iowait, total
    try:
        i1, t1 = read()
        time.sleep(0.1)
        i2, t2 = read()
        dt = t2 - t1
        return round(100 * (dt - (i2 - i1)) / dt, 1) if dt > 0 else 0.0
    except Exception:  # noqa: BLE001
        return None


def _mem() -> dict | None:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                info[k] = int(rest.split()[0]) * 1024      # kB → bytes
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        return {"total": total, "used": used,
                "percent": round(100 * used / total, 1) if total else 0}
    except Exception:  # noqa: BLE001
        return None


def _is_wsl() -> bool:
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except Exception:  # noqa: BLE001
        return False


def _disk_path() -> str:
    """`/` under WSL2 is its own dynamically-expanding VIRTUAL disk image,
    not the real Windows drive underneath it — confirmed live: reported
    ~1TB total/free on a node whose actual C: drive had only ~480GB free,
    because the virtual disk itself happened to be provisioned at ~1TB.
    /mnt/c is WSL2's passthrough mount of the real drive, so it reflects
    genuine physical capacity instead."""
    if _is_wsl() and os.path.isdir("/mnt/c"):
        return "/mnt/c"
    return "/"


def _disk(path: str | None = None) -> dict | None:
    try:
        st = os.statvfs(path or _disk_path())
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return {"total": total, "used": used,
                "percent": round(100 * used / total, 1) if total else 0}
    except Exception:  # noqa: BLE001
        return None


_NVIDIA_SMI_FALLBACKS = [
    # WSL2's own NVIDIA driver shim lives here — added to PATH only by
    # interactive shell startup files (.bashrc / /etc/profile.d), which a
    # systemd service never inherits (confirmed live: `systemctl show
    # sandos-server-manager --property=Environment` is empty). shutil.which()
    # alone silently found nothing under the running service even though
    # nvidia-smi worked fine and the GPU was correctly detected elsewhere
    # (config.py's own detection has a PATH-independent /proc/driver/nvidia
    # fallback for exactly this reason) — this one didn't, until now.
    "/usr/lib/wsl/lib/nvidia-smi",
]


def _nvidia_smi_path() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for candidate in _NVIDIA_SMI_FALLBACKS:
        if os.path.exists(candidate):
            return candidate
    return None


def _gpu() -> dict | None:
    nvidia_smi = _nvidia_smi_path()
    if not nvidia_smi:
        return None
    try:
        r = subprocess.run(
            [nvidia_smi, "--query-gpu=utilization.gpu,memory.used,memory.total,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        util, mused, mtotal, name = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        mtot = float(mtotal)
        return {"util": float(util), "mem_used_mb": float(mused), "mem_total_mb": mtot,
                "mem_percent": round(100 * float(mused) / mtot, 1) if mtot else 0,
                "name": name}
    except Exception:  # noqa: BLE001
        return None


def _uptime() -> int | None:
    try:
        with open("/proc/uptime") as f:
            return int(float(f.readline().split()[0]))
    except Exception:  # noqa: BLE001
        return None


def collect() -> dict:
    return {
        "cpu_percent": _cpu_percent(),
        "cpu_count": os.cpu_count(),
        "mem": _mem(),
        "disk": _disk(),
        "gpu": _gpu(),
        "uptime": _uptime(),
    }


def top_processes(limit: int = 60) -> list[dict]:
    """Running processes with CPU% + memory, via `ps` (no psutil). `cpu` is ps's
    lifetime-average %CPU; `mem` is %RAM and `rss` is resident bytes (both live).
    Sorted by CPU; the Hub UI can re-sort/filter."""
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,%cpu,%mem,rss,comm", "--sort=-%cpu", "--no-headers"],
            capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, cpu, mem, rss, comm = parts
        try:
            out.append({"pid": int(pid), "cpu": float(cpu), "mem": float(mem),
                        "rss": int(rss) * 1024, "name": comm})
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out
