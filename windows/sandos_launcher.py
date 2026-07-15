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
import subprocess
import sys
import urllib.error
import urllib.request

SM_PORT = 8170
SM_BASE = f"http://localhost:{SM_PORT}"
REPO_URL = "https://github.com/Reen06/Sand-OS-Server-Manager.git"
DISTRO = "Ubuntu"
CLONE_DIR = "~/Sand-OS-Server-Manager"


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


def wsl_distro_exists(name: str = DISTRO) -> bool:
    r = _run(["wsl", "-l", "-v"])
    # wsl -l -v prints UTF-16 on some Windows builds; decode defensively.
    out = (r.stdout or "").replace("\x00", "")
    return name.lower() in out.lower()


def setup() -> None:
    print("=== SandOS Server Manager — Windows/WSL setup ===")
    if not wsl_distro_exists():
        print(f"No {DISTRO} WSL distro found — installing one now.")
        print("Windows may require a reboot partway through this step; if so, "
              "just run this script again afterward to continue.")
        _run(["wsl", "--install", "-d", DISTRO])
        print("If a reboot was requested, reboot now and re-run this script.")
        return

    print(f"{DISTRO} found. Ensuring systemd is enabled (needed for the SM's "
          "own service, and unmodified from how it runs on Linux)...")
    _run(["wsl", "-d", DISTRO, "--", "bash", "-lc",
          "grep -q '^systemd=true' /etc/wsl.conf 2>/dev/null || "
          "(sudo mkdir -p /etc && printf '[boot]\\nsystemd=true\\n' | sudo tee -a /etc/wsl.conf)"])
    print("If systemd was just enabled for the first time, WSL needs a restart: "
          "run `wsl --shutdown` in a terminal, then re-run this script.")

    print("\nMake sure Docker Desktop's WSL2 integration is turned ON for "
          f"'{DISTRO}' (Docker Desktop → Settings → Resources → WSL Integration) "
          "before continuing — this script can't flip that toggle for you.")
    input("Press Enter once that's confirmed... ")

    print("Cloning the repo and running the normal Linux installer inside WSL "
          "(this is the SAME install.sh used on every other node — nothing "
          "Windows-specific about it once you're inside the distro)...")
    clone_and_install = (
        f"test -d {CLONE_DIR}/.git || git clone {REPO_URL} {CLONE_DIR}; "
        f"cd {CLONE_DIR} && bash install.sh"
    )
    subprocess.run(["wsl", "-d", DISTRO, "--", "bash", "-lc", clone_and_install])

    print("\nSetting up autostart: a Scheduled Task that wakes this WSL distro "
          "at logon. Once WSL2 is up, systemd starts the already-enabled "
          "sandos-server-manager service on its own — nothing further needed.")
    task_cmd = f'wsl.exe -d {DISTRO} -- true'
    _run(["schtasks", "/create", "/tn", "SandOS Server Manager (WSL wake)",
          "/tr", task_cmd, "/sc", "onlogon", "/rl", "highest", "/f"])

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

    def refresh() -> None:
        try:
            info = get_info()
        except (urllib.error.URLError, ConnectionError, TimeoutError):
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

    def on_toggle() -> None:
        toggle_btn.config(state="disabled")
        try:
            set_busy(not _state["busy"])
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            messagebox.showerror("SandOS", f"Couldn't reach the Server Manager: {e}")
        refresh()

    def on_override_toggle() -> None:
        try:
            set_override_allowed(override_var.get())
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            messagebox.showerror("SandOS", f"Couldn't reach the Server Manager: {e}")
            override_var.set(not override_var.get())

    toggle_btn.config(command=on_toggle)
    override_cb.config(command=on_override_toggle)
    refresh()
    root.mainloop()


def main() -> None:
    if "--setup" in sys.argv or not wsl_distro_exists():
        setup()
        return
    run_gui()


if __name__ == "__main__":
    main()
