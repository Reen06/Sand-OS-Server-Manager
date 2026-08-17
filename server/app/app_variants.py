"""App version manager — install/uninstall/switch between an app's declared
`AppVariant`s (see models.py), and free disk space by removing ones you're
not using. Generic: any AppDef that declares `variants` gets this for free;
apps with none (the vast majority) are untouched.

Design:
  - "installed"  = the variant's image_tag exists locally (`docker image
    inspect`). Never implied by the catalog — the catalog just lists what
    COULD be installed.
  - "active"     = the variant docker_backend.spawn() will actually launch.
    Persisted in a small local JSON state file (per SM node — versions are a
    node-local disk/software choice, not a fleet-wide or per-user setting).
  - install()    = docker build (from build_context) or docker pull, run in a
    background thread so the endpoint returns immediately; progress is
    polled via status(). A resolver (e.g. "freecad-weekly") computes
    build_args dynamically right before building, for channels that roll
    forward (no fixed URL to hardcode).
  - uninstall()  = docker rmi the tag — refused if it's the active variant
    (switch first) or if a running container is using that image (stop it
    first). Per-user app SETTINGS (NFS .appdata) are untouched; this only
    affects which image bytes sit on this node's disk.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request

from . import config
from .models import AppDef, AppVariant

_STATE_FILE = os.path.join(config.NAS_ROOT, ".app-variants-state.json")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_lock = threading.Lock()
# app_id -> {"variant_id": str, "log": [str], "started_at": float, "done": bool, "error": str|None}
_jobs: dict[str, dict] = {}


def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _variant(app: AppDef, variant_id: str) -> AppVariant:
    for v in app.variants:
        if v.id == variant_id:
            return v
    raise KeyError(f"no such variant {variant_id!r} for {app.id}")


def _host_args(host: str | None) -> list[str]:
    return ["-H", host] if host else []


def _docker_image_exists(tag: str, host: str | None = None) -> bool:
    r = subprocess.run(["docker", *_host_args(host), "image", "inspect", tag],
                       capture_output=True, timeout=10)
    return r.returncode == 0


def _docker_image_size(tag: str, host: str | None = None) -> int | None:
    r = subprocess.run(["docker", *_host_args(host), "image", "inspect", tag, "--format", "{{.Size}}"],
                       capture_output=True, text=True, timeout=10)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _image_in_use(tag: str, host: str | None = None) -> bool:
    """True if any container (running or stopped) still references this image."""
    r = subprocess.run(["docker", *_host_args(host), "ps", "-a", "--filter", f"ancestor={tag}", "-q"],
                       capture_output=True, text=True, timeout=10)
    return bool(r.stdout.strip())


def _host_for(app: AppDef) -> str | None:
    from . import app_images
    return app_images.active_docker_host(app.id)


# ── dynamic resolvers: fill in build_args right before an install ──────────────

def _resolve_freecad_weekly(variant: AppVariant) -> dict[str, str]:
    """Latest FreeCAD weekly dev build's Linux x86_64 AppImage asset URL."""
    req = urllib.request.Request(
        "https://api.github.com/repos/FreeCAD/FreeCAD/releases",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "SandOS-SM"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        releases = json.loads(resp.read())
    weekly = next((r for r in releases if r.get("tag_name", "").startswith("weekly-")), None)
    if weekly is None:
        raise RuntimeError("no weekly release found on FreeCAD/FreeCAD")
    asset = next(
        (a for a in weekly["assets"]
         if re.search(r"Linux-x86_64\.AppImage$", a["name"])),
        None)
    if asset is None:
        raise RuntimeError(f"no Linux x86_64 AppImage asset on {weekly['tag_name']}")
    return {"FREECAD_APPIMAGE_URL": asset["browser_download_url"],
            "_resolved_tag": weekly["tag_name"]}


_RESOLVERS = {"freecad-weekly": _resolve_freecad_weekly}


# ── catalog / status ─────────────────────────────────────────────────────────

def list_variants(app: AppDef, show_dev: bool = False) -> dict:
    if not app.variants:
        return {"supported": False, "variants": []}
    state = _load_state()
    active_id = state.get(app.id, {}).get("variant_id") or _default_active(app)
    job = _jobs.get(app.id)
    host = _host_for(app)

    out = []
    for v in app.variants:
        if v.channel == "dev" and not show_dev:
            continue
        installed = _docker_image_exists(v.image_tag, host)
        out.append({
            "id": v.id, "label": v.label, "channel": v.channel,
            "installed": installed,
            "active": v.id == active_id,
            "size": _docker_image_size(v.image_tag, host) if installed else None,
        })
    return {
        "supported": True,
        "variants": out,
        "installing": (
            {"variant_id": job["variant_id"], "done": job["done"],
             "error": job["error"], "log_tail": job["log"][-15:]}
            if job else None
        ),
    }


def _default_active(app: AppDef) -> str:
    stable = next((v for v in app.variants if v.channel == "stable"), app.variants[0])
    return stable.id


def active_image(app: AppDef) -> str:
    """What docker_backend.spawn() should actually run. Falls back to
    `app.image` when variants are unused/undeclared, or the selected variant
    somehow isn't installed (never launch a tag that isn't there). Checks the
    app's ACTUAL daemon (local, or a USB drive if the image was relocated) —
    checking the wrong one would wrongly report "not installed"."""
    if not app.variants:
        # A dev-source app on a node that doesn't have the live checkout
        # would otherwise still try to launch `image` — which docker_backend
        # now refuses to bind-mount there, so it'd run against WHATEVER the
        # image itself contains (nothing, for the plain dev image). Prefer
        # the self-contained packaged build instead, if one exists on this
        # node. On the dev machine itself (source_tree_ready == True) this
        # never triggers — `image` off the live bind mount stays the default,
        # completely unaffected by whether a packaged tag also happens to
        # exist on disk there.
        from . import registry
        if app.packaged_image and app.binds and not registry.source_tree_ready(app):
            host = _host_for(app)
            if _docker_image_exists(app.packaged_image, host):
                return app.packaged_image
        return app.image
    state = _load_state()
    variant_id = state.get(app.id, {}).get("variant_id") or _default_active(app)
    try:
        v = _variant(app, variant_id)
    except KeyError:
        return app.image
    return v.image_tag if _docker_image_exists(v.image_tag, _host_for(app)) else app.image


def image_source_commit(app: AppDef, host: str | None = None) -> str | None:
    """The dev-source git commit baked into THIS node's currently-active
    image for `app`, via the `sandos.source_commit` label _run_install() sets
    at build time (only for a binds-having app whose source is a git repo —
    see App Definition Standard §8/§10). None if the image has no such
    label — a pulled/manually-built image, or one built before this existed.
    Paired with registry.dev_source_commit() (the CURRENT source commit, only
    known on whichever node actually has the live checkout) to answer "is
    this packaged copy behind the dev source" — a check, never an automatic
    rebuild; see the fleet_source_check Hub endpoint."""
    tag = active_image(app)
    r = subprocess.run(
        ["docker", *_host_args(host), "image", "inspect", tag,
         "--format", '{{index .Config.Labels "sandos.source_commit"}}'],
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return None
    val = r.stdout.strip()
    return val if val and val != "<no value>" else None


# ── actions ──────────────────────────────────────────────────────────────────

def select(app: AppDef, variant_id: str) -> dict:
    v = _variant(app, variant_id)
    if not _docker_image_exists(v.image_tag, _host_for(app)):
        raise ValueError(f"{v.label} isn't installed yet — install it first")
    state = _load_state()
    state[app.id] = {"variant_id": variant_id}
    _save_state(state)
    return {"active": variant_id}


def uninstall(app: AppDef, variant_id: str) -> dict:
    v = _variant(app, variant_id)
    host = _host_for(app)
    state = _load_state()
    if state.get(app.id, {}).get("variant_id", _default_active(app)) == variant_id:
        raise ValueError("can't uninstall the active version — switch to another first")
    if not _docker_image_exists(v.image_tag, host):
        return {"removed": False, "reason": "not installed"}
    if _image_in_use(v.image_tag, host):
        raise ValueError("an instance is using this version — stop it first")
    r = subprocess.run(["docker", *_host_args(host), "rmi", v.image_tag],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return {"removed": True}


def install_status(app_id: str) -> dict | None:
    job = _jobs.get(app_id)
    if not job:
        return None
    return {"variant_id": job["variant_id"], "done": job["done"],
            "error": job["error"], "log_tail": job["log"][-30:]}


def install(app: AppDef, variant_id: str) -> dict:
    """Kick off install in a background thread; returns immediately."""
    v = _variant(app, variant_id)
    with _lock:
        existing = _jobs.get(app.id)
        if existing and not existing["done"]:
            raise ValueError(f"{app.id} already has an install in progress")
        job = {"variant_id": variant_id, "log": [], "started_at": time.time(),
               "done": False, "error": None}
        _jobs[app.id] = job

    threading.Thread(target=_run_install, args=(app, v, job), daemon=True,
                     name=f"install-{app.id}-{variant_id}").start()
    return {"ok": True, "status": "installing"}


def _run_install(app: AppDef, v: AppVariant, job: dict) -> None:
    # Note: builds/pulls always target the app's CURRENT daemon (local, or a
    # USB drive if the image already lives there) — installing a new variant
    # of an already-relocated app keeps it on that same drive.
    host = _host_for(app)
    try:
        build_args = dict(v.build_args)
        if v.resolver:
            resolved = _RESOLVERS[v.resolver](v)
            job["log"].append(f"resolved: {resolved.get('_resolved_tag', '')}".strip())
            build_args.update({k: val for k, val in resolved.items() if not k.startswith("_")})

        env = None
        if v.kind == "pull":
            cmd = ["docker", *_host_args(host), "pull", v.source or v.image_tag]
        else:
            context = os.path.join(_REPO_ROOT, app.build_context)
            cmd = ["docker", *_host_args(host), "build", "-t", v.image_tag]
            for k, val in build_args.items():
                cmd += ["--build-arg", f"{k}={val}"]
            if app.binds:
                # Bake in the dev source's CURRENT commit at the moment of
                # this build — a deliberate snapshot, not a live link. This
                # is what image_source_commit() later compares against the
                # dev node's then-current commit to flag "this packaged copy
                # is behind" (registry.dev_source_commit(), fleet_source_check
                # on the Hub) — never anything that rebuilds automatically.
                from . import registry
                commit = registry.dev_source_commit(app)
                if commit:
                    cmd += ["--label", f"sandos.source_commit={commit}"]
                    job["log"].append(f"building from source commit {commit[:12]}")
            cmd.append(context)
            if host:
                # `docker build` is aliased to `docker buildx build`, and
                # buildx's default builder targets its OWN docker CONTEXT —
                # it silently ignores -H, so a "build directly on the USB
                # drive" ends up on local disk instead (confirmed live: an
                # OpenFOAM GUI image built with -H <usb-socket> landed on the
                # default daemon regardless). DOCKER_BUILDKIT=0 forces the
                # legacy builder, which does respect -H correctly. Only
                # needed for `build`, not `pull` — plain pulls always target
                # -H correctly regardless of buildx.
                env = {**os.environ, "DOCKER_BUILDKIT": "0"}

        job["log"].append("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env)
        for line in proc.stdout:
            job["log"].append(line.rstrip())
            job["log"][:] = job["log"][-500:]  # bounded
        proc.wait(timeout=1800)

        if v.kind == "pull" and v.source and v.source != v.image_tag:
            subprocess.run(["docker", *_host_args(host), "tag", v.source, v.image_tag], timeout=15)

        if proc.returncode != 0:
            job["error"] = f"exit code {proc.returncode}"
        elif not _docker_image_exists(v.image_tag, host):
            job["error"] = "build finished but the image tag wasn't produced"
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
    finally:
        job["done"] = True


# ── packaged builds (webcad/helix-style dev-source apps) ───────────────────
# Separate from the variants system above on purpose — these two apps
# deliberately have no `variants` list (see app_definition standard §8-10 /
# docker_backend.spawn()'s bind-mount guard): giving them one risked the dev
# machine's own default launch silently switching to a self-contained build
# instead of the live bind-mounted source the moment one got built there too
# (variants' _default_active() would pick it up with zero explicit
# selection). This is a parallel, much narrower job type: only ever runs on
# whichever node source_tree_ready() is true — the "Rebuild & deploy" flow
# the Hub's fleet_rebuild_and_deploy orchestrates.
_packaged_jobs: dict[str, dict] = {}

APPDEF_MANIFEST_SCHEMA = 1


def build_manifest(app: AppDef) -> dict:
    """The portable subset of this AppDef, baked into a published image as
    the `sandos.appdef` label (see Docker Hub publish, below) — enough for a
    DIFFERENT Server Manager install, on a mesh that's never heard of this
    one, to auto-wire the app (port, volumes, env keys, GPU need) instead of
    an admin hand-writing an AppDef in Python from scratch. Deliberately
    excludes anything mesh-specific (binds, packaged_build_context,
    dockerhub_repo itself, SSO/proxy wiring that assumes THIS Hub's auth) —
    those aren't portable facts about the app, they're facts about this
    particular deployment of it. env VALUES are never included — only which
    keys the app expects — since values are often secrets or instance-
    specific (a receiving admin fills these in themselves)."""
    return {
        "schema": APPDEF_MANIFEST_SCHEMA,
        "id": app.id,
        "label": app.label,
        "icon": app.icon,
        "color": app.color,
        "desc": app.desc,
        "kind": app.kind,
        "mode": app.mode,
        "internal_port": app.internal_port,
        "gpu": app.gpu,
        "multi_node": app.multi_node,
        "mem_limit": app.mem_limit,
        "proxy_subpath": app.proxy_subpath,
        "keepalive_seconds": app.keepalive_seconds,
        # Portable frontend facts (NOT mesh-specific): whether the app's
        # bundle hardcodes absolute paths and so can't live under a shared
        # subpath (openfoamgui/engineeringpaper — see own_subdomain's
        # docstring in models.py), and whether it ships its own real PWA
        # manifest that must not be overridden. A receiving SM's proxy keys
        # its base-href/PWA-injection behavior off these.
        "own_subdomain": app.own_subdomain,
        "native_pwa": app.native_pwa,
        "native_pwa_apple_icon": app.native_pwa_apple_icon or "",
        # Streamed-only tuning — defaults for web apps, real for FreeCAD-class
        # streamed desktops; a receiving node needs them to spawn correctly.
        "encoder": app.encoder,
        "resize": app.resize,
        # Trusted-header SSO names are PORTABLE app facts, unlike docker_args
        # (mesh plumbing, deliberately excluded): the receiving SM's proxy
        # injects ITS OWN authenticated username under these exact headers,
        # so an app tailored for header SSO (Open WebUI) logs users straight
        # in on any mesh. Header NAMES only — never secrets.
        "sso_header": app.sso_header or "",
        "sso_role_header": app.sso_role_header or "",
        "sso_role_value": app.sso_role_value,
        # scope="root" (mount the ENTIRE fleet NAS export — Nextcloud/Open
        # WebUI scope per-user themselves) is inherently mesh-specific: a
        # receiving install has no fleet NAS. Translate to a plain shared
        # volume so the app still gets persistent storage at that path;
        # `storage` (nfs/usb) is likewise stripped — a receiver's own
        # storage layout is its own business.
        "mounts": [{"name": m.name, "path": m.path,
                    "scope": "shared" if m.scope == "root" else m.scope,
                    "ro": m.ro}
                   for m in app.mounts],
        "env_required": sorted(app.env.keys()),
        "services": [{"name": s.name, "image": s.image, "env": sorted(s.env.keys()),
                      "mounts": [{"name": m.name, "path": m.path} for m in s.mounts]}
                     for s in app.services],
    }


def _context_commit(ctx: str) -> str | None:
    """Git commit of whatever tree a build context sits in — the app's own
    source clone (EngineeringPaper.xyz, OpenMapper) or this SM repo itself
    (containers/* contexts). None for a non-git context. Gives every
    published image a truthful sandos.source_commit even for apps with no
    dev-source binds."""
    try:
        r = subprocess.run(["git", "-C", ctx, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _publish_build_spec(app: AppDef) -> tuple[str, str | None, str, dict[str, str]]:
    """(context_abs, dockerfile_abs|None, image_tag, build_args) for building
    this app's publishable image. Three shapes:
      - binds app (webcad/helix/openfoamgui): the SEPARATE packaged build —
        Dockerfile.packaged, packaged_image tag — since its normal image
        expects a bind-mounted source tree.
      - variants app (freecad-streamer): the ACTIVE variant's build
        definition — its image_tag and, crucially, its build_args (a bare
        context build without FREECAD_APPIMAGE_URL would fail). Resolver
        variants get resolved by the caller, same as _run_install.
      - plain custom build (engineeringpaper, renode…): the app's normal
        build definition — its image already IS self-contained, so the
        publish rebuild just adds the labels.
    Raises ValueError for apps with nothing custom to build (pure upstream
    images like stirlingpdf/ollama — publishing someone else's unmodified
    image would be pointless AND label-less)."""
    if app.binds:
        if not (app.packaged_image and app.packaged_build_context and app.packaged_dockerfile):
            raise ValueError(f"{app.id} has no packaged build configured")
        ctx = app.packaged_build_context
        return ctx, os.path.join(ctx, app.packaged_dockerfile), app.packaged_image, {}
    if app.build_context:
        ctx = app.build_context if os.path.isabs(app.build_context) \
            else os.path.join(_REPO_ROOT, app.build_context)
        df = None
        if app.build_dockerfile:
            df = app.build_dockerfile if os.path.isabs(app.build_dockerfile) \
                else os.path.join(_REPO_ROOT, app.build_dockerfile)
        tag = app.image
        build_args: dict[str, str] = {}
        if app.variants:
            state = _load_state()
            variant_id = state.get(app.id, {}).get("variant_id") or _default_active(app)
            try:
                v = _variant(app, variant_id)
                tag = v.image_tag
                build_args = dict(v.build_args)
                if v.resolver:
                    resolved = _RESOLVERS[v.resolver](v)
                    build_args.update({k: val for k, val in resolved.items()
                                       if not k.startswith("_")})
            except KeyError:
                pass
        return ctx, df, tag, build_args
    raise ValueError(f"{app.id} runs a pure upstream image — nothing custom to publish")


def build_packaged(app: AppDef) -> dict:
    """Kick off a background rebuild of app.packaged_image FROM THIS NODE's
    live checkout, baking in its current commit as the sandos.source_commit
    label (compared later by image_source_commit()/registry.dev_source_commit()
    — see the Hub's fleet_source_check). Raises ValueError (never silently
    no-ops) if this app has no packaged build configured, or this node isn't
    the one with the checkout to build from."""
    from . import registry
    if not (app.packaged_image and app.packaged_build_context and app.packaged_dockerfile):
        raise ValueError(f"{app.id} has no packaged build configured")
    if not registry.source_tree_ready(app):
        raise ValueError("this node doesn't have the dev source checkout to build from")
    with _lock:
        existing = _packaged_jobs.get(app.id)
        if existing and not existing["done"]:
            raise ValueError(f"{app.id} packaged build already in progress")
        job = {"log": [], "started_at": time.time(), "done": False, "error": None}
        _packaged_jobs[app.id] = job
    threading.Thread(target=_run_packaged_build, args=(app, job), daemon=True,
                     name=f"packaged-build-{app.id}").start()
    return {"ok": True, "status": "building"}


def _run_packaged_build(app: AppDef, job: dict) -> None:
    from . import registry
    try:
        # Build against the app's ACTIVE daemon, same rule _run_install
        # follows for variants: openfoamgui's multi-GB base lives on the USB
        # drive's dockerd precisely so it never touches the node's own disk —
        # building the packaged variant locally would drag it all back.
        # webcad/helix resolve to None (local daemon), unchanged.
        host = _host_for(app)
        ctx, dockerfile, tag, build_args = _publish_build_spec(app)
        # Truthful provenance either way: the live dev checkout's commit for
        # a binds app, else the build context's own git tree (the app's
        # source clone, or this SM repo for containers/* contexts).
        commit = registry.dev_source_commit(app) or _context_commit(ctx)
        cmd = ["docker", *_host_args(host), "build"]
        if dockerfile:
            cmd += ["-f", dockerfile]
        cmd += ["-t", tag]
        for k, val in build_args.items():
            cmd += ["--build-arg", f"{k}={val}"]
        if commit:
            cmd += ["--label", f"sandos.source_commit={commit}"]
            job["log"].append(f"building from source commit {commit[:12]}")
        cmd += ["--label", f"sandos.appdef={json.dumps(build_manifest(app), separators=(',', ':'))}"]
        cmd.append(ctx)

        env = None
        if host:
            # Same buildx-ignores--H trap _run_install documents: force the
            # legacy builder so the build actually lands on the -H daemon.
            env = {**os.environ, "DOCKER_BUILDKIT": "0"}

        job["log"].append("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=env)
        for line in proc.stdout:
            job["log"].append(line.rstrip())
            job["log"][:] = job["log"][-500:]
        proc.wait(timeout=1800)

        if proc.returncode != 0:
            job["error"] = f"exit code {proc.returncode}"
        elif not _docker_image_exists(tag, host):
            job["error"] = "build finished but the image tag wasn't produced"
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
    finally:
        job["done"] = True


def packaged_build_status(app_id: str) -> dict | None:
    job = _packaged_jobs.get(app_id)
    if not job:
        return None
    return {"done": job["done"], "error": job["error"], "log_tail": job["log"][-30:]}


# ── publish to Docker Hub ────────────────────────────────────────────────────
# App Definition Standard §11's developer-side half: sharing an app OUTSIDE
# this mesh entirely, for a Server Manager install on a mesh that's never
# heard of this one to consume. Deliberately separate from the internal
# Rebuild & deploy flow (sm_fleet.py) — that transfers image bytes over a
# Hub-brokered SSH relay between two nodes that already trust each other;
# this pushes to a PUBLIC registry, a fundamentally different trust
# boundary, and always rebuilds fresh first so what's published is never
# stale relative to what "Rebuild & deploy" would have shipped internally.
_publish_jobs: dict[str, dict] = {}


def publish_to_dockerhub(app: AppDef) -> dict:
    """Rebuild app.packaged_image fresh (so the published copy is never
    behind the dev source — see build_packaged()) then `docker push` it to
    app.dockerhub_repo as both `:latest` and `:<short-commit>`. Requires
    `docker login` to already be set up for this daemon's user — this never
    handles or stores a registry credential itself, same boundary as the
    plain `docker` CLI. Raises ValueError if this app has no dockerhub_repo
    configured, or (via build_packaged) this node isn't the dev machine."""
    if not app.dockerhub_repo:
        raise ValueError(f"{app.id} has no dockerhub_repo configured")
    with _lock:
        existing = _publish_jobs.get(app.id)
        if existing and not existing["done"]:
            raise ValueError(f"{app.id} publish already in progress")
        job = {"log": [], "started_at": time.time(), "done": False, "error": None,
               "repo": app.dockerhub_repo, "tags": []}
        _publish_jobs[app.id] = job
    threading.Thread(target=_run_publish, args=(app, job), daemon=True,
                     name=f"publish-{app.id}").start()
    return {"ok": True, "status": "publishing"}


def _run_publish(app: AppDef, job: dict) -> None:
    from . import registry
    try:
        job["log"].append("rebuilding packaged image fresh before publishing…")
        build_job = {"log": [], "started_at": time.time(), "done": False, "error": None}
        _run_packaged_build(app, build_job)
        job["log"].extend(build_job["log"])
        if build_job["error"]:
            job["error"] = f"build failed: {build_job['error']}"
            return

        # Tag+push from the same daemon the build landed on (see
        # _run_packaged_build) — `docker push` auth is client-side, so the
        # node's normal `docker login` works unchanged through -H.
        host = _host_for(app)
        ctx, _dockerfile, built_tag, _build_args = _publish_build_spec(app)
        commit = registry.dev_source_commit(app) or _context_commit(ctx)
        tags = ["latest"] + ([commit[:12]] if commit else [])
        for tag in tags:
            target = f"{app.dockerhub_repo}:{tag}"
            r = subprocess.run(["docker", *_host_args(host), "tag", built_tag, target],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                job["error"] = f"tag {target} failed: {r.stderr.strip()}"
                return
            job["log"].append(f"$ docker push {target}")
            proc = subprocess.Popen(["docker", *_host_args(host), "push", target],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                job["log"].append(line.rstrip())
                job["log"][:] = job["log"][-500:]
            proc.wait(timeout=900)
            if proc.returncode != 0:
                job["error"] = f"push {target} failed: exit code {proc.returncode}"
                return
            job["tags"].append(target)
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
    finally:
        job["done"] = True


def publish_status(app_id: str) -> dict | None:
    job = _publish_jobs.get(app_id)
    if not job:
        return None
    return {"done": job["done"], "error": job["error"], "log_tail": job["log"][-30:],
            "repo": job["repo"], "tags": job["tags"]}
