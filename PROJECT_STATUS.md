# Project Status — Sand-OS Server Manager

> **Checkpoint: 2026-07-16 — every catalogued app verified working end-to-end.**
> This supersedes the June 2026 planning-phase notes below (kept at the bottom
> for history). If something breaks after this point, this is the reference
> for how the system was actually wired when it last worked in full. Full
> blow-by-blow debugging history for everything summarized here:
> `docs/Known Issues & Fixes.md` in this repo.

## What's actually running, right now

Sand-OS Server Manager (SM) runs on **CortexPC** as
`sandos-server-manager.service` (systemd, enabled) — NOT a manually-launched
`run.sh` process. Its env file, `/etc/sandos-server-manager.env`, must mirror
`server/run.sh`'s own defaults (`SM_HUB_URL`, `SM_HUB_INTERNAL_URL`,
`SM_LLM_API_KEY`, `SM_OLLAMA_NAS_TRANSFER`) — confirmed once out of sync
(missing everything but `SM_HUB_URL`, pointed at the wrong address), which
silently drops Hub SSO's fast LAN path, the Hub LLM Router seeding, and
NAS-based Ollama model transfer while SM still *looks* like it's running fine.

Every catalogued app (FreeCAD, Filebrowser, WebCAD/CAM, HeliX, OpenMapper, Ray
Optics, Renode, EngineeringPaper, OpenFOAM GUI, ParaView, Stirling PDF,
Nextcloud, Ollama, Open WebUI) launches and serves correctly as of this
checkpoint — verified individually, not assumed.

## Two Docker daemons — never let them share a subnet

- **Main `docker.service`** — bridge `docker0`, `172.17.0.0/16`. Hosts
  Ollama, Open WebUI, the NFS server, and anything without its own
  `build_context` pointed at the USB drive.
- **USB app-hosting dockerd** (`sandos-usb-dockerd@<uuid>.service`) — a
  *separate* daemon for apps whose images are too large for local disk
  (FreeCAD, OpenMapper, HeliX, RayOptics, Renode, OpenFOAM GUI, ParaView).
  Has its own dedicated bridge, `docker-usb0` at `172.30.0.1/24`
  (pre-created via `ExecStartPre` in `sandos-usb-dockerd@.service`, every
  boot). **Do not let this daemon fall back to the default `docker0` name/
  subnet** — that caused a genuine duplicate-IP collision on one L2 segment
  (confirmed live: a USB-daemon container got handed the exact same address
  the NFS server already held on the main daemon), which manifested as
  intermittent, unkillable D-state NFS hangs that looked nothing like a
  networking bug until traced all the way down.

## Persistence — what self-heals on reboot vs. what doesn't

**Self-healing (systemd units, survive any reboot):**
- `sandos-server-manager.service` — the real SM process.
- `sandos-docker0-forward-fix.service` (oneshot, `After=docker.service`) —
  re-asserts docker0's NAT/FORWARD rules every boot. Without it: every
  container on the *default* bridge silently loses ALL outbound internet
  access (DNS, package installs, external APIs) the moment those rules go
  missing — confirmed this can happen without any obvious trigger (not just
  the known ufw-ordering race), and host-to-container traffic (published
  ports) keeps working the whole time, which is exactly why it's easy to
  miss. `systemd/install.sh` installs and enables this as part of normal setup.
- `sandos-usb-dockerd@<uuid>.service` — `After=ufw.service docker.service`,
  own bridge auto-created on every start.

**Plain committed code (re-read fresh on every SM start, no separate
deploy step since SM runs directly from this checkout):**
- All `AppDef` config in `registry.py` — `mem_limit`, `own_subdomain`,
  `ready_path`/`ready_bad_status`, `env`, `mounts`.

## Apps with their own dedicated subdomain (`own_subdomain=True`)

Some apps' compiled frontends hard-code absolute paths (asset URLs,
`fetch()` calls, WebSocket URLs) that cannot survive being served under any
subpath (`/apps/stream/{id}/`) — the browser resolves them against the real
origin root regardless of where the page itself was loaded from. These get
their own DuckDNS subdomain instead (free — DuckDNS resolves any subdomain
to the same IP and answers the DNS-01 challenge with the same TXT record):

| App | Subdomain | Why |
|---|---|---|
| Open WebUI | `ai.<domain>` | absolute `/_app`, `/static` asset paths |
| Stirling PDF | `pdf.<domain>` | `fetch()` to absolute `/api/v1/...`; also needed `SECURITY_ENABLELOGIN=false` (its own login fights our SSO) and `mem_limit=2g` (was OOMing on JVM Metaspace at 1g) |
| EngineeringPaper | `calc.<domain>` | absolute `/assets/index-*.js` tags |
| OpenFOAM GUI | `cfd.<domain>` | absolute CSS/JS, `fetch('/api/lan-info')`, a service worker registered at absolute scope `/` |
| ParaView | `pv.<domain>` | absolute WebSocket URL construction — see below, this one needed four separate fixes stacked together |

Every `own_subdomain` app also needed: the base-href rewrite *skipped*
(`proxy.py`), and the PWA manifest/icon scope computed as bare `/` not
`/stream/{app}/` (`pwa.py`'s `_scope()`) — because each dedicated Caddy host
already unconditionally prepends `/stream/{app}` to every request it gets,
so anything WE also prefix gets double-counted and 404s.

### ParaView specifically — five stacked fixes, in order found
1. Absolute WebSocket URL → `own_subdomain=True` (routing fixed, page still blank).
2. Double-prefixed manifest/icon links (the `_scope()` bug above, affects all
   `own_subdomain` apps, only ParaView's own functionality depended on it).
3. Apache's `mod_proxy` circuit breaker: one failed connection to the
   launcher backend (a same-container sibling process that starts a beat
   after Apache does) locks that backend out for a **60-second cooldown** —
   every request in that window 503s with no further connection attempt at
   all. Fixed with a thin custom image layer (`containers/paraview/
   Dockerfile`) adding `retry=0` to the launcher's `ProxyPass`.
4. The launcher's session response hardcodes `ws://localhost/proxy?...` —
   fine for same-machine testing, broken for literally any proxied/remote
   access. Fixed with a proxy-side rewrite (`_rewrite_paraview_session` in
   `proxy.py`) swapping in the real `Host` header.
5. The readiness check (`ready_path="paraview/"`) initially still waved a
   503 through as "the server answered, therefore ready" — added
   `ready_bad_status=(503,)` so the dashboard's own launch-wait loop
   actually waits for the real dependency instead of racing it.
6. (Separately, not a bug fix) ParaView had no NAS mount at all and ran
   `mode="shared"` — switched to `mode="per-user"` with the same NAS home
   mount FreeCAD/Filebrowser/Nextcloud already use, so there's actually
   something to load.

## The `/apps/*` routing gap (fixed 2026-07-16, lived in SandOS-Hub)

The Hub's main dashboard domain strips `/apps` before forwarding to its own
backend — existed for the IP-based vhosts from the start
(`config/Caddyfile`'s `handle_path /apps/*`), but was **missing from the
DuckDNS domain block** until fixed. Any app WITHOUT its own subdomain
(FreeCAD, Filebrowser, WebCAD, Nextcloud, Ollama) was unreachable via the
domain (mobile/WireGuard/remote — anything not a raw LAN IP) the entire
time this was missing. Always worked over LAN IP, which is exactly why it
went unnoticed for so long.

## Login-redirect-through-login (fixed 2026-07-16, also SandOS-Hub)

An expired-session top-level navigation (a PWA shortcut, most commonly)
redirects through the Hub's login page and back. The "back" URL must be
built differently depending on whether the app has its own subdomain (bare
`/{path}` — that host's Caddy rewrite re-adds the app prefix automatically)
or not (`/stream/{app_id}/{path}` — hits the main domain's final catch-all
directly). Getting this wrong silently sends you back to the dashboard
instead of the app after re-login.

## Known, real, non-bug limitations

- **ParaViewWeb's filter/reader catalog is a small curated subset**
  (`paraview/web/_default_proxies.py` inside the image), not the full
  desktop ParaView. Streamlines (`StreamTracer`) work; most mesh-cleanup
  filters and most file formats beyond legacy `.vtk`/`.xdmf` do not. A
  full-desktop-ParaView streaming app (Selkies-based, same pattern as
  FreeCAD) was discussed as a genuinely easier alternative for the complete
  feature set + GPU rendering — not yet built.
- **iOS installed-PWA browser chrome** (back/share buttons) reappears
  whenever a navigation leaves the app's manifest `scope` — e.g. the
  login-redirect bounce above. This is WebKit's own behavior; no manifest
  setting or web API suppresses it. Force-quitting and reopening the PWA
  resets it.

---

## History (pre-2026-07-16 planning phase — kept for context, not current)

_Last updated: 2026-06-30_

**Phase: PLANNING — orchestrator greenfield; first app image ✅ DONE + VALIDATED**

**Built + validated (2026-06-30):** `containers/freecad-streamer/` — a fresh container boots **straight into FreeCAD 1.1.1** in a GPU-accelerated Selkies WebRTC desktop (confirmed by screenshot on the GTX 1060 via CDI; FreeCAD autostarts). Selkies `nvidia-egl-desktop` base + FreeCAD 1.1.1 AppImage (from `FreeCAD/FreeCAD` releases; the `-Bundle` repo lags); `run-gpu.sh` (NVENC) / `run-dev.sh` (x264). Web UI on :8099.
- **Requires GPU** (nvidia-container-toolkit, CDI `--device nvidia.com/gpu=all`) even for software encode — no pure-CPU path on this base.
- Session runs **as root, HOME=/home/ubuntu**; autostart in `/home/ubuntu/.config/autostart`; `LD_PRELOAD=VirtualGL` session-wide → GPU GL.
- Source also cloned at `/home/control/ALL_CNC_Programs/FreeCAD`.

### What this is (one line)
On-demand orchestrator that spawns/streams/reaps containerized server-side apps
across the homelab — first workload: multi-instance streamed FreeCAD.

### Decisions locked (2026-06-29)
- **Build on the SandOS Hub chassis** (auth, node registry, apps lifecycle UI) rather than from scratch — reuse its patterns; Selkies/Kasm only as a streaming fallback.
- **Docker-everywhere** as the instance substrate (uniform across Proxmox / Ubuntu / desktop / Pi; Windows-capable). Proxmox-native LXC deferred.
- **WebRTC streaming (Selkies-GStreamer)** for the per-instance stream — the path to a "buttery smooth" 3D viewport (not VNC).
- **Mixed GPU/CPU fleet** — scheduler is GPU-aware; GPU nodes for heavy 3D, llvmpipe nodes for light work.
- **Compute/data separation** — per-user project files on the NAS, mounted into whatever ephemeral instance spawns.

### Control surface locked (2026-06-30)
- **Hub-owned app catalogue, mirrored to every device** (Hub = catalogue/identity/scopes authority, SM = execution engine).
- App **kinds**: local / server-shared / server-streamed (small devices = thin launcher+viewer).
- **Four instance modes** per app: per-user, shared/singleton, ephemeral, per-group/scope. (In practice: `mode="shared"` and `mode="per-user"` are what's implemented and used today.)
- **Idle = no active session**; default **tiered suspend→stop** (overridable) + manual Connect/Suspend/Stop/Restart.
