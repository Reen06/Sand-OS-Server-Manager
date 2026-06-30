# freecad-streamer

Full **FreeCAD**, streamed to any browser over **WebRTC** — the first [[Remote FreeCAD]]
workload for the [[Server Manager]]. Built on the Selkies `nvidia-egl-desktop`
base (headless KDE + Xvfb + VirtualGL + Selkies WebRTC on port **8080**).

> ✅ **Validated 2026-06-30:** FreeCAD 1.1.1 confirmed rendering in the GPU-accelerated
> WebRTC desktop on a GTX 1060 (screenshot). Pipeline: CDI GPU → VirtualGL → KDE → FreeCAD.
>
> ⚠️ **This base needs the GPU** (nvidia-container-toolkit, CDI) even with the `x264enc`
> *software encoder* — the encoder is software, the GL desktop is not. No pure-CPU path.
>
> ℹ️ **The KDE session runs as root** with `HOME=/home/ubuntu`, so FreeCAD autostart lives
> in `/home/ubuntu/.config/autostart` (not `/etc/skel`). `LD_PRELOAD=VirtualGL` is set
> session-wide, so autostarted FreeCAD gets GPU GL automatically.

## Build

```bash
cd containers/freecad-streamer
docker build -t freecad-streamer:dev .
```

- **FreeCAD version:** defaults to **1.1.1 stable** (official AppImage from the
  **`FreeCAD/FreeCAD`** releases — note the `FreeCAD-Bundle` repo lags). Override with:
  ```bash
  docker build -t freecad-streamer:dev \
    --build-arg FREECAD_APPIMAGE_URL=https://github.com/FreeCAD/FreeCAD/releases/download/<tag>/<asset>.AppImage .
  ```
  Source is also cloned locally at `/home/control/ALL_CNC_Programs/FreeCAD` (post-1.1 weekly) if you ever want to build from source instead.

## Run

All scripts need the **NVIDIA container toolkit (CDI)** — see `install-nvidia-toolkit.sh`
(or `run-gpu.sh` header). The base needs the GPU even for software *encode*.

| Script | Use | Encode | Web UI |
|---|---|---|---|
| `./run-gpu.sh` | local / same-host browser | `nvh264enc` (NVENC) | `http://localhost:8099` |
| `./run-dev.sh` | local, lighter encode | `x264enc` (CPU) | `http://localhost:8099` |
| `LAN_IP=<ip> ./run-lan.sh` | **other devices on the LAN** | `nvh264enc` | `http://<LAN_IP>:8099` |

`run-lan.sh` publishes the web UI + the internal **TURN** server (3478 + a small relay
range) and points it at `LAN_IP`, so WebRTC connects from any LAN device. (On a host
where `:8080` is free, `--network=host` is simpler and needs no TURN.)

**Login:** basic-auth user **`user`**, password **`freecad`** (override `PASSWD=…`).

## Kiosk (single-app) mode
The container streams **FreeCAD only** — no KDE desktop. `freecad-kiosk.sh` (the
autostart) removes the plasma panel/wallpaper, keeps KWin, and maximizes FreeCAD.

## Notes / TODO
- Autostart lives in `/home/ubuntu/.config/autostart` (the session runs as root,
  HOME=/home/ubuntu) — **not** `/etc/skel`.
- The kiosk launcher also `chmod 755`s the runtime dir so nginx (www-data) can read
  the basic-auth file (else login 500s).
- Future: a lighter single-app base (Xvfb + minimal WM + Selkies, no KDE) would cut
  RAM/boot vs. kiosk-on-KDE; and wire `freecad-projects` as FreeCAD's project dir.
- `freecad-projects` volume is mounted at `/mnt/freecad-projects` — **TODO:** wire it
  as FreeCAD's default project dir and, in the Server Manager, make it the per-user
  NAS volume (per [[ADR-0011 - App and Compute Orchestration via Server Manager]] compute/data separation).
- Production multi-instance (per-user spawn, auth-gated routing, suspend→stop) is the
  Server Manager's job — this image is just the unit it spawns.
