# kicad-streamer

Full **KiCad** (schematic capture + PCB layout + 3D viewer), streamed to any browser
over **WebRTC** — the [[App Definition Standard|streamed]] `kicad` app in the [[Server
Manager]] catalogue. Built on the Selkies `nvidia-egl-desktop` base (headless KDE +
Xvfb + VirtualGL + Selkies WebRTC on port **8080**), copied file-for-file from
`containers/freecad-streamer/` per the four mandatory base-image patches — see vault
"Streamed Apps — The Selkies Base Patches".

> ℹ️ **This base needs the GPU** (nvidia-container-toolkit, CDI) even with the
> `x264enc` *software encoder* — the encoder is software, the GL desktop is not.
>
> ℹ️ **The KDE session runs as root** with `HOME=/home/ubuntu`, so KiCad's autostart
> lives in `/home/ubuntu/.config/autostart` (not `/etc/skel`). `LD_PRELOAD=VirtualGL`
> is set session-wide, so autostarted KiCad gets GPU GL automatically (matters for
> the 3D PCB viewer).

## Build

```bash
cd containers/kicad-streamer
docker build -t kicad-streamer:dev .
```

- **KiCad version:** defaults to **10.0.5 stable**, from the official KiCad AppImage
  mirror (`downloads.kicad.org` / regional mirrors). Note the upstream artifact is a
  **`.tar` wrapping the `.AppImage`**, not a bare AppImage — the Dockerfile unpacks
  the tar first, then extracts the AppImage. Override with:
  ```bash
  docker build -t kicad-streamer:dev \
    --build-arg KICAD_APPIMAGE_TAR_URL=https://mirrors.mit.edu/kicad/appimage/stable/kicad-<version>-x86_64.AppImage.tar .
  ```

## Registered as

`registry.py`'s `CATALOG["kicad"]` — `kind="streamed"`, `mode="per-user"` (each user
gets their own instance/GPU/ports, same shape as `freecad`), `gpu=True`, per-user NAS
home + config/share mounts. One installable variant today (`stable`); add more
`AppVariant` entries the same way FreeCAD's `weekly-dev` does if a rolling/nightly
KiCad build is ever wanted.

## Kiosk (multi-window) mode

Unlike FreeCAD (one main window), KiCad opens the **project manager**, then separate
top-level windows for the **Schematic Editor** and **PCB Editor** as the user opens
them — often minutes into the session. `kicad-kiosk.sh` therefore runs its
maximize-new-windows loop for the whole session (not just a fixed startup window like
FreeCAD's), matching by `WM_CLASS` rather than window title since every KiCad
top-level window is a real editor, not an auxiliary panel/dialog.

## Notes

- Autostart lives in `/home/ubuntu/.config/autostart` (session runs as root,
  HOME=/home/ubuntu) — **not** `/etc/skel`.
- The kiosk launcher also `chmod 755`s the runtime dir so nginx (www-data) can read
  the basic-auth file (else login 500s) — same fix as FreeCAD's launcher.
- KiCad is **wxWidgets**, not Qt — there is no single "scale the whole app" env var
  like FreeCAD's `QT_SCALE_FACTOR`, so this app declares `sandos.ui.app_scale="false"`
  and offers no scale slider in its settings panel.
