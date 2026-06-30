# Project Status — Sand-OS Server Manager

> Living local status for this repo. The **project-wide** living history is the
> vault's `00 - Project Log & Current State.md` — update that too. Architecture
> belongs in the vault (see README), not duplicated here.

_Last updated: 2026-06-30_

## Phase: PLANNING — orchestrator greenfield; first app image ✅ DONE + VALIDATED

**Built + validated (2026-06-30):** `containers/freecad-streamer/` — a fresh container boots **straight into FreeCAD 1.1.1** in a GPU-accelerated Selkies WebRTC desktop (confirmed by screenshot on the GTX 1060 via CDI; FreeCAD autostarts). Selkies `nvidia-egl-desktop` base + FreeCAD 1.1.1 AppImage (from `FreeCAD/FreeCAD` releases; the `-Bundle` repo lags); `run-gpu.sh` (NVENC) / `run-dev.sh` (x264). Web UI on :8099.
- **Requires GPU** (nvidia-container-toolkit, CDI `--device nvidia.com/gpu=all`) even for software encode — no pure-CPU path on this base.
- Session runs **as root, HOME=/home/ubuntu**; autostart in `/home/ubuntu/.config/autostart`; `LD_PRELOAD=VirtualGL` session-wide → GPU GL.
- Source also cloned at `/home/control/ALL_CNC_Programs/FreeCAD`.
- TODO: wire `freecad-projects` volume as FreeCAD's project dir; per-user/auth-gated routing is the orchestrator's job.

## What this is (one line)
On-demand orchestrator that spawns/streams/reaps containerized server-side apps
across the homelab — first workload: multi-instance streamed FreeCAD.

## Decisions locked (2026-06-29)
- **Build on the SandOS Hub chassis** (auth, node registry, apps lifecycle UI) rather than from scratch — reuse its patterns; Selkies/Kasm only as a streaming fallback.
- **Docker-everywhere** as the instance substrate (uniform across Proxmox / Ubuntu / desktop / Pi; Windows-capable). Proxmox-native LXC deferred.
- **WebRTC streaming (Selkies-GStreamer)** for the per-instance stream — the path to a "buttery smooth" 3D viewport (not VNC).
- **Mixed GPU/CPU fleet** — scheduler is GPU-aware; GPU nodes for heavy 3D, llvmpipe nodes for light work.
- **Compute/data separation** — per-user project files on the NAS, mounted into whatever ephemeral instance spawns.
- Primary deployment: Server Manager runs on the **Proxmox** box, spawns there by default, calls out to the **personal desktop** when more compute is needed.

→ Full rationale: vault `ADR-0011`.

## Control surface locked (2026-06-30) — vault `ADR-0014`
- **Hub-owned app catalogue, mirrored to every device** (Hub = catalogue/identity/scopes authority, SM = execution engine — resolves the prior in-Hub-vs-separate question).
- App **kinds**: local / server-shared / server-streamed (small devices = thin launcher+viewer).
- **Four instance modes** per app: per-user, shared/singleton, ephemeral, per-group/scope.
- **App Definitions** = the config unit edited via the "wiring" button.
- **Idle = no active session**; default **tiered suspend→stop** (overridable) + manual Connect/Suspend/Stop/Restart.
- **Visibility filtered by scope + device capability.**
- **Cold-start loading screen**; **GPU auto-queue + "back to menu"** when CPU fallback infeasible; **permissioned cross-user viewing (observe)**; **per-app endpoints** (manual or WireGuard auto-discovered) with **owner-chosen placement**.
- → Full design: vault `Compute/Server Manager Apps & Instances.md` + `ADR-0014`.

## Open questions
- Reverse-proxy choice for auth-gated per-instance routing (Traefik vs Caddy vs self-proxied WebRTC signaling).
- Exact FreeCAD image base (GPU/VirtualGL variant + llvmpipe variant).
- Cold-start resume UX; GPU contention policy when per-user GPU instances are maxed.
- Catalogue sync transport (Hub push vs node pull on the existing federation channel).

## Next steps
1. Confirm the Hub chassis pieces to reuse (auth, node registry, `api/apps.py` lifecycle pattern) + how App Definitions extend it.
2. Prototype a single FreeCAD + Selkies Docker image; verify smoothness on a GPU node over LAN.
3. Design the node-spawn interface (Docker API per node) + GPU-aware scheduler.
4. Auth-gated routing PoC (user → their instance only) + session-based idle signal driving suspend→stop.
