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
  * one job per app at a time, so a stuck download cannot be stacked up
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

# Runs in the container. Streams to a .part file and renames only on success,
# so a failed or killed download can never look like a complete model -- which
# would otherwise surface much later as a corrupt-checkpoint error.
_SCRIPT = r'''
import json, os, sys, urllib.request
url, dest = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(dest), exist_ok=True)
tmp = dest + ".part"
req = urllib.request.Request(url, headers={"User-Agent": "sandos-sm/1.0"})
tok = os.environ.get("HF_TOKEN", "")
if tok and ("huggingface.co" in url or "hf.co" in url):
    req.add_header("Authorization", "Bearer " + tok)
with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
    total = int(r.headers.get("Content-Length") or 0)
    done = last = 0
    while True:
        chunk = r.read(1 << 20)
        if not chunk:
            break
        f.write(chunk)
        done += len(chunk)
        if done - last >= (1 << 23):       # report every 8 MB, not every MB
            last = done
            print(json.dumps({"done": done, "total": total}), flush=True)
os.replace(tmp, dest)
print(json.dumps({"done": done, "total": total, "complete": True}), flush=True)
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


def _run(app_id: str, url: str, dest: str, job: dict) -> None:
    try:
        p = subprocess.Popen(
            ["docker", "exec", "-i", job["container"], "python", "-c", _SCRIPT, url, dest],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in p.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            with _lock:
                job.update(done=d.get("done", 0), total=d.get("total", 0))
                if d.get("complete"):
                    job.update(state="done", finished=time.time())
        err = (p.stderr.read() or "").strip()
        p.wait()
        with _lock:
            if job["state"] != "done":
                job.update(state="failed",
                           error=(err.splitlines() or ["download failed"])[-1][:300])
    except Exception as e:  # noqa: BLE001
        with _lock:
            job.update(state="failed", error=str(e)[:300])


def start(app_id: str, url: str, filename: str = "", subdir: str = "") -> dict:
    name, dest = _validate(app_id, url, filename, subdir)
    container = _container(app_id)
    if not container:
        raise RuntimeError(f"{app_id} is not running on this node")
    with _lock:
        cur = _jobs.get(app_id)
        if cur and cur["state"] == "running":
            raise RuntimeError(f"already downloading {cur['filename']}")
        job = {"state": "running", "filename": name, "dest": dest, "url": url,
               "container": container, "done": 0, "total": 0,
               "started": time.time(), "error": ""}
        _jobs[app_id] = job
    threading.Thread(target=_run, args=(app_id, url, dest, job), daemon=True).start()
    return {"ok": True, "filename": name, "dest": dest}


def status(app_id: str) -> dict:
    with _lock:
        j = _jobs.get(app_id)
        if not j:
            return {"state": "idle"}
        return {k: v for k, v in j.items() if k != "container"}
