#!/usr/bin/env python3
"""SandOS Server Manager — Windows/WSL launcher + Busy/Available control.

Run with no arguments once WSL is already set up: opens a small window with
the Busy/Available toggle for this machine's SM node.

Run with --setup (or it auto-detects nothing is set up yet) to provision a
WSL distro, clone the repo into it, and run the EXISTING Linux install.sh
unmodified inside it — WSL2 supports systemd (once /etc/wsl.conf turns it
on), so nothing about the SM's own install process needs reinventing here,
only "get a distro, run the normal installer inside it, make Windows wake
WSL2 on logon so systemd (already `systemctl enable`d by install.sh) starts
the service on its own."

Talks to the SM node over http://localhost:8170 — WSL2 forwards that port to
Windows' own localhost automatically, no networking setup needed. Uses only
the Python standard library (tkinter + urllib) so there's nothing to `pip
install` before this runs.
"""
from __future__ import annotations
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SM_PORT = 8170
SM_BASE = f"http://localhost:{SM_PORT}"
REPO_URL = "https://github.com/Reen06/Sand-OS-Server-Manager.git"
RAW_SELF_URL = "https://raw.githubusercontent.com/Reen06/Sand-OS-Server-Manager/main/windows/sandos_launcher.py"
FRESH_INSTALL_DISTRO = "Ubuntu"   # what `wsl --install -d Ubuntu` actually registers
CLONE_DIR = "~/Sand-OS-Server-Manager"
FIREWALL_RULE_NAME = "SandOS Server Manager"
# Per-app dedicated subdomains the Hub's own frontend uses (SandOS Hub's
# frontend/js/pages/apps.js, _SUBDOMAIN_APPS) — duplicated here since the SM
# has no reason to know about Hub frontend routing otherwise. Keep in sync
# if the Hub ever adds/removes one.
HUB_APP_SUBDOMAINS = ["pdf", "ai", "calc", "cfd", "pv"]
_HOSTS_MARK_BEGIN = "# --- SandOS: DNS hairpin fix (sandos_launcher.py) ---"
_HOSTS_MARK_END = "# --- end SandOS ---"


# ── local SM API calls (loopback — no Hub login needed, see main.py's
# _require_admin_or_local) ──────────────────────────────────────────────────
def _api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{SM_BASE}{path}", timeout=5) as r:
        return json.loads(r.read())


def _api_post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{SM_BASE}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get_info() -> dict:
    return _api_get("/api/sm/info")


def set_busy(enabled: bool) -> dict:
    return _api_post("/api/sm/busy", {"enabled": enabled})


def set_override_allowed(allowed: bool) -> dict:
    return _api_post("/api/sm/busy/override-permission", {"allowed": allowed})


# ── first-run WSL setup ──────────────────────────────────────────────────────
def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


def _list_distros() -> list[str]:
    """Exact registered distro names, one per line — `-l -q` (quiet) is the
    documented script-parseable form; `-l -v`'s padded/asterisk'd table is
    for humans, not for picking out a name to pass to `-d` later. Still
    strips UTF-16 nulls some Windows builds emit either way."""
    r = _run(["wsl", "-l", "-q"])
    out = (r.stdout or "").replace("\x00", "")
    return [line.strip() for line in out.splitlines() if line.strip()]


def find_ubuntu_distro() -> str | None:
    """The real registered name of whatever Ubuntu-based distro exists, if
    any — NOT a hardcoded "Ubuntu" assumption. A distro installed from the
    Microsoft Store (e.g. "Ubuntu 24.04.1 LTS") registers as "Ubuntu-24.04",
    not "Ubuntu" — passing the wrong literal name to `wsl -d <name>` fails
    with WSL_E_DISTRO_NOT_FOUND even though a perfectly good distro exists."""
    for name in _list_distros():
        if "ubuntu" in name.lower():
            return name
    return None


# ── LAN reachability ──────────────────────────────────────────────────────────
# WSL2's default network is NAT'd and isolated: something bound inside WSL is
# reachable from THIS Windows machine via localhost (WSL's own automatic
# forwarding), but NOT from other devices on the LAN hitting this machine's
# real IP — nothing is actually listening there at all. Two fixes, tried in
# order: mirrored networking (Windows 11 22H2+ / recent WSL — WSL shares the
# host's real network directly, nothing else to maintain) and, if that isn't
# available or doesn't verify, a netsh port-forward to WSL's current internal
# IP (works on any WSL2 version, but that IP changes every restart, so it has
# to be re-applied each time WSL wakes — wired into the same logon task that
# already wakes WSL).
def _wslconfig_path() -> Path:
    return Path.home() / ".wslconfig"


def _mirrored_networking_configured() -> bool:
    path = _wslconfig_path()
    if not path.exists():
        return False
    return "networkingmode" in path.read_text().lower() and "mirrored" in path.read_text().lower()


def _enable_mirrored_networking() -> None:
    path = _wslconfig_path()
    lines = path.read_text().splitlines() if path.exists() else []
    if "[wsl2]" not in lines:
        lines.append("[wsl2]")
    out: list[str] = []
    in_wsl2 = False
    written = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_wsl2 = stripped == "[wsl2]"
        if in_wsl2 and stripped.lower().startswith("networkingmode"):
            out.append("networkingMode=mirrored")
            written = True
            continue
        out.append(line)
    if not written:
        idx = out.index("[wsl2]")
        out.insert(idx + 1, "networkingMode=mirrored")
    path.write_text("\n".join(out) + "\n")


def _windows_lan_ip() -> str | None:
    """Best-effort: this machine's own LAN IP, used only to self-test whether
    mirrored networking actually took effect (imperfect proxy for "can
    another machine reach me," but a reasonable signal from this same box)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def _restart_wsl(distro: str) -> None:
    _run(["wsl", "--shutdown"])
    time.sleep(2)
    _run(["wsl", "-d", distro, "--", "true"])
    time.sleep(3)


def _wsl_internal_ip(distro: str) -> str | None:
    r = _run(["wsl", "-d", distro, "--", "hostname", "-I"])
    parts = (r.stdout or "").split()
    return parts[0] if parts else None


def _refresh_port_forward(distro: str) -> bool:
    """Idempotent: delete any existing rule for this port, then re-add it
    pointing at WSL's CURRENT internal IP (it changes every WSL restart in
    NAT mode). Needs Administrator — netsh portproxy/firewall both do."""
    wsl_ip = _wsl_internal_ip(distro)
    if not wsl_ip:
        print("Couldn't determine the WSL2 VM's internal IP — skipping port forwarding.")
        return False
    _run(["netsh", "interface", "portproxy", "delete", "v4tov4",
         f"listenport={SM_PORT}", "listenaddress=0.0.0.0"])
    r = _run(["netsh", "interface", "portproxy", "add", "v4tov4",
             "listenaddress=0.0.0.0", f"listenport={SM_PORT}",
             f"connectaddress={wsl_ip}", f"connectport={SM_PORT}"])
    if r.returncode != 0:
        print(f"Couldn't set up port forwarding (needs Administrator): {(r.stderr or '').strip()}")
        print("Run this script as Administrator, or set it up yourself:")
        print(f"  netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 "
              f"listenport={SM_PORT} connectaddress={wsl_ip} connectport={SM_PORT}")
        return False
    _run(["netsh", "advfirewall", "firewall", "add", "rule",
         f"name={FIREWALL_RULE_NAME}", "dir=in", "action=allow",
         "protocol=TCP", f"localport={SM_PORT}"])
    print(f"Port forwarding set: this machine's LAN IP:{SM_PORT} → WSL {wsl_ip}:{SM_PORT}")
    return True


def _verify_lan_reachable() -> bool:
    ip = _windows_lan_ip()
    if not ip:
        return False
    try:
        with urllib.request.urlopen(f"http://{ip}:{SM_PORT}/api/sm/info", timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return False


def setup_lan_reachability(distro: str) -> None:
    print("\nMaking this node reachable from other machines on your LAN "
          "(not just from this Windows box itself)...")
    print("Trying mirrored networking first (Windows 11 22H2+ / recent WSL only)...")
    _enable_mirrored_networking()
    _restart_wsl(distro)

    if _verify_lan_reachable():
        print("Confirmed reachable — mirrored networking is working, nothing else to do.")
        return

    print("Mirrored networking didn't verify as reachable (older Windows/WSL, or it "
          "just needs a bit longer) — falling back to port forwarding instead.")
    _refresh_port_forward(distro)


def refresh_network() -> None:
    """What the logon Scheduled Task actually runs: wake WSL, and if mirrored
    networking ISN'T configured (meaning the port-forward fallback is in
    use), redo the forward since WSL's internal IP may have changed since
    last boot. Mirrored networking needs no per-boot refresh at all."""
    distro = find_ubuntu_distro()
    if not distro:
        return
    _run(["wsl", "-d", distro, "--", "true"])
    if not _mirrored_networking_configured():
        time.sleep(2)
        _refresh_port_forward(distro)


# ── Hub DNS hairpin fix ──────────────────────────────────────────────────────
# A Hub reached over a public domain (DuckDNS etc.) resolves to the router's
# WAN IP — and most home routers refuse to loop a connection from inside the
# LAN back out through their own public IP ("NAT hairpinning"). The main
# dashboard is usually fine (often reached via the Hub's LAN IP directly),
# but the Hub's own per-app dedicated subdomains (Stirling PDF, Open WebUI,
# ...) always use the public domain, so a LAN client hits this every time —
# confirmed live: DNS resolved correctly, the connection itself just hung.
# Cheapest real fix: an explicit Windows hosts-file entry pointing straight
# at the Hub's LAN IP, bypassing public DNS/the router entirely for these
# specific names.
def _windows_hosts_path() -> Path:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(system_root) / "System32" / "drivers" / "etc" / "hosts"


def _hub_hostname() -> str | None:
    try:
        hub_url = get_info().get("hub_url") or ""
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return None
    if not hub_url:
        return None
    return urlparse(hub_url).hostname


def _build_hosts_block(hostnames: list[str], lan_ip: str) -> list[str]:
    return [_HOSTS_MARK_BEGIN] + [f"{lan_ip} {h}" for h in hostnames] + [_HOSTS_MARK_END]


def _merge_hosts_file(existing: str, hostnames: list[str], lan_ip: str) -> str:
    """Idempotent: strips any previous SandOS-managed block (so a re-run with
    a changed IP replaces cleanly, not duplicates) before appending the
    current one. Everything else in the file is left untouched."""
    lines = existing.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == _HOSTS_MARK_BEGIN:
            skipping = True
            continue
        if line.strip() == _HOSTS_MARK_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    out += [""] + _build_hosts_block(hostnames, lan_ip)
    return "\n".join(out) + "\n"


def fix_hub_dns_hairpin() -> None:
    hostname = _hub_hostname()
    if not hostname:
        print("No Hub URL configured on this node (standalone mode) — nothing to fix.")
        return

    print(f"\nThe Hub ({hostname}) and its per-app subdomains "
          f"({', '.join(f'{s}.{hostname}' for s in HUB_APP_SUBDOMAINS)}) resolve to "
          "your router's public IP — most home routers won't loop a LAN connection "
          "back through their own public address, so those specific addresses can "
          "hang or fail from inside your own network.")
    lan_ip = input(f"Enter the Hub's LAN IP (e.g. 10.0.0.177) to fix this, "
                   "or press Enter to skip: ").strip()
    if not lan_ip:
        print("Skipped — you can re-run this anytime with --fix-hub-dns.")
        return

    hostnames = [hostname] + [f"{s}.{hostname}" for s in HUB_APP_SUBDOMAINS]
    path = _windows_hosts_path()
    try:
        existing = path.read_text() if path.exists() else ""
        path.write_text(_merge_hosts_file(existing, hostnames, lan_ip))
    except PermissionError:
        print(f"\nCouldn't write {path} (needs Administrator). Add these lines "
              f"yourself (Notepad → Run as administrator → open that file):")
        for line in _build_hosts_block(hostnames, lan_ip):
            print(f"  {line}")
        return
    print(f"Done — {len(hostnames)} hostnames now resolve straight to {lan_ip}.")
    _run(["ipconfig", "/flushdns"])


def setup() -> None:
    print("=== SandOS Server Manager — Windows/WSL setup ===")
    distro = find_ubuntu_distro()
    if not distro:
        print("No Ubuntu-based WSL distro found — installing one now.")
        print("Windows may require a reboot partway through this step; if so, "
              "just run this script again afterward to continue.")
        _run(["wsl", "--install", "-d", FRESH_INSTALL_DISTRO])
        print("If a reboot was requested, reboot now and re-run this script.")
        return

    print(f"Found WSL distro '{distro}'. Ensuring systemd is enabled (needed "
          "for the SM's own service, and unmodified from how it runs on Linux)...")
    r = _run(["wsl", "-d", distro, "--", "bash", "-lc",
             "grep -q '^systemd=true' /etc/wsl.conf 2>/dev/null || "
             "(sudo mkdir -p /etc && printf '[boot]\\nsystemd=true\\n' | sudo tee -a /etc/wsl.conf)"])
    if r.returncode != 0:
        print(f"\nCouldn't check/enable systemd in '{distro}': {(r.stderr or '').strip()}")
        print("Setup stopped here — fix the error above and re-run this script.")
        return
    print("If systemd was just enabled for the first time, WSL needs a restart: "
          "run `wsl --shutdown` in a terminal, then re-run this script.")

    print(f"\nMake sure Docker Desktop's WSL2 integration is turned ON for "
          f"'{distro}' (Docker Desktop → Settings → Resources → WSL Integration) "
          "before continuing — this script can't flip that toggle for you.")
    input("Press Enter once that's confirmed... ")

    print("Cloning (or updating) the repo and running the normal Linux installer "
          "inside WSL (this is the SAME install.sh used on every other node — "
          "nothing Windows-specific about it once you're inside the distro)...")
    # Always pull latest on an existing checkout — silently reusing whatever
    # was cloned on a previous, possibly-failed --setup run means any fix
    # pushed since then would never actually reach this machine.
    clone_and_install = (
        f"if [ -d {CLONE_DIR}/.git ]; then "
        f"  git -C {CLONE_DIR} pull --ff-only; "
        f"else "
        f"  git clone {REPO_URL} {CLONE_DIR}; "
        f"fi; "
        f"cd {CLONE_DIR} && bash install.sh"
    )
    r = subprocess.run(["wsl", "-d", distro, "--", "bash", "-lc", clone_and_install])
    if r.returncode != 0:
        print(f"\nSetup did NOT finish — the clone/install step inside WSL failed "
              f"(exit code {r.returncode}). See the error above, fix it, and "
              "re-run this script; autostart was NOT configured yet.")
        return

    setup_lan_reachability(distro)
    fix_hub_dns_hairpin()

    print("\nSetting up autostart: a Scheduled Task that wakes this WSL distro "
          "(and keeps it reachable on the LAN) at logon. Once WSL2 is up, "
          "systemd starts the already-enabled sandos-server-manager service "
          "on its own — nothing further needed.")
    script_path = Path(__file__).resolve()
    task_cmd = f'python "{script_path}" --refresh-network'
    r = _run(["schtasks", "/create", "/tn", "SandOS Server Manager (WSL wake)",
             "/tr", task_cmd, "/sc", "onlogon", "/rl", "highest", "/f"])
    if r.returncode != 0:
        print(f"\nCouldn't create the logon Scheduled Task: {(r.stderr or '').strip()}")
        print("The install itself succeeded — you'll just need to start WSL "
              "yourself (or create the task manually) until this is fixed.")
        return

    print("\nSetup complete. Run this script again with no arguments to open "
          "the Busy/Available control window.")


# ── Busy/Available GUI ───────────────────────────────────────────────────────
def run_gui() -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("SandOS Server Manager")
    root.geometry("320x160")

    status_var = tk.StringVar(value="Checking…")
    status_label = tk.Label(root, textvariable=status_var, font=("Segoe UI", 12, "bold"))
    status_label.pack(pady=(16, 8))

    toggle_btn = tk.Button(root, text="…", width=20)
    toggle_btn.pack(pady=4)

    override_var = tk.BooleanVar()
    override_cb = tk.Checkbutton(root, text="Allow Hub admins to override",
                                 variable=override_var)
    override_cb.pack(pady=(12, 0))

    _state = {"busy": False}
    _NET_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError, OSError)

    # Every urllib call above is BLOCKING network I/O — calling any of them
    # directly from a button handler or root.after() freezes the entire
    # window (Tkinter's mainloop can't process anything, including redraws,
    # while its own thread is stuck waiting on a socket) for up to that
    # call's full timeout. Confirmed live: a slow/unreachable SM made every
    # button appear completely unresponsive. Fixed by running the network
    # call in a background thread and only ever touching widgets back on
    # the main thread via root.after(0, ...) — Tkinter itself isn't
    # thread-safe for direct widget access from a non-main thread.
    def _run_async(work, on_done) -> None:
        def _worker():
            try:
                result = work()
            except _NET_ERRORS as e:
                root.after(0, lambda: on_done(None, e))
                return
            root.after(0, lambda: on_done(result, None))
        threading.Thread(target=_worker, daemon=True).start()

    def refresh() -> None:
        def _done(info, err):
            if err is not None:
                status_var.set("SM not reachable")
                toggle_btn.config(text="…", state="disabled")
                root.after(3000, refresh)
                return
            _state["busy"] = bool(info.get("busy"))
            status_var.set("BUSY" if _state["busy"] else "Available")
            status_label.config(fg="#c99640" if _state["busy"] else "#34d399")
            toggle_btn.config(text="Set Available" if _state["busy"] else "Set Busy", state="normal")
            override_var.set(bool(info.get("busy_override_allowed")))
            root.after(5000, refresh)
        _run_async(get_info, _done)

    def on_toggle() -> None:
        toggle_btn.config(state="disabled")
        def _done(_result, err):
            if err is not None:
                messagebox.showerror("SandOS", f"Couldn't reach the Server Manager: {err}")
            refresh()
        _run_async(lambda: set_busy(not _state["busy"]), _done)

    def on_override_toggle() -> None:
        override_cb.config(state="disabled")
        desired = override_var.get()
        def _done(_result, err):
            override_cb.config(state="normal")
            if err is not None:
                messagebox.showerror("SandOS", f"Couldn't reach the Server Manager: {err}")
                override_var.set(not desired)
        _run_async(lambda: set_override_allowed(desired), _done)

    toggle_btn.config(command=on_toggle)
    override_cb.config(command=on_override_toggle)
    refresh()
    root.mainloop()


# ── self-update ───────────────────────────────────────────────────────────────
# Unlike the WSL-side checkout (git pull --ff-only on every --setup run), this
# file itself just sits standalone wherever it was first downloaded to on the
# Windows filesystem — nothing about running it ever re-fetches it. A real
# user hit exactly this: ran a flag (--fix-hub-dns) that didn't exist yet in
# their locally-saved copy, and it silently fell through to the GUI instead
# of erroring, with no indication they were running a stale file at all.
def _self_update() -> None:
    try:
        this_file = Path(__file__).resolve()
        current = this_file.read_text(encoding="utf-8")
        with urllib.request.urlopen(RAW_SELF_URL, timeout=5) as r:
            latest = r.read().decode("utf-8")
    except Exception:  # noqa: BLE001 — never block a real run over a failed update check
        return
    if latest == current or not latest.strip():
        return
    try:
        this_file.write_text(latest, encoding="utf-8")
    except OSError:
        return
    print("Updated to the latest version — restarting...")
    # NOT os.execv(): confirmed live, it hung indefinitely on Windows when
    # this script was invoked non-interactively (over SSH, with piped
    # stdin) — Windows has no true POSIX exec, so CPython emulates it by
    # spawning a brand-new process, and that spawn didn't inherit the
    # piped stdin/stdout cleanly, leaving the "replaced" process stuck
    # waiting on a handle that would never receive anything. subprocess.run
    # has well-defined, predictable stream inheritance on every platform
    # (it's what the rest of this script already uses everywhere else) —
    # wait for the fresh instance to finish, then exit with its result.
    result = subprocess.run([sys.executable, str(this_file)] + sys.argv[1:])
    sys.exit(result.returncode)


def main() -> None:
    _self_update()
    if "--refresh-network" in sys.argv:
        refresh_network()
        return
    if "--fix-hub-dns" in sys.argv:
        fix_hub_dns_hairpin()
        return
    if "--setup" in sys.argv or not find_ubuntu_distro():
        setup()
        return
    run_gui()


if __name__ == "__main__":
    main()
