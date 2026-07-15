"""Ollama model management — list, pull, delete, internet toggle, NAS transfer.

All model operations proxy to the LOCAL Ollama container via its API port, which
the SM discovers from the running instance (no hardcoded port). Pull and NAS
transfer operations run in background threads and expose job-id polling.

Internet toggle:
  Stored in a state file; applies an iptables OUTPUT rule on the container IP
  when Ollama is running. Requires a narrowly-scoped NOPASSWD sudoers rule:
    control ALL=(root) NOPASSWD: /usr/sbin/iptables -I OUTPUT -s * ! -d * ! -d * ! -d * ! -d * -j DROP
    control ALL=(root) NOPASSWD: /usr/sbin/iptables -D OUTPUT -s * ! -d * ! -d * ! -d * ! -d * -j DROP
  Without it, the toggle saves state but does not enforce the rule until the
  SM is restarted with the rule already in place.

NAS transfer:
  Uses Alpine Docker containers to rsync model blobs + manifests between the
  Ollama volume and a NAS staging directory — same pattern as app_storage.py.
  Requires SM_OLLAMA_NAS_TRANSFER to be set to the NAS mount point.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid as _uuid_mod
from typing import AsyncGenerator

import httpx

from . import config, registry

# ── Ollama connection ─────────────────────────────────────────────────────────

def ollama_url() -> str:
    """URL of the running Ollama container as seen from localhost."""
    inst = registry.get_instance("ollama", registry._SHARED)
    if not inst:
        raise RuntimeError("Ollama is not running — start it first")
    return f"http://127.0.0.1:{inst.web_port}"


def ollama_running() -> bool:
    inst = registry.get_instance("ollama", registry._SHARED)
    if not inst:
        return False
    from . import docker_backend, app_images
    return docker_backend.running(inst.name, host=app_images.active_docker_host("ollama"))


# ── Model list ────────────────────────────────────────────────────────────────

def list_models() -> list[dict]:
    """Installed models from Ollama's /api/tags. Returns [] if not running."""
    if not ollama_running():
        return []
    try:
        r = httpx.get(f"{ollama_url()}/api/tags", timeout=10.0)
        return r.json().get("models") or []
    except Exception:  # noqa: BLE001
        return []


def running_models() -> list[dict]:
    """Models currently loaded in GPU/CPU memory (/api/ps). Returns [] if not running."""
    if not ollama_running():
        return []
    try:
        r = httpx.get(f"{ollama_url()}/api/ps", timeout=10.0)
        return r.json().get("models") or []
    except Exception:  # noqa: BLE001
        return []


def node_llm_status() -> dict:
    """Snapshot of LLM capability for the Hub router to poll."""
    running = ollama_running()
    models = list_models() if running else []
    active = running_models() if running else []
    return {
        "ollama_running": running,
        "models": [{"name": m["name"], "size": m.get("size")} for m in models],
        "active_models": [m["name"] for m in active],
        "load_score": len(active),  # simple: more running = more busy
    }


# ── Streaming proxy (async generator) ────────────────────────────────────────

async def stream_to_ollama(path: str, body: dict) -> AsyncGenerator[bytes, None]:
    """Async generator that streams bytes from Ollama — used by FastAPI
    StreamingResponse for /api/apps/ollama/v1/* proxy endpoints."""
    url = ollama_url()
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST", f"{url}{path}", json=body,
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0),
        ) as resp:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk


async def fetch_models_openai() -> dict:
    """Model list in OpenAI /v1/models format — for the Hub router."""
    models = list_models()
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m["name"], "object": "model", "created": now, "owned_by": "ollama"}
            for m in models
        ],
    }


# ── Pull jobs ─────────────────────────────────────────────────────────────────

_pull_jobs: dict[str, dict] = {}
_pull_jobs_lock = threading.Lock()


def _new_pull_job(model_name: str) -> tuple[str, dict]:
    job_id = _uuid_mod.uuid4().hex
    job: dict = {
        "job_id": job_id, "model": model_name, "action": "pull",
        "done": False, "ok": False, "error": None,
        "status": "starting", "total_bytes": None, "bytes_pulled": 0,
    }
    with _pull_jobs_lock:
        _pull_jobs[job_id] = job
        done_ids = [k for k, v in _pull_jobs.items() if v["done"]]
        for k in done_ids[:-100]:
            del _pull_jobs[k]
    return job_id, job


def pull_job_status(job_id: str) -> dict | None:
    return _pull_jobs.get(job_id)


def _run_pull(job: dict, model_name: str) -> None:
    try:
        url = ollama_url()
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "POST", f"{url}/api/pull",
                json={"model": model_name, "stream": True},
            ) as resp:
                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue
                    try:
                        d = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    job["status"] = d.get("status", job["status"])
                    total = d.get("total")
                    completed = d.get("completed")
                    if total:
                        job["total_bytes"] = total
                    if completed is not None:
                        job["bytes_pulled"] = max(job["bytes_pulled"], completed)
                    if d.get("status") == "success":
                        job["ok"] = True
                        return
        if not job["ok"]:
            job["error"] = "pull ended without success status"
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
        job["ok"] = False
    finally:
        job["done"] = True


def start_pull(model_name: str) -> str:
    if not ollama_running():
        raise RuntimeError("Ollama is not running")
    job_id, job = _new_pull_job(model_name)
    t = threading.Thread(
        target=_run_pull, args=(job, model_name),
        daemon=True, name=f"ollama-pull-{job_id[:8]}",
    )
    t.start()
    return job_id


def delete_model(model_name: str) -> dict:
    if not ollama_running():
        raise RuntimeError("Ollama is not running")
    r = httpx.delete(f"{ollama_url()}/api/delete", json={"name": model_name}, timeout=30.0)
    if r.status_code not in (200, 204):
        raise RuntimeError(r.text[:200] or f"HTTP {r.status_code}")
    return {"ok": True, "deleted": model_name}


# ── Internet toggle ───────────────────────────────────────────────────────────

_INTERNET_STATE_FILE = os.path.join(config.NAS_ROOT, ".ollama-internet-enabled")


def get_internet_access() -> bool:
    return os.path.exists(_INTERNET_STATE_FILE)


def set_internet_access(enabled: bool) -> dict:
    """Toggle internet access for the Ollama container via iptables.

    Saves the desired state so the setting survives restarts. If Ollama is
    running, also applies the iptables rule immediately (requires sudoers).
    """
    applied = False
    note = None

    if enabled:
        try:
            os.makedirs(os.path.dirname(_INTERNET_STATE_FILE), exist_ok=True)
            open(_INTERNET_STATE_FILE, "w").close()
        except OSError:
            pass
    else:
        try:
            os.unlink(_INTERNET_STATE_FILE)
        except FileNotFoundError:
            pass

    if ollama_running():
        container_ip = _ollama_container_ip()
        if container_ip:
            result = _apply_internet_rule(enabled, container_ip)
            applied = result.returncode == 0
            if not applied:
                note = "iptables rule failed — check sudoers (see ollama_mgr.py header)"
        else:
            note = "could not detect container IP; state saved, restart Ollama to apply"
    else:
        note = "Ollama not running; state saved, will apply on next start"

    return {"ok": True, "internet_enabled": enabled, "applied": applied, "note": note}


def _ollama_container_ip() -> str:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", "sm-ollama"],
        capture_output=True, text=True, timeout=10,
    )
    return r.stdout.strip()


def _apply_internet_rule(allow: bool, container_ip: str) -> subprocess.CompletedProcess:
    # Allow: remove the block rule (if it doesn't exist, iptables returns 1 — that's ok)
    # Block: insert the block rule
    action = "-D" if allow else "-I"
    return subprocess.run(
        ["sudo", "-n", "/usr/sbin/iptables", action, "OUTPUT",
         "-s", container_ip,
         "!", "-d", "10.0.0.0/8",
         "!", "-d", "172.16.0.0/12",
         "!", "-d", "192.168.0.0/16",
         "!", "-d", "127.0.0.0/8",
         "-j", "DROP"],
        capture_output=True, timeout=10,
    )


def apply_internet_state_on_start() -> None:
    """Called after Ollama container starts — re-applies the saved internet state."""
    if not get_internet_access():
        container_ip = _ollama_container_ip()
        if container_ip:
            _apply_internet_rule(allow=False, container_ip=container_ip)


# ── NAS model transfer ────────────────────────────────────────────────────────

_TRANSFER_JOBS: dict[str, dict] = {}
_TRANSFER_JOBS_LOCK = threading.Lock()


def _new_transfer_job(model: str, direction: str) -> tuple[str, dict]:
    job_id = _uuid_mod.uuid4().hex
    job: dict = {
        "job_id": job_id, "model": model, "direction": direction,
        "done": False, "ok": False, "error": None,
        "status": "starting", "bytes_copied": 0, "total_bytes": None,
    }
    with _TRANSFER_JOBS_LOCK:
        _TRANSFER_JOBS[job_id] = job
        done_ids = [k for k, v in _TRANSFER_JOBS.items() if v["done"]]
        for k in done_ids[:-100]:
            del _TRANSFER_JOBS[k]
    return job_id, job


def transfer_job_status(job_id: str) -> dict | None:
    return _TRANSFER_JOBS.get(job_id)


def _ollama_volume_name() -> str:
    """Docker volume name backing the ollama-models mount."""
    return "sm-shared-ollama-models"


def _volume_data_path() -> str:
    return f"/var/lib/docker/volumes/{_ollama_volume_name()}/_data"


def _run_export(job: dict, model_name: str, nas_path: str) -> None:
    """Copy model blobs + manifest from the Ollama volume to NAS staging."""
    try:
        vol = _ollama_volume_name()
        staging = f"{nas_path}/{model_name.replace(':', '_')}"
        job["status"] = "copying to NAS"

        # Use Alpine to copy manifest + referenced blobs — same pattern as app_storage.
        # The manifest path follows Ollama's registry layout.
        # For a model like "llama3.2:3b" the manifest is at:
        #   models/manifests/registry.ollama.ai/library/llama3.2/3b
        script = r"""#!/bin/sh
set -e
MODEL="$1"
STAGING="$2"
OLLAMA_ROOT=/ollama

# Resolve name:tag
NAME=$(echo "$MODEL" | cut -d: -f1)
TAG=$(echo "$MODEL" | cut -d: -f2)
[ -z "$TAG" ] && TAG=latest

MANIFEST_DIR="$OLLAMA_ROOT/models/manifests/registry.ollama.ai/library/$NAME"
MANIFEST_FILE="$MANIFEST_DIR/$TAG"

if [ ! -f "$MANIFEST_FILE" ]; then
  echo "ERROR: manifest not found: $MANIFEST_FILE" >&2
  exit 1
fi

mkdir -p "$STAGING/manifests/registry.ollama.ai/library/$NAME"
mkdir -p "$STAGING/blobs"

cp "$MANIFEST_FILE" "$STAGING/manifests/registry.ollama.ai/library/$NAME/$TAG"

# Extract blob digests (sha256:...) and copy each blob file
grep -o '"sha256:[^"]*"' "$MANIFEST_FILE" | tr -d '"' | while read DIGEST; do
  BLOB_NAME=$(echo "$DIGEST" | tr ':' '-')
  SRC="$OLLAMA_ROOT/models/blobs/$BLOB_NAME"
  DST="$STAGING/blobs/$BLOB_NAME"
  if [ -f "$SRC" ] && [ ! -f "$DST" ]; then
    cp "$SRC" "$DST"
  fi
done
echo OK
"""
        proc = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{vol}:/ollama",
             "-v", f"{nas_path}:/nas",
             "alpine", "sh", "-c",
             script, "_", model_name, f"/nas/{model_name.replace(':', '_')}"],
            capture_output=True, text=True, timeout=3600,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "export failed")
        job["ok"] = True
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
        job["ok"] = False
    finally:
        job["done"] = True


def _run_import(job: dict, model_name: str, nas_path: str) -> None:
    """Copy model blobs + manifest from NAS staging into the Ollama volume."""
    try:
        vol = _ollama_volume_name()
        staging = f"{nas_path}/{model_name.replace(':', '_')}"
        job["status"] = "importing from NAS"

        script = r"""#!/bin/sh
set -e
MODEL="$1"
STAGING="$2"
OLLAMA_ROOT=/ollama

NAME=$(echo "$MODEL" | cut -d: -f1)
TAG=$(echo "$MODEL" | cut -d: -f2)
[ -z "$TAG" ] && TAG=latest

SRC_MANIFEST="$STAGING/manifests/registry.ollama.ai/library/$NAME/$TAG"
if [ ! -f "$SRC_MANIFEST" ]; then
  echo "ERROR: staging manifest not found: $SRC_MANIFEST" >&2
  exit 1
fi

mkdir -p "$OLLAMA_ROOT/models/manifests/registry.ollama.ai/library/$NAME"
mkdir -p "$OLLAMA_ROOT/models/blobs"

cp "$SRC_MANIFEST" "$OLLAMA_ROOT/models/manifests/registry.ollama.ai/library/$NAME/$TAG"

for BLOB in "$STAGING/blobs"/*; do
  [ -f "$BLOB" ] || continue
  BNAME=$(basename "$BLOB")
  DST="$OLLAMA_ROOT/models/blobs/$BNAME"
  if [ ! -f "$DST" ]; then
    cp "$BLOB" "$DST"
  fi
done
echo OK
"""
        proc = subprocess.run(
            ["docker", "run", "--rm",
             "-v", f"{vol}:/ollama",
             "-v", f"{nas_path}:/nas",
             "alpine", "sh", "-c",
             script, "_", model_name, f"/nas/{model_name.replace(':', '_')}"],
            capture_output=True, text=True, timeout=3600,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "import failed")
        job["ok"] = True
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
        job["ok"] = False
    finally:
        job["done"] = True


def start_export(model_name: str) -> str:
    """Export a model to NAS staging. Returns job_id."""
    if not config.OLLAMA_NAS_TRANSFER_PATH:
        raise ValueError("NAS transfer path not configured (SM_OLLAMA_NAS_TRANSFER)")
    models = [m["name"] for m in list_models()]
    # Allow partial match (e.g. "llama3.2" matches "llama3.2:3b")
    if model_name not in models:
        raise KeyError(f"model {model_name!r} not found on this node")
    job_id, job = _new_transfer_job(model_name, "export")
    t = threading.Thread(
        target=_run_export,
        args=(job, model_name, config.OLLAMA_NAS_TRANSFER_PATH),
        daemon=True, name=f"ollama-export-{job_id[:8]}",
    )
    t.start()
    return job_id


def start_import(model_name: str) -> str:
    """Import a model from NAS staging. Returns job_id."""
    if not config.OLLAMA_NAS_TRANSFER_PATH:
        raise ValueError("NAS transfer path not configured (SM_OLLAMA_NAS_TRANSFER)")
    staging_dir = os.path.join(config.OLLAMA_NAS_TRANSFER_PATH, model_name.replace(":", "_"))
    if not os.path.isdir(staging_dir):
        raise FileNotFoundError(f"no staged model at {staging_dir}")
    job_id, job = _new_transfer_job(model_name, "import")
    t = threading.Thread(
        target=_run_import,
        args=(job, model_name, config.OLLAMA_NAS_TRANSFER_PATH),
        daemon=True, name=f"ollama-import-{job_id[:8]}",
    )
    t.start()
    return job_id
