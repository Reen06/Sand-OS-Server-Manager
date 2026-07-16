#!/usr/bin/env python3
"""SandOS Server Manager — Busy/Available control for a native Linux desktop.

For a Linux machine running the Server Manager directly (installed via the
repo root's install.sh — no WSL/Windows involved), that installer already
sets up the systemd service; there's nothing left to "provision" here. This
script is just the small daily-use Busy/Available toggle, the Linux twin of
windows/sandos_launcher.py's GUI (same idea, minus the WSL setup steps that
don't apply on native Linux). Standard library only (tkinter + urllib) — no
`pip install` needed.

Talks to http://localhost:8170 — the SM's own loopback-trusted API (see
main.py's _require_admin_or_local), so no Hub login is ever needed here;
this is the same machine the SM node is running on.
"""
from __future__ import annotations
import json
import urllib.error
import urllib.request

SM_BASE = "http://localhost:8170"


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


def main() -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("SandOS Server Manager")
    root.geometry("320x160")

    status_var = tk.StringVar(value="Checking…")
    status_label = tk.Label(root, textvariable=status_var, font=("Sans", 12, "bold"))
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


if __name__ == "__main__":
    main()
