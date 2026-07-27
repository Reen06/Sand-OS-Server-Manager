"""Install Sand-OS apps from Docker Hub — the receiving half of App
Definition Standard §11's user workflow.

The publishing half (app_variants.publish_to_dockerhub()) bakes a
`sandos.appdef` label into every published image: the portable subset of the
app's AppDef (port, mounts, env keys, GPU need). This module is what a
Server Manager install — on ANY mesh, including one that's never heard of
the publisher's — does with that: pull the image, read the manifest back
out, build a real AppDef from it, and register it into registry.APPS so the
entire existing machinery (spawn, proxy, Fleet/Apps pages, reconcile) treats
it exactly like a hand-written app.

Design decisions that matter:
  - The manifest is UNTRUSTED input from a public registry. _validate_manifest()
    whitelists every field with strict shape checks; anything unknown is
    dropped. Fields that could touch the host (binds, docker_args) or assume
    this Hub's auth (SSO headers) are never accepted — they aren't in the
    published manifest either (build_manifest() strips them), but this side
    revalidates independently rather than trusting the publisher.
  - Sidecar services are rejected in v1: their env VALUES (DB passwords…)
    are deliberately stripped at publish, so a service-having manifest can't
    be reconstructed into something runnable anyway. Explicit error beats a
    container stack that half-starts.
  - App ids are namespaced `hub-<id>` — collision-proof against this node's
    own built-in apps (the publisher's mesh may well have the same app as a
    native AppDef; ours does, which is exactly how this gets tested).
  - State lives in a node-local JSON file under ~/.sandos-sm/ — hub installs
    are a per-node fact (like variant choice), never fleet-global.
  - Update checking talks to Docker Hub's registry API directly (anonymous
    pull token + manifest HEAD) and compares digests — no pull, no side
    effects, same read-only spirit as the dev-source staleness check.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from .models import AppDef, Mount

STATE_FILE = os.environ.get(
    "SM_HUB_APPS_STATE", os.path.expanduser("~/.sandos-sm/hub-apps.json"))

SUPPORTED_SCHEMA = 1
ID_PREFIX = "hub-"

_lock = threading.Lock()
# normalized "repo:tag" -> job dict
_jobs: dict[str, dict] = {}
# app_id -> persisted record (mirrors the state file; in-memory copy)
_state: dict[str, dict] = {}

_REPO_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_NAME_RE = _ID_RE
_ENVKEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MEM_RE = re.compile(r"^\d+[bkmgBKMG]?$")


# ── repo reference parsing ───────────────────────────────────────────────────

def normalize_repo(text: str) -> tuple[str, str]:
    """Accepts 'user/name', 'user/name:tag', or a hub.docker.com URL →
    ('user/name', 'tag'). Raises ValueError on anything else — this is the
    one place raw user input enters, so it's strict by design."""
    text = (text or "").strip()
    if "hub.docker.com" in text:
        # https://hub.docker.com/r/reen16/webcad[/...] → reen16/webcad
        m = re.search(r"hub\.docker\.com/r/([^/\s]+/[^/\s:?#]+)", text)
        if not m:
            raise ValueError("couldn't extract a repository from that Docker Hub URL")
        text = m.group(1)
    tag = "latest"
    if ":" in text:
        text, tag = text.rsplit(":", 1)
    if not _REPO_RE.match(text):
        raise ValueError(f"invalid Docker Hub repository: {text!r} (expected user/name)")
    if not _TAG_RE.match(tag):
        raise ValueError(f"invalid tag: {tag!r}")
    return text, tag


# ── manifest validation ──────────────────────────────────────────────────────

def _validate_manifest(m: dict) -> dict:
    """Whitelist-validate an untrusted sandos.appdef manifest → cleaned dict.
    Raises ValueError with a message specific enough to actually fix the
    publishing side, never a bare 'invalid'."""
    if not isinstance(m, dict):
        raise ValueError("manifest is not a JSON object")
    schema = m.get("schema")
    if not isinstance(schema, int) or not (1 <= schema <= SUPPORTED_SCHEMA):
        raise ValueError(f"unsupported manifest schema {schema!r} (this SM supports ≤{SUPPORTED_SCHEMA})")

    def _s(key: str, maxlen: int, default: str = "", required: bool = False) -> str:
        v = m.get(key, default)
        if v is None and not required:
            v = default
        if not isinstance(v, str) or (required and not v):
            raise ValueError(f"manifest field {key!r} missing or not a string")
        return v[:maxlen]

    app_id = _s("id", 41, required=True)
    if not _ID_RE.match(app_id):
        raise ValueError(f"manifest id {app_id!r} invalid (lowercase alphanumeric + dashes)")

    kind = _s("kind", 16, "web")
    if kind not in ("web", "streamed"):
        raise ValueError(f"manifest kind {kind!r} not one of web/streamed")
    mode = _s("mode", 16, "shared")
    if mode not in ("per-user", "shared", "ephemeral"):
        raise ValueError(f"manifest mode {mode!r} invalid")
    port = m.get("internal_port")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"manifest internal_port {port!r} invalid")
    proxy_subpath = _s("proxy_subpath", 16, "forward")
    if proxy_subpath not in ("forward", "root"):
        raise ValueError(f"manifest proxy_subpath {proxy_subpath!r} invalid")
    mem = _s("mem_limit", 16)
    if mem and not _MEM_RE.match(mem):
        raise ValueError(f"manifest mem_limit {mem!r} invalid")
    keepalive = m.get("keepalive_seconds", 600)
    if not isinstance(keepalive, int) or not (0 <= keepalive <= 86400):
        keepalive = 600

    mounts = m.get("mounts", [])
    if not isinstance(mounts, list) or len(mounts) > 10:
        raise ValueError("manifest mounts invalid (list, max 10)")
    clean_mounts = []
    for mt in mounts:
        if not isinstance(mt, dict):
            raise ValueError("manifest mount entry is not an object")
        name = mt.get("name", "")
        path = mt.get("path", "")
        scope = mt.get("scope", "per-user")
        if not (isinstance(name, str) and _NAME_RE.match(name)):
            raise ValueError(f"mount name {name!r} invalid")
        if not (isinstance(path, str) and path.startswith("/") and ".." not in path):
            raise ValueError(f"mount path {path!r} invalid (absolute, no ..)")
        if scope not in ("per-user", "shared"):
            raise ValueError(f"mount scope {scope!r} invalid")
        clean_mounts.append({"name": name, "path": path, "scope": scope,
                             "ro": bool(mt.get("ro", False))})

    env_required = m.get("env_required", [])
    if not isinstance(env_required, list) or len(env_required) > 20:
        raise ValueError("manifest env_required invalid (list, max 20)")
    for k in env_required:
        if not (isinstance(k, str) and _ENVKEY_RE.match(k)):
            raise ValueError(f"env_required key {k!r} invalid")

    if m.get("services"):
        raise ValueError(
            "this app declares sidecar services (DB/cache) — not yet supported "
            "for Docker Hub installs; the sidecars' credentials aren't in the manifest")

    # Portable frontend facts (see build_manifest()): absolute-path bundles
    # that can't run under a shared subpath, and real-PWA apps whose own
    # manifest must be served untouched. apple icon is a same-origin path.
    apple_icon = _s("native_pwa_apple_icon", 200)
    if apple_icon and (not apple_icon.startswith("/") or ".." in apple_icon):
        apple_icon = ""

    # Streamed tuning: whitelist the encoder (it lands in the spawn args).
    encoder = _s("encoder", 16, "nvh264enc")
    if encoder not in ("nvh264enc", "x264enc"):
        encoder = "nvh264enc"

    # Trusted-header SSO NAMES (portable — this SM's proxy injects its own
    # authenticated user under them). Strict header-token shape; anything
    # else is dropped rather than reaching the proxy layer.
    hdr_re = re.compile(r"^[A-Za-z0-9-]{1,64}$")
    sso_header = _s("sso_header", 64)
    sso_role_header = _s("sso_role_header", 64)
    if sso_header and not hdr_re.match(sso_header):
        sso_header = ""
    if sso_role_header and not hdr_re.match(sso_role_header):
        sso_role_header = ""

    return {
        "schema": schema, "id": app_id,
        "label": _s("label", 60) or app_id,
        "icon": _s("icon", 30) or "cpu",       # unknown glyphs fall back frontend-side
        "color": _s("color", 20) or "blue",
        "desc": _s("desc", 200),
        "kind": kind, "mode": mode, "internal_port": port,
        "gpu": bool(m.get("gpu", False)),
        "mem_limit": mem, "proxy_subpath": proxy_subpath,
        "keepalive_seconds": keepalive,
        "own_subdomain": bool(m.get("own_subdomain", False)),
        "native_pwa": bool(m.get("native_pwa", False)),
        "native_pwa_apple_icon": apple_icon,
        "encoder": encoder, "resize": bool(m.get("resize", False)),
        "sso_header": sso_header, "sso_role_header": sso_role_header,
        "sso_role_value": _s("sso_role_value", 32) or "user",
        "mounts": clean_mounts, "env_required": list(env_required),
    }


def _manifest_to_appdef(man: dict, image_ref: str, env_values: dict[str, str]) -> AppDef:
    return AppDef(
        id=ID_PREFIX + man["id"],
        label=man["label"], icon=man["icon"], color=man["color"], desc=man["desc"],
        image=image_ref, kind=man["kind"], mode=man["mode"],
        internal_port=man["internal_port"], gpu=man["gpu"],
        mem_limit=man["mem_limit"], proxy_subpath=man["proxy_subpath"],
        keepalive_seconds=man["keepalive_seconds"],
        own_subdomain=man.get("own_subdomain", False),
        native_pwa=man.get("native_pwa", False),
        native_pwa_apple_icon=man.get("native_pwa_apple_icon") or None,
        encoder=man.get("encoder", "nvh264enc"),
        resize=man.get("resize", False),
        sso_header=man.get("sso_header") or None,
        sso_role_header=man.get("sso_role_header") or None,
        sso_role_value=man.get("sso_role_value", "user"),
        mounts=[Mount(name=mt["name"], path=mt["path"], scope=mt["scope"], ro=mt["ro"])
                for mt in man["mounts"]],
        env=dict(env_values),
    )


# ── persistence ──────────────────────────────────────────────────────────────

def _save_state() -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def load_installed() -> dict[str, AppDef]:
    """Rebuild AppDefs for every hub-installed app from the state file —
    called from registry.py at import time, BEFORE reconcile_from_docker(),
    so running hub-app containers get re-adopted across SM restarts exactly
    like built-in apps. Never raises: a corrupt state file logs and yields
    {} rather than bricking SM startup."""
    out: dict[str, AppDef] = {}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        for app_id, rec in data.items():
            try:
                man = _validate_manifest(rec["manifest"])      # defensive re-validate
                env = {k: str(v) for k, v in (rec.get("env") or {}).items()
                       if _ENVKEY_RE.match(k)}
                out[app_id] = _manifest_to_appdef(man, rec["image_ref"], env)
                _state[app_id] = rec
            except Exception as e:  # noqa: BLE001
                print(f"[dockerhub_apps] skipping bad state entry {app_id}: {e}")
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[dockerhub_apps] state file unreadable, starting empty: {e}")
    return out


def is_hub_app(app_id: str) -> bool:
    return app_id in _state


def hub_repo_of(app_id: str) -> str | None:
    rec = _state.get(app_id)
    return rec["repo"] if rec else None


# ── install / update jobs ────────────────────────────────────────────────────

def job_status(repo_input: str) -> dict | None:
    try:
        repo, tag = normalize_repo(repo_input)
    except ValueError:
        return None
    job = _jobs.get(f"{repo}:{tag}")
    if not job:
        return None
    return {"done": job["done"], "error": job["error"], "log_tail": job["log"][-30:],
            "app_id": job.get("app_id"), "env_required": job.get("env_required", []),
            "generic": job.get("generic", False)}


def install(repo_input: str, env: dict[str, str] | None = None,
            internal_port: int | None = None) -> dict:
    """Kick off a background pull+register of an app image from Docker Hub.
    Returns immediately; poll job_status(). `env` supplies values for the
    manifest's declared env_required keys — unknown keys are rejected loudly
    rather than silently dropped (generic installs, which declare nothing,
    accept any keys). `internal_port` overrides port detection for a generic
    image exposing several ports."""
    repo, tag = normalize_repo(repo_input)
    key = f"{repo}:{tag}"
    with _lock:
        existing = _jobs.get(key)
        if existing and not existing["done"]:
            raise ValueError(f"{key} install already in progress")
        job = {"log": [], "started_at": time.time(), "done": False, "error": None,
               "app_id": None, "env_required": [], "generic": False}
        _jobs[key] = job
    threading.Thread(target=_run_install, args=(repo, tag, dict(env or {}), internal_port, job),
                     daemon=True, name=f"hub-install-{repo.replace('/', '-')}").start()
    return {"ok": True, "status": "installing", "repo": repo, "tag": tag}


def _synthesize_manifest(repo: str, image_ref: str, internal_port: int | None,
                         log: list[str]) -> dict:
    """A best-effort manifest for a NON-Sand-OS image, from what the image
    itself declares: EXPOSE → internal_port, VOLUME → shared named volumes
    (so its data survives container recreation). This is the deliberate v1
    of 'install any reasonable web-app image': no SSO, no GPU, shared mode,
    default proxying — the admin can't get a broken bind mount or a hostile
    mount path out of it because everything still passes _validate_manifest.
    Raises ValueError when the image declares no usable port and none was
    supplied — an unproxiable app, better refused than half-installed."""
    r = subprocess.run(["docker", "image", "inspect", image_ref,
                        "--format", "{{json .Config.ExposedPorts}} {{json .Config.Volumes}}"],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise ValueError("docker image inspect failed after pull")
    exposed_raw, _, volumes_raw = r.stdout.strip().partition(" ")
    exposed = json.loads(exposed_raw or "null") or {}
    volumes = json.loads(volumes_raw or "null") or {}

    ports = sorted(int(p.split("/")[0]) for p in exposed if p.endswith("/tcp"))
    if internal_port:
        port = internal_port
        log.append(f"using supplied port {port}")
    elif len(ports) == 1:
        port = ports[0]
        log.append(f"image exposes port {port}")
    elif not ports:
        raise ValueError(
            "this image declares no EXPOSEd TCP port and none was supplied — "
            "can't proxy it; provide the app's HTTP port explicitly")
    else:
        raise ValueError(
            f"this image exposes several ports ({', '.join(map(str, ports))}) — "
            "provide the app's HTTP port explicitly")

    name = repo.rsplit("/", 1)[-1]
    app_id = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40] or "app"
    mounts = []
    for path in sorted(volumes):
        mname = re.sub(r"[^a-z0-9-]", "-", path.lower()).strip("-")[:40]
        if mname:
            mounts.append({"name": mname, "path": path, "scope": "shared", "ro": False})
            log.append(f"volume {path} → persistent shared volume")

    return _validate_manifest({
        "schema": SUPPORTED_SCHEMA, "id": app_id,
        "label": name[:60], "icon": "cpu", "color": "blue",
        "desc": f"Generic Docker Hub install of {repo} (no Sand-OS manifest).",
        "kind": "web", "mode": "shared", "internal_port": port,
        "gpu": False, "mem_limit": "", "proxy_subpath": "forward",
        "keepalive_seconds": 600, "mounts": mounts, "env_required": [],
    })


def _run_install(repo: str, tag: str, env: dict[str, str],
                 internal_port: int | None, job: dict) -> None:
    from . import registry
    image_ref = f"{repo}:{tag}"
    try:
        job["log"].append(f"$ docker pull {image_ref}")
        proc = subprocess.Popen(["docker", "pull", image_ref], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            job["log"].append(line.rstrip())
            job["log"][:] = job["log"][-500:]
        proc.wait(timeout=3600)
        if proc.returncode != 0:
            job["error"] = f"docker pull failed (exit {proc.returncode})"
            return

        r = subprocess.run(
            ["docker", "image", "inspect", image_ref,
             "--format", '{{index .Config.Labels "sandos.appdef"}}'],
            capture_output=True, text=True, timeout=15)
        raw = r.stdout.strip()
        generic = r.returncode == 0 and (not raw or raw == "<no value>")
        if r.returncode != 0:
            job["error"] = "docker image inspect failed after pull"
            return

        if generic:
            # No Sand-OS manifest — fall back to what the image itself
            # declares (EXPOSE/VOLUME). Full-fidelity config (SSO, GPU,
            # per-user mode, subdomain frontends…) still needs a published
            # manifest; this path is "reasonable web app, sane defaults".
            job["generic"] = True
            job["log"].append("no sandos.appdef manifest — generic install from image metadata")
            try:
                man = _synthesize_manifest(repo, image_ref, internal_port, job["log"])
            except ValueError as e:
                job["error"] = str(e)
                return
        else:
            try:
                man = _validate_manifest(json.loads(raw))
            except (ValueError, json.JSONDecodeError) as e:
                job["error"] = f"invalid sandos.appdef manifest: {e}"
                return
            job["env_required"] = man["env_required"]
            unknown = set(env) - set(man["env_required"])
            if unknown:
                job["error"] = f"env keys not declared by this app: {', '.join(sorted(unknown))}"
                return

        # Generic installs accept any (valid-shaped) env keys — there's no
        # declared contract to check against, and images like linuxserver.io's
        # genuinely need PUID/TZ-style settings.
        bad_keys = [k for k in env if not _ENVKEY_RE.match(k)]
        if bad_keys:
            job["error"] = f"invalid env key(s): {', '.join(sorted(bad_keys))}"
            return

        app_id = ID_PREFIX + man["id"]
        if app_id in registry.APPS and not is_hub_app(app_id):
            job["error"] = f"app id {app_id!r} already exists on this node"
            return

        digest = _local_digest(repo, image_ref)
        app = _manifest_to_appdef(man, image_ref, env)
        with _lock:
            registry.APPS[app_id] = app
            _state[app_id] = {"repo": repo, "tag": tag, "image_ref": image_ref,
                              "manifest": man, "env": env, "generic": generic,
                              "digest": digest, "installed_at": time.time()}
            _save_state()
        job["app_id"] = app_id
        job["log"].append(f"registered as {app_id} ({man['label']})"
                          + (" — generic install" if generic else ""))
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
    finally:
        job["done"] = True


def uninstall(app_id: str) -> dict:
    """Deregister + remove a hub-installed app and its image. Refuses while
    any instance is running. Data volumes are deliberately left intact —
    reinstalling the app finds its data again; deleting data is a separate,
    explicit decision, never a side effect."""
    from . import registry
    rec = _state.get(app_id)
    if not rec:
        raise ValueError(f"{app_id} is not a Docker Hub-installed app")
    running = [i for i in registry.instances_summary()
               if i["app_id"] == app_id and i["running"]]
    if running:
        raise ValueError("stop the app before uninstalling it")
    with _lock:
        registry.APPS.pop(app_id, None)
        _state.pop(app_id, None)
        _save_state()
    r = subprocess.run(["docker", "rmi", rec["image_ref"]],
                       capture_output=True, text=True, timeout=60)
    return {"removed": True,
            "image_removed": r.returncode == 0,
            "note": None if r.returncode == 0 else (r.stderr.strip() or r.stdout.strip())}


# ── update checking (Docker Hub registry API, read-only) ─────────────────────

def _local_digest(repo: str, image_ref: str) -> str | None:
    r = subprocess.run(["docker", "image", "inspect", image_ref,
                        "--format", "{{join .RepoDigests \",\"}}"],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return None
    for d in r.stdout.strip().split(","):
        if d.startswith(repo + "@"):
            return d.split("@", 1)[1]
    return None


def _remote_digest(repo: str, tag: str) -> str:
    """Digest of repo:tag on Docker Hub right now, via an anonymous pull
    token + manifest HEAD — no image bytes move. Raises on any failure (the
    caller reports it as 'couldn't check', never as 'up to date')."""
    tok_url = ("https://auth.docker.io/token?service=registry.docker.io"
               f"&scope=repository:{urllib.parse.quote(repo)}:pull")
    with urllib.request.urlopen(tok_url, timeout=15) as resp:
        token = json.loads(resp.read())["token"]
    req = urllib.request.Request(
        f"https://registry-1.docker.io/v2/{repo}/manifests/{urllib.parse.quote(tag)}",
        method="HEAD",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": ", ".join([
                     "application/vnd.docker.distribution.manifest.list.v2+json",
                     "application/vnd.oci.image.index.v1+json",
                     "application/vnd.docker.distribution.manifest.v2+json",
                     "application/vnd.oci.image.manifest.v1+json"])})
    with urllib.request.urlopen(req, timeout=15) as resp:
        digest = resp.headers.get("Docker-Content-Digest")
    if not digest:
        raise RuntimeError("registry returned no Docker-Content-Digest header")
    return digest


def check_update(app_id: str) -> dict:
    rec = _state.get(app_id)
    if not rec:
        raise ValueError(f"{app_id} is not a Docker Hub-installed app")
    local = _local_digest(rec["repo"], rec["image_ref"]) or rec.get("digest")
    try:
        remote = _remote_digest(rec["repo"], rec["tag"])
    except Exception as e:  # noqa: BLE001
        return {"repo": rec["repo"], "tag": rec["tag"], "local_digest": local,
                "remote_digest": None, "update_available": None,
                "error": f"couldn't reach Docker Hub: {e}"}
    return {"repo": rec["repo"], "tag": rec["tag"], "local_digest": local,
            "remote_digest": remote,
            "update_available": bool(local and remote and local != remote),
            "error": None}


def update(app_id: str) -> dict:
    """Re-pull + re-register from the same repo:tag — a fresh install run
    that keeps the admin's env values. Same job machinery/status endpoint
    as install. A generic install keeps its established port across
    updates (re-detection could flip to 'several ports, be explicit' if
    the image's EXPOSE list grew)."""
    rec = _state.get(app_id)
    if not rec:
        raise ValueError(f"{app_id} is not a Docker Hub-installed app")
    port = rec["manifest"]["internal_port"] if rec.get("generic") else None
    return install(f"{rec['repo']}:{rec['tag']}", rec.get("env") or {}, internal_port=port)
