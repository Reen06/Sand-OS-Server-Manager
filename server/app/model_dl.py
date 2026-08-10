"""Fetch a model file onto THIS node, into a running app's model directory.

ComfyUI has no server-side downloader, and ComfyUI-Manager's only installs
models that appear in its curated model-list.json -- checked against a real
template's models and matched none of 1076 entries. So neither can pull a model
the app itself is asking for, and the browser is left to download gigabytes to
whatever device is holding it. This closes that gap.

The transfer runs INSIDE the app's container rather than on the host. The model
directory is a docker volume owned by root; the Server Manager runs as an
ordinary user and cannot write there, while the container already has it mounted
at the path the app reads from. Doing it in there also means the file lands with
the ownership the app expects, with no chown dance afterwards.

Bounded on purpose -- this takes a URL from a browser and writes it to disk:
  * https only, and only from hosts that actually serve model weights
  * the filename is reduced to a basename, so no path can escape the directory
  * the subdirectory must be one ComfyUI actually reads
  * an admin-only endpoint, and a model already present is never refetched
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlparse

# Where each app keeps its models, inside its own container.
_MODEL_ROOT = {"comfyui": "/opt/ComfyUI/models"}

# Hosts that serve model weights. Not a general fetcher: without this the
# endpoint would be an open request-forwarder into the node's network.
_ALLOWED_HOSTS = {
    "huggingface.co", "hf.co", "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.hf.co", "civitai.com", "github.com",
    "objects.githubusercontent.com", "raw.githubusercontent.com",
}

# Directories ComfyUI actually loads from. An unknown one would silently put a
# file where nothing ever looks for it.
_DIRS = {
    "checkpoints", "unet", "diffusion_models", "vae", "clip", "text_encoders",
    "clip_vision", "loras", "controlnet", "upscale_models", "embeddings",
    "style_models", "gligen", "hypernetworks", "photomaker", "audio_encoders",
    "model_patches", "vae_approx",
}

_lock = threading.Lock()
_jobs: dict[str, dict] = {}

# Runs detached inside the container, reporting into a status file rather than
# over a pipe. A pipe made the transfer a child of the Server Manager, so
# restarting SM -- or anything that recycled it -- killed a multi-gigabyte
# download at whatever percent it had reached. A status file also means progress
# survives SM restarting and can still be read afterwards.
_SCRIPT = r'''
import json, os, sys, time, urllib.request
url, dest, stat = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(os.path.dirname(dest), exist_ok=True)
os.makedirs(os.path.dirname(stat), exist_ok=True)
name = os.path.basename(dest)

def report(**kw):
    kw.setdefault("filename", name)
    kw.setdefault("at", int(time.time()))
    with open(stat + ".tmp", "w") as f:
        json.dump(kw, f)
    os.replace(stat + ".tmp", stat)

try:
    # Already here: say so instead of spending an hour fetching it again. This
    # is the common case when a template lists several models and only some are
    # missing -- re-downloading a 5 GB file nobody asked for is worse than
    # useless, it costs the bandwidth the missing one needs.
    if os.path.exists(dest):
        report(state="done", done=os.path.getsize(dest),
               total=os.path.getsize(dest), already=True)
        sys.exit(0)

    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "sandos-sm/1.0"})
    tok = os.environ.get("HF_TOKEN", "")
    if tok and ("huggingface.co" in url or "hf.co" in url):
        req.add_header("Authorization", "Bearer " + tok)
    # Resume where an interrupted attempt stopped. These files are gigabytes and
    # the machine is not always stable; starting from zero every time is what
    # turns one bad moment into an unfinishable download.
    if have:
        req.add_header("Range", "bytes=%d-" % have)
    report(state="running", done=have, total=0)

    with urllib.request.urlopen(req, timeout=60) as r:
        resumed = r.status == 206
        total = int(r.headers.get("Content-Length") or 0) + (have if resumed else 0)
        if not resumed:
            have = 0                       # server ignored Range: start over
        done = have
        last = 0.0
        with open(tmp, "ab" if resumed else "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                # Time-based, not byte-based. A byte threshold reports
                # constantly on a fast link and almost never on a slow one --
                # the bar either flickers or sits dead. Twice a second gives
                # the page something fresh on every poll at any speed.
                now = time.time()
                if now - last >= 0.5:
                    last = now
                    report(state="running", done=done, total=total)
    os.replace(tmp, dest)
    report(state="done", done=done, total=total)
except Exception as e:
    report(state="failed", error=str(e)[:300])
    sys.exit(1)
'''


def _container(app_id: str) -> str:
    from . import registry
    name = registry.instance_name(app_id, "")     # shared-aware
    out = subprocess.run(["docker", "ps", "-q", "--filter", f"name=^{name}$"],
                         capture_output=True, text=True, timeout=15).stdout.strip()
    return name if out else ""


def _validate(app_id: str, url: str, filename: str, subdir: str) -> tuple[str, str]:
    if app_id not in _MODEL_ROOT:
        raise ValueError(f"{app_id} has no known model directory")
    u = urlparse(url)
    if u.scheme != "https":
        raise ValueError("only https URLs can be fetched")
    if u.hostname not in _ALLOWED_HOSTS:
        raise ValueError(f"{u.hostname} is not a recognised model host")
    name = os.path.basename(filename or u.path.split("?")[0]) or ""
    # Basename first, THEN character check: the point is that nothing which
    # could traverse a directory survives, whatever it was called.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", name):
        raise ValueError("unusable filename")
    if not name.lower().endswith((".safetensors", ".ckpt", ".pt", ".pth",
                                  ".bin", ".gguf", ".sft", ".onnx")):
        raise ValueError("not a model file")
    d = (subdir or "checkpoints").strip("/")
    if d not in _DIRS:
        raise ValueError(f"unknown model directory {d!r}")
    return name, f"{_MODEL_ROOT[app_id]}/{d}/{name}"


def _statfile(app_id: str, name: str) -> str:
    return f"{_MODEL_ROOT[app_id]}/.sm-downloads/{name}.json"


def start(app_id: str, url: str, filename: str = "", subdir: str = "") -> dict:
    name, dest = _validate(app_id, url, filename, subdir)
    container = _container(app_id)
    if not container:
        raise RuntimeError(f"{app_id} is not running on this node")
    # Detached, and deliberately NOT one-at-a-time. A template routinely lists
    # several missing models; refusing the second because the first is still
    # running surfaced in the page as "download failed", which is both wrong and
    # the opposite of helpful. They run alongside each other and each reports
    # its own progress.
    stat = _statfile(app_id, name)
    subprocess.run(["docker", "exec", "-d", container, "python", "-c",
                    _SCRIPT, url, dest, stat], check=True, timeout=30)
    with _lock:
        _jobs[app_id] = {"filename": name, "started": time.time()}
    return {"ok": True, "filename": name, "dest": dest}


def status(app_id: str) -> dict:
    """Progress of every download this app has going, newest first.

    Read from the container rather than from memory, so it survives the Server
    Manager restarting -- which, with detached transfers, no longer stops them.
    """
    if app_id not in _MODEL_ROOT:
        return {"state": "idle", "jobs": []}
    container = _container(app_id)
    if not container:
        return {"state": "idle", "jobs": []}
    d = f"{_MODEL_ROOT[app_id]}/.sm-downloads"
    out = subprocess.run(
        ["docker", "exec", container, "sh", "-c",
         f"cat {d}/*.json 2>/dev/null | sed 's/}}{{/}}\\n{{/g'"],
        capture_output=True, text=True, timeout=20).stdout
    jobs = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            jobs.append(json.loads(line))
        except ValueError:
            continue
    jobs.sort(key=lambda j: j.get("at", 0), reverse=True)
    if not jobs:
        return {"state": "idle", "jobs": []}
    live = next((j for j in jobs if j.get("state") == "running"), None)
    head = live or jobs[0]
    return {**head, "jobs": jobs}


# ── What is on disk ──────────────────────────────────────────────────────────
# Read inside the container for the same reason downloads write there: the model
# directory is a root-owned volume the Server Manager cannot see from the host.
_LIST_SCRIPT = r'''
import json, os, shutil, sys
root = sys.argv[1]
out = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
    for fn in filenames:
        if fn.startswith(".") or fn.endswith(".json"):
            continue
        full = os.path.join(dirpath, fn)
        try:
            st = os.stat(full)
        except OSError:
            continue
        rel = os.path.relpath(dirpath, root)
        out.append({"folder": "" if rel == "." else rel, "name": fn,
                    "size": st.st_size, "mtime": int(st.st_mtime),
                    # A .part is an interrupted download, not a usable model.
                    # Shown so the space it holds is accounted for and can be
                    # reclaimed, rather than being invisible weight on the disk.
                    "partial": fn.endswith(".part")})
try:
    du = shutil.disk_usage(root)
    disk = {"free": du.free, "total": du.total}
except Exception:
    disk = {}
print(json.dumps({"models": out, "disk": disk}))
'''


def list_models(app_id: str) -> dict:
    if app_id not in _MODEL_ROOT:
        return {"models": [], "disk": {}, "used": 0}
    container = _container(app_id)
    if not container:
        return {"models": [], "disk": {}, "used": 0, "error": "app not running"}
    r = subprocess.run(["docker", "exec", container, "python", "-c",
                        _LIST_SCRIPT, _MODEL_ROOT[app_id]],
                       capture_output=True, text=True, timeout=60)
    try:
        d = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"models": [], "disk": {}, "used": 0,
                "error": (r.stderr or "could not read models")[-200:]}
    d["used"] = sum(m["size"] for m in d.get("models", []))
    d["models"].sort(key=lambda m: m["size"], reverse=True)
    return d


def delete_model(app_id: str, folder: str, name: str) -> dict:
    """Remove one model file.

    Validated the same way a download is: the folder must be one ComfyUI reads
    from and the name is reduced to a basename, so nothing here can be pointed
    at a path outside the model tree however it is spelled.
    """
    if app_id not in _MODEL_ROOT:
        raise ValueError(f"{app_id} has no known model directory")
    folder = (folder or "").strip("/")
    if folder and folder not in _DIRS:
        raise ValueError(f"unknown model directory {folder!r}")
    base = os.path.basename(name or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,220}", base):
        raise ValueError("unusable filename")
    container = _container(app_id)
    if not container:
        raise RuntimeError(f"{app_id} is not running on this node")
    root = _MODEL_ROOT[app_id]
    path = f"{root}/{folder}/{base}" if folder else f"{root}/{base}"
    r = subprocess.run(
        ["docker", "exec", container, "python", "-c",
         # Re-checked inside the container against the resolved real path: the
         # validation above is about the request, this is about where the path
         # actually lands once symlinks are followed.
         "import os,sys;p=os.path.realpath(sys.argv[1]);r=os.path.realpath(sys.argv[2]);"
         "sys.exit(2) if not p.startswith(r + os.sep) else None;"
         "os.remove(p);print('ok')", path, root],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "delete failed").strip()[-200:])
    return {"ok": True, "removed": f"{folder}/{base}" if folder else base}
