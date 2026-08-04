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
import re
import subprocess
import threading
import time
import uuid as _uuid_mod
from typing import AsyncGenerator

import httpx

from . import config, registry

# How long a streamed response may go completely silent (no bytes at all)
# before we give up on it — NOT a cap on total response duration; httpx's
# `read` timeout resets on every chunk received, so a real generation that
# keeps producing tokens runs as long as it needs to (20-30+ min is fine).
# This only fires on genuine dead air, which a real hang should hit well
# within two minutes even accounting for a slow model's first-token latency.
STREAM_IDLE_TIMEOUT = 120.0

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


def unload_model(name: str) -> tuple[bool, str]:
    """Evict one model from memory now.

    Ollama has no explicit unload call — the documented way is a generate
    request with keep_alive=0, which loads nothing and drops whatever is
    resident for that model. Mirrors what Open WebUI's own model manager
    does when it isn't proxied through us.
    """
    if not ollama_running():
        return False, "Ollama is not running"
    try:
        r = httpx.post(f"{ollama_url()}/api/generate",
                       json={"model": name, "keep_alive": 0},
                       timeout=30.0)
        if r.status_code >= 400:
            return False, (r.text or "")[:200] or f"HTTP {r.status_code}"
        return True, f"{name} unloaded"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def set_keep_alive(value: str) -> tuple[bool, str]:
    """Set how long a model stays resident after its last request.

    Applied by restarting Ollama with OLLAMA_KEEP_ALIVE — it is read at
    process start, so there is no way to change it on a live daemon. Accepts
    Ollama's own duration forms ("5m", "1h", "0" to unload immediately, "-1"
    to keep loaded indefinitely).
    """
    if not _KEEPALIVE_RE.match(value or ""):
        return False, "invalid duration (use e.g. 5m, 1h, 0, or -1)"
    app = registry.APPS.get("ollama")
    if app is None:
        return False, "ollama app is not in this node's library"
    # Persisted on the AppDef's env so it survives restarts and is applied by
    # every future launch, not just this one.
    app.env["OLLAMA_KEEP_ALIVE"] = value
    _save_keep_alive(value)
    return True, f"keep-alive set to {value}"


_KEEPALIVE_RE = re.compile(r"^-?\d+(\.\d+)?(ns|us|ms|s|m|h)?$")
_KEEPALIVE_FILE = os.path.join(config.NAS_ROOT, ".ollama-keepalive")


def _save_keep_alive(value: str) -> None:
    try:
        os.makedirs(os.path.dirname(_KEEPALIVE_FILE), exist_ok=True)
        with open(_KEEPALIVE_FILE, "w") as f:
            f.write(value)
    except OSError:
        pass


def get_keep_alive() -> str:
    """Current keep-alive setting; Ollama's own default is 5m."""
    try:
        with open(_KEEPALIVE_FILE) as f:
            v = f.read().strip()
            if _KEEPALIVE_RE.match(v):
                return v
    except OSError:
        pass
    return "5m"


_MODEL_CFG_FILE = os.path.join(config.NAS_ROOT, ".ollama-model-config.json")
# Ollama's own default when a model doesn't specify otherwise. Also what its
# OpenAI-compatible endpoint silently falls back to (see apply_model_config).
DEFAULT_NUM_CTX = 4096


def _load_model_cfg() -> dict:
    try:
        with open(_MODEL_CFG_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def model_config(name: str | None = None):
    """Per-model inference settings for THIS node. Per-node on purpose: the
    same model on a big GPU box and a small one wants different limits, and
    the whole point of storing it here is that granularity."""
    cfg = _load_model_cfg()
    if name is None:
        return cfg
    return cfg.get(name, {})


def set_model_config(name: str, opts: dict) -> tuple[bool, str]:
    """Store per-model options for this node AND bake them onto the model so
    they actually take effect (see apply_model_config for why that is the
    only approach that works through /v1). Only a known, validated subset is
    accepted — these reach Ollama, so anything unknown or out of range is
    rejected rather than passed through unchecked."""
    clean: dict = {}
    try:
        if opts.get("num_ctx") not in (None, ""):
            n = int(opts["num_ctx"])
            if not (256 <= n <= 1_048_576):
                return False, "context length must be between 256 and 1048576"
            clean["num_ctx"] = n
        if opts.get("temperature") not in (None, ""):
            t = float(opts["temperature"])
            if not (0.0 <= t <= 2.0):
                return False, "temperature must be between 0 and 2"
            clean["temperature"] = t
        if opts.get("top_p") not in (None, ""):
            p = float(opts["top_p"])
            if not (0.0 <= p <= 1.0):
                return False, "top_p must be between 0 and 1"
            clean["top_p"] = p
        if opts.get("num_gpu") not in (None, ""):
            g = int(opts["num_gpu"])
            if not (0 <= g <= 999):
                return False, "num_gpu must be 0 or more"
            clean["num_gpu"] = g
    except (TypeError, ValueError):
        return False, "invalid value"

    cfg = _load_model_cfg()
    if clean:
        cfg[name] = clean
    else:
        cfg.pop(name, None)   # empty = revert to Ollama's defaults
    try:
        os.makedirs(os.path.dirname(_MODEL_CFG_FILE), exist_ok=True)
        tmp = _MODEL_CFG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, _MODEL_CFG_FILE)
    except OSError as e:
        return False, f"could not save: {e}"
    if clean:
        ok, msg = apply_model_config(name, clean)
        if not ok:
            return False, f"saved, but could not apply: {msg}"
        return True, "saved and applied"
    return True, "reverted to defaults (restart the model to clear baked-in values)"


def apply_model_config(name: str, opts: dict) -> tuple[bool, str]:
    """Bake inference parameters onto the model itself, in place.

    This is the ONLY approach that actually works through the endpoint we
    serve. Ollama's OpenAI-compatible /v1/* layer discards a request's
    `options` block, including num_ctx — verified live: the identical request
    yields 8192 on /api/chat and 4096 on /v1/chat/completions. Rewriting
    requests to the native API would mean translating Ollama's streaming
    response back into OpenAI SSE, which is a lot of fragile surface for one
    setting.

    Instead, /api/create with `from` pointing at the model itself re-registers
    it under the same name with the parameters attached. Those become the
    model's own defaults, so they apply on EVERY request, including through
    /v1 — confirmed live (context_length 8192 on a plain /v1 call with no
    options). A per-request value still overrides them where the API honours
    one.
    """
    if not ollama_running():
        return False, "Ollama is not running"
    params = {k: v for k, v in opts.items()
              if k in ("num_ctx", "temperature", "top_p", "num_gpu")}
    try:
        r = httpx.post(f"{ollama_url()}/api/create",
                       json={"model": name, "from": name,
                             "parameters": params, "stream": False},
                       timeout=180.0)
        if r.status_code >= 400:
            return False, (r.text or "")[:200] or f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    # Evict so the next request loads with the new parameters rather than
    # continuing on the already-resident copy.
    unload_model(name)
    return True, "applied"


def _vram_mb() -> int:
    """Total GPU memory in MB, 0 when there's no usable GPU. Routing uses this
    to tell a machine that can hold a large context from one that can't.

    Resolved through metrics._nvidia_smi_path rather than by bare name, for the
    reason documented there: a systemd service inherits none of the shell
    startup files that put WSL's nvidia-smi shim on PATH, so `nvidia-smi` alone
    is simply not found under the running service.

    Confirmed live on a node with a working RTX 3070 Ti: the container saw the
    GPU and metrics.py reported it correctly, while this one function returned
    0. Nothing failed outright — the router treats capacity as an ordering hint,
    never a filter, so the node stayed eligible. It was just described wrongly,
    falling back to system RAM at a CPU discount (24 GB / 4 = 6 GB) so an 8 GB
    GPU machine ranked as a mid-sized CPU one, and the fact it had a GPU at all
    never reached the routing decision.
    """
    from .metrics import _nvidia_smi_path
    exe = _nvidia_smi_path()
    if not exe:
        return 0
    try:
        r = subprocess.run(
            [exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return max(int(float(v)) for v in r.stdout.split() if v.strip().isdigit())
    except Exception:  # noqa: BLE001
        pass
    return 0


def _ram_mb() -> int:
    """Total system RAM in MB — the fallback capacity signal for a CPU-only
    node, which serves models out of system memory."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


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
        # Per-model context limits this node is configured for, so the Hub's
        # router can match a request's size against what each node can
        # actually serve (see llm_router.pick_node).
        "model_config": model_config(),
        # Real capacity signals for routing: a big-context request belongs on
        # a machine that can hold it, not merely one that has the model.
        "vram_mb": _vram_mb(),
        "ram_mb": _ram_mb(),
    }


# ── Streaming proxy (async generator) ────────────────────────────────────────

async def stream_to_ollama(path: str, body: dict) -> AsyncGenerator[bytes, None]:
    """Async generator that streams bytes from Ollama — used by FastAPI
    StreamingResponse for /api/apps/ollama/v1/* proxy endpoints."""
    url = ollama_url()
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST", f"{url}{path}", json=body,
            # httpx's `read` timeout is a per-chunk IDLE gap, not a cap on total
            # duration — it resets on every byte received, so a real generation
            # that keeps producing tokens sails through untouched no matter how
            # long it runs (20-30+ min is fine). It only fires if the connection
            # goes fully silent for longer than this — that's the actual hang
            # this is meant to catch, tuned tighter than the old 600s so a
            # genuine stall doesn't sit unnoticed for ten minutes.
            timeout=httpx.Timeout(connect=10.0, read=STREAM_IDLE_TIMEOUT, write=30.0, pool=10.0),
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
# The most recently STARTED pull job's id — lets a client that doesn't (or no
# longer) knows the job_id (e.g. the model manager was closed and reopened, or
# opened on a different browser tab) find out "is something downloading right
# now" without having to have remembered it. Only one pull realistically runs
# at a time per node (the UI only ever starts one), so "the latest" is enough.
_latest_job_id: str | None = None


def _new_pull_job(model_name: str) -> tuple[str, dict]:
    global _latest_job_id
    job_id = _uuid_mod.uuid4().hex
    job: dict = {
        "job_id": job_id, "model": model_name, "action": "pull",
        "done": False, "ok": False, "error": None,
        "status": "starting", "total_bytes": None, "bytes_pulled": 0,
    }
    with _pull_jobs_lock:
        _pull_jobs[job_id] = job
        _latest_job_id = job_id
        done_ids = [k for k, v in _pull_jobs.items() if v["done"]]
        for k in done_ids[:-100]:
            del _pull_jobs[k]
    return job_id, job


def pull_job_status(job_id: str) -> dict | None:
    return _pull_jobs.get(job_id)


def latest_pull_job() -> dict | None:
    """The most recent pull job on this node (running or, briefly, just
    finished), or None if nothing's ever been pulled this SM process's
    lifetime — lets a client resume watching progress after reopening the
    model manager without needing to already have the job_id."""
    if _latest_job_id is None:
        return None
    return _pull_jobs.get(_latest_job_id)


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
    # httpx.delete() has no `json=` parameter (unlike post/put) — passing one
    # raised TypeError before the request was ever sent, so every delete 500'd
    # without touching Ollama at all (the model was never actually removed).
    # Ollama's DELETE /api/delete still expects a JSON body, so build the
    # request explicitly instead of using the delete() shortcut.
    r = httpx.request("DELETE", f"{ollama_url()}/api/delete",
                      json={"name": model_name}, timeout=30.0)
    if r.status_code not in (200, 204):
        raise RuntimeError(r.text[:200] or f"HTTP {r.status_code}")
    return {"ok": True, "deleted": model_name}


# ── LAN access toggle ─────────────────────────────────────────────────────────
# Whether this node's Ollama answers the LAN, or only the machine it runs on.
#
# OFF is the default, and is the setting that costs nothing: the Hub reaches
# every node's Ollama through the Server Manager on its own port, never on
# 11434 directly, so routing, the model list, pulls and inference all work
# fully with the port closed. What OFF removes is an UNAUTHENTICATED Ollama
# API answering anything on the network — anyone who can reach the box can
# list, run, and delete its models, with no credential of any kind.
#
# ON is for reaching this Ollama from something outside the fleet: a script, an
# editor plugin, another machine's tooling pointed straight at
# http://<node>:11434. Real uses, just not ones the dashboard needs.
#
# Node-local by design rather than a fleet-wide setting: it describes one
# machine's exposure on the network it happens to sit on, and a laptop that
# travels should not inherit a decision made for a server in the house.
_LAN_STATE_FILE = os.path.join(config.NAS_ROOT, ".ollama-lan-enabled")

# The fleet-reserved Ollama port (see the AppDef): fixed, unlike slot ports.
OLLAMA_LAN_PORT = 11434


def get_lan_access() -> bool:
    return os.path.exists(_LAN_STATE_FILE)


def set_lan_access(enabled: bool) -> dict:
    """Record whether Ollama should publish its port to the LAN.

    Only the desired state is written here. A container's published ports are
    fixed when it is created, so this cannot take effect on a running one —
    the caller is told whether a restart is outstanding rather than having the
    container pulled out from under it, which would drop whatever is mid-
    generation. `pending` is the honest answer to "is this live yet".
    """
    try:
        if enabled:
            os.makedirs(os.path.dirname(_LAN_STATE_FILE), exist_ok=True)
            open(_LAN_STATE_FILE, "w").close()
        else:
            try:
                os.unlink(_LAN_STATE_FILE)
            except FileNotFoundError:
                pass
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "lan_enabled": enabled, "pending": ollama_running()}


def lan_port_args() -> list[str]:
    """The -p flags Ollama's container is created with.

    Bound to loopback when LAN access is off. The port is still published, so
    everything on the node itself — the SM proxy, and Open WebUI reaching it by
    container name — is unaffected; it simply stops answering other machines.
    """
    if get_lan_access():
        return ["-p", f"{OLLAMA_LAN_PORT}:{OLLAMA_LAN_PORT}"]
    return ["-p", f"127.0.0.1:{OLLAMA_LAN_PORT}:{OLLAMA_LAN_PORT}"]


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
