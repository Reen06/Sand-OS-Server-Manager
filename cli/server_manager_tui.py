#!/usr/bin/env python3
"""SandOS Server Manager — terminal Busy/Available toggle for headless boxes.

Same idea as windows/sandos_launcher.py and linux/sandos_busy_toggle.py's GUI
toggle, but curses-based for a server with no display — this is what
`server-manager` runs (see install.sh, which symlinks that command to this
script). Standard library only (curses + urllib), talks to the SM's own
loopback-trusted API (http://localhost:8170 — see main.py's
_require_admin_or_local), so no Hub login is ever needed here.
"""
from __future__ import annotations
import curses
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


def _draw(win, info: dict | None, error: str | None, message: str) -> None:
    win.erase()
    h, w = win.getmaxyx()
    win.addstr(0, 0, "SandOS Server Manager".center(w - 1), curses.A_BOLD)
    win.addstr(1, 0, "─" * (w - 1))

    if error:
        win.addstr(3, 2, f"SM not reachable: {error}", curses.color_pair(2) | curses.A_BOLD)
        win.addstr(5, 2, "Retrying…")
    else:
        busy = bool(info.get("busy"))
        override = bool(info.get("busy_override_allowed"))
        node = info.get("node_name") or ""

        win.addstr(3, 2, f"Node: {node}")
        status_text = "BUSY" if busy else "Available"
        color = curses.color_pair(2) if busy else curses.color_pair(1)
        win.addstr(4, 2, "Status: ")
        win.addstr(status_text, color | curses.A_BOLD)

        win.addstr(6, 2, f"Remote override allowed: {'yes' if override else 'no'}")

        win.addstr(9, 2, "[Space/Enter]", curses.A_BOLD)
        win.addstr(f"  {'Set Available' if busy else 'Set Busy'}")
        win.addstr(10, 2, "[o]", curses.A_BOLD)
        win.addstr(f"            {'Disallow' if override else 'Allow'} Hub admins to override")
        win.addstr(11, 2, "[q]", curses.A_BOLD)
        win.addstr("            Quit")

    if message:
        win.addstr(h - 2, 2, message, curses.A_DIM)
    win.refresh()


def _main(stdscr) -> None:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    stdscr.timeout(2000)   # redraw/poll every 2s while waiting for a key

    info: dict | None = None
    error: str | None = None
    message = ""

    while True:
        try:
            info = get_info()
            error = None
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            error = str(e)

        _draw(stdscr, info, error, message)
        message = ""
        key = stdscr.getch()

        if key in (ord("q"), ord("Q")):
            break
        if error:
            continue   # nothing actionable while unreachable
        if key in (ord(" "), 10, 13, curses.KEY_ENTER):
            try:
                set_busy(not bool(info.get("busy")))
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                message = f"Failed: {e}"
        elif key in (ord("o"), ord("O")):
            try:
                set_override_allowed(not bool(info.get("busy_override_allowed")))
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                message = f"Failed: {e}"


def main() -> None:
    curses.wrapper(_main)


if __name__ == "__main__":
    main()
