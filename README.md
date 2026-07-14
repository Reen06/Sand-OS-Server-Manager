# Sand-OS Server Manager

The **Server Manager** is the compute engine behind Sand-OS apps — it spawns,
streams, and manages containerised applications (FreeCAD, Nextcloud, Files,
WebCAD, and more) and connects them to your Sand-OS Hub for auth, fleet
placement, and shared storage.

It runs on any Linux machine with Docker: a gaming PC, a home server, a cloud
VM, or co-located alongside the Hub on the same device.

---

## App catalogue

| App | Type | GPU required |
|-----|------|:---:|
| **FreeCAD 1.1** | Streamed desktop (Selkies/WebRTC) | ✓ |
| **Files** (Filebrowser) | Web app | — |
| **WebCAD** | Web app | — |
| **HeliX Motion** | Web app | — |
| **OpenMapper** | Web app | — |
| **Ray Optics** | Web app | — |
| **Renode** | Web app | — |
| **Stirling PDF** | Web app | — |
| **Nextcloud** (+MariaDB, Redis, Collabora) | Web app | — |

GPU-accelerated apps require an NVIDIA GPU with
[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
installed and CDI configured. Without a GPU, only web apps are available
(they work on any hardware, including ARM).

---

## Prerequisites

- **Docker** — required on the machine running the Server Manager
- **Python 3.11+** — for the orchestrator process itself
- **systemd** — for the managed service (Debian / Ubuntu / Arch)
- **Sand-OS Hub** — optional but recommended for auth and fleet features

---

## Quick install

### 1. Clone the repo

```bash
git clone https://github.com/<your-user>/Sand-OS-Server-Manager
cd Sand-OS-Server-Manager
```

### 2. Run the installer

```bash
sudo bash install.sh
```

The installer is a guided TUI — it asks a few questions and then writes
`/etc/sandos-server-manager.env`, installs the Python venv, and enables the
`sandos-server-manager` systemd service. No manual file editing needed.

---

## Deployment modes

The installer asks which of three modes applies to your setup.

### Mode 1 — Same LAN as Hub

```
┌──────────────────────┐           ┌──────────────────────┐
│   Sand-OS Hub        │           │   Server Manager     │
│   10.0.0.177         │◄─────────►│   10.0.0.164:8170    │
│                      │  LAN      │                      │
└──────────────────────┘           └──────────────────────┘
          │                                   │
          └──────── Browser ──────────────────┘
                  /apps/* proxied
```

The Hub reaches the Server Manager directly over the local network. No VPN
needed. The Hub's Caddy routes `/apps/*` to the Server Manager's port.

### Mode 2 — Remote machine via VPN / WireGuard

```
┌──────────────────────┐   WireGuard    ┌──────────────────────┐
│   Sand-OS Hub        │   10.79.x.x    │   Server Manager     │
│   Home LAN           │◄──────────────►│   Remote / cloud     │
│                      │                │   SM_LAN_IP=WG IP    │
└──────────────────────┘                └──────────────────────┘
```

The Server Manager's WireGuard IP is used as both the API endpoint and a
TURN relay candidate, so the Hub and browsers can reach it across network
boundaries. Set `SM_TURN_EXTRA_HOST` to the WireGuard IP (the installer
does this automatically in VPN mode).

> **Zero-touch remote enrollment:** on the Hub's Fleet page, click "Enroll
> Remote Server…" to mint a one-time link (15 minutes, single use). Clone
> this repo on the remote box, run `sudo bash install.sh`, pick VPN mode,
> and paste the link when asked — the installer brings up the WireGuard
> tunnel, reads its assigned IP, and pre-fills the Hub URL and network
> identity steps for you. The remote peer is scoped server-side (reachable
> only through the Hub and the fleet NAS, never the rest of the home LAN)
> and needs no new router port-forward. Fleet registration afterward stays
> a deliberate manual step — click "Add device" with the printed IP once
> the installer finishes.

### Mode 3 — Co-located on the Hub device

```
┌──────────────────────────────────────────────┐
│   Hub device  (e.g. SandOS Hub)              │
│                                              │
│   SandOS Hub  :80 / :443  (Caddy)            │
│   Server Mgr  :8170       (SM API)           │
│   Docker                                     │
└──────────────────────────────────────────────┘
          ▲
     Single device, apps + Hub share hardware
```

Both services run on one machine. The Hub's Caddy routes `/apps/*` to
`127.0.0.1:8170` on the loopback interface. Useful if the Hub device is
powerful enough to run apps (mini-PC, NUC, etc.).

---

## TUI walkthrough

When you run `sudo bash install.sh` you'll step through:

| Step | What it asks |
|------|-------------|
| **1 — Mode** | Same LAN / Remote VPN (optionally paste an enrollment link) / Co-located |
| **2 — Network identity** | LAN or WireGuard IP, port, friendly node name |
| **3 — Hub connection** | Hub URL for SSO (blank = standalone), TLS verify, mount path |
| **4 — Shared storage** | NAS enable/disable, NFS host + root path |
| **5 — Compute capacity** | GPU enable/disable, max concurrent app slots |
| **Summary** | Review all settings before writing anything |

All settings are written to `/etc/sandos-server-manager.env`. Re-run
`sudo bash install.sh` at any time to change them — the installer is
idempotent.

---

## Hub integration (Caddy snippet)

After installation the installer prints the exact Caddy block to add to
your Hub's Caddyfile. Here's the template:

```
# Inside your Hub's https://… site block — before the catch-all

redir /apps /apps/
handle_path /apps/* {
    reverse_proxy <SM_LAN_IP>:8170
}
```

For co-located mode use `127.0.0.1:8170` as the upstream.

Reload Caddy after editing:

```bash
sudo systemctl reload caddy
```

---

## Configuration reference

All settings live in `/etc/sandos-server-manager.env` (written by the
installer). You can edit this file directly and restart the service.

| Variable | Default | Description |
|----------|---------|-------------|
| `SM_LAN_IP` | auto-detected | IP this node advertises — Hub and browsers connect here |
| `SM_PORT` | `8170` | Port the Server Manager listens on |
| `SM_NODE_NAME` | hostname | Friendly name shown in the Hub's fleet view |
| `SM_TURN_EXTRA_HOST` | _(empty)_ | Extra TURN IP for VPN/WireGuard clients (set automatically in VPN mode) |
| `SM_HUB_URL` | _(empty)_ | Hub URL for SSO — leave blank for standalone mode |
| `SM_HUB_VERIFY_TLS` | `false` | Set `true` if the Hub uses a public CA certificate |
| `SM_EXTERNAL_BASE` | `/apps` | URL path the Hub mounts the SM under |
| `SM_NAS_ENABLED` | `false` | Enable NFSv4-backed shared storage |
| `SM_NAS_HOST` | `$SM_LAN_IP` | IP of the NFS server |
| `SM_NAS_ROOT` | `/home/control/sandos-nas` | Path exported as the NFS pseudo-root (fsid=0) |
| `SM_GPU` | auto-detected | Override GPU detection (`true`/`false`) |
| `SM_SLOT_COUNT` | `8` | Max concurrent app instances across all users |

After editing, restart the service:

```bash
sudo systemctl restart sandos-server-manager
```

---

## Service management

```bash
# View live logs
journalctl -u sandos-server-manager -f

# Check status
systemctl status sandos-server-manager

# Restart after config change
sudo systemctl restart sandos-server-manager

# Stop / disable
sudo systemctl stop sandos-server-manager
sudo systemctl disable sandos-server-manager
```

---

## NAS setup (optional)

If you enabled the NAS layer and this machine is the NFS host:

```bash
sudo apt install nfs-kernel-server

# Create the NAS root
sudo mkdir -p /home/control/sandos-nas

# Export it — edit /etc/exports and add:
/home/control/sandos-nas  10.0.0.0/8(rw,fsid=0,no_subtree_check,all_squash,anonuid=1000,anongid=1000)

# Apply
sudo exportfs -ra
sudo systemctl enable --now nfs-server
```

---

## Development

To run the Server Manager in dev mode without installing the systemd service:

```bash
cd server
SM_LAN_IP=10.0.0.164 ./run.sh
# → http://10.0.0.164:8170
```

The `run.sh` script creates a local `.venv` and starts uvicorn in reload mode.

---

## Related projects

| Project | Location | Role |
|---------|----------|------|
| **Sand-OS** | `../Sand-OS/` | Travel router gateway node |
| **SandOS Hub** | `../SandOS Hub/` | Always-on home hub — auth, fleet UI, mesh orchestrator |
| **Sand-OS-Server-Manager** | *(this repo)* | App/compute orchestrator |
