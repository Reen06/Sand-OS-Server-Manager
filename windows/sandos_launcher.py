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
FRESH_INSTALL_DISTRO = "Ubuntu"   # what `wsl --install -d Ubuntu` actually registers
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

    print("\nSetting up autostart: a Scheduled Task that wakes this WSL distro "
          "at logon. Once WSL2 is up, systemd starts the already-enabled "
          "sandos-server-manager service on its own — nothing further needed.")
    task_cmd = f'wsl.exe -d {distro} -- true'
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
    if "--setup" in sys.argv or not find_ubuntu_distro():
        setup()
        return
    run_gui()


if __name__ == "__main__":
    main()
