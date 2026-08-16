#!/usr/bin/env python3
"""Power and lock agent for a Sand-OS display panel.

Owns the backlight and the idle clock, and tells the browser page what state the
panel is in. The page owns what the user SEES (the lock overlay, the PIN pad);
this owns when the screen is bright, dim, or off, and whether the panel is
locked.

WHY THE CLOCK LIVES HERE AND NOT IN THE PAGE. The browser is the wrong place to
time this. Its timers are throttled when the tab is backgrounded or the machine
is under load, and a page that has been suspended comes back believing no time
has passed — so a panel would sit bright all night, or fail to lock. This process
reads real input events from the kernel. It cannot be throttled, it does not care
what the browser is doing, and it keeps dimming correctly when chromium is
restarting, updating, or wedged.

WHY THE BACKLIGHT AND NOT DPMS. A backlight is one integer in sysfs: writing it
is instant and survives the compositor misbehaving. Blanking through the
compositor is slower to come back, can flash, and needs the compositor healthy —
the opposite of what a wall panel needs.

THE STATES

    FULL  ──dim_after──▶  DIM  ──off_after──▶  OFF  (+ locks itself)
      ▲                                          │
      └──────────── any touch ◀──────────────────┘

Turning fully off is what locks the panel, on the principle that a screen you
cannot see is a screen you should have to unlock. While locked, a touch brings
the backlight up so the PIN can be entered, and the panel goes dark again after
its own shorter timeout — so a locked panel someone brushed past does not sit
lit all night.

KEEP-AWAKE exists for the case that makes all of this dangerous: a machine
control app showing live output while it is actually cutting. Dimming that is
merely annoying; blanking and locking it mid-operation is not. While a keep-awake
is held the panel stays bright — but a MANUAL lock still overrides it, because
the person walking away is a better judge than the machine.
"""
from __future__ import annotations

import errno
import glob
import json
import os
import select
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_EV_SIZE = struct.calcsize("llHHi")
_BL_ROOT = "/sys/class/backlight"
_STATE_FILE = "/var/lib/sandos-display/state.json"

# Loopback only. The page that talks to this is served over https, and browsers
# treat http://127.0.0.1 as a secure context, so it is reachable from that page
# without being reachable from anywhere else on the network.
_BIND = ("127.0.0.1", 8371)


class Panel:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bl, self.maxb = self._find_backlight()
        self.full = self.maxb
        self.dim_after = int(os.environ.get("SANDOS_DIM_AFTER", "90"))
        self.off_after = int(os.environ.get("SANDOS_OFF_AFTER", "300"))
        self.lock_off_after = int(os.environ.get("SANDOS_LOCK_OFF_AFTER", "300"))
        self.dim_pct = max(1, min(int(os.environ.get("SANDOS_DIM_LEVEL", "12")), 100))
        self._load()

        self.state = "full"
        self.locked = False
        self.keepawake = False
        self.last_input = time.monotonic()
        self.locked_at = 0.0
        self._apply("full")

    # ── backlight ────────────────────────────────────────────────────────────
    def _find_backlight(self) -> tuple[str | None, int]:
        for d in sorted(glob.glob(os.path.join(_BL_ROOT, "*"))):
            try:
                with open(os.path.join(d, "max_brightness")) as f:
                    return d, int(f.read().strip())
            except (OSError, ValueError):
                continue
        return None, 255

    def _write(self, value: int) -> None:
        if not self.bl:
            return
        try:
            with open(os.path.join(self.bl, "brightness"), "w") as f:
                f.write(str(value))
        except OSError as e:
            if e.errno == errno.EACCES:
                print("panel: cannot set brightness — is the user in 'video'?",
                      file=sys.stderr, flush=True)

    def _apply(self, state: str) -> None:
        # 0 is genuinely off on this panel: the DSI backlight goes dark and the
        # screen reads as off, without involving the compositor at all.
        self._write({"full": self.full,
                     "dim": max(1, (self.maxb * self.dim_pct) // 100),
                     "off": 0}[state])
        self.state = state

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        """Timings survive a restart. Someone who set 30 minutes from the lock
        screen should not silently get 90 seconds back after a reboot."""
        try:
            with open(_STATE_FILE) as f:
                d = json.load(f)
            for k in ("dim_after", "off_after", "lock_off_after", "dim_pct"):
                if isinstance(d.get(k), int):
                    setattr(self, k, d[k])
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
            tmp = _STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"dim_after": self.dim_after, "off_after": self.off_after,
                           "lock_off_after": self.lock_off_after,
                           "dim_pct": self.dim_pct}, f)
            os.replace(tmp, _STATE_FILE)      # never a half-written file
        except OSError as e:
            print(f"panel: could not save timings: {e}", file=sys.stderr, flush=True)

    # ── events ───────────────────────────────────────────────────────────────
    def touched(self) -> None:
        with self.lock:
            self.last_input = time.monotonic()
            if self.locked:
                # Wake to let the PIN be entered, but stay locked, and restart
                # the locked-blank countdown from this touch.
                self.locked_at = time.monotonic()
            if self.state != "full":
                self._apply("full")

    def do_lock(self) -> None:
        with self.lock:
            self.locked = True
            self.locked_at = time.monotonic()
            # A manual lock means "I am walking away", so it beats keep-awake.
            self.keepawake = False
            self._apply("full")     # the PIN pad has to be readable

    def do_unlock(self) -> None:
        with self.lock:
            self.locked = False
            self.last_input = time.monotonic()
            self._apply("full")

    def set_keepawake(self, on: bool) -> None:
        with self.lock:
            self.keepawake = bool(on)
            if on and not self.locked:
                self.last_input = time.monotonic()
                if self.state != "full":
                    self._apply("full")

    def set_timings(self, **kw) -> None:
        with self.lock:
            for k in ("dim_after", "off_after", "lock_off_after"):
                if isinstance(kw.get(k), int) and kw[k] >= 0:
                    setattr(self, k, kw[k])
            if isinstance(kw.get("dim_pct"), int):
                self.dim_pct = max(1, min(kw["dim_pct"], 100))
            self._save()

    def snapshot(self) -> dict:
        with self.lock:
            return {"state": self.state, "locked": self.locked,
                    "keepawake": self.keepawake,
                    "idle": round(time.monotonic() - self.last_input, 1),
                    "dim_after": self.dim_after, "off_after": self.off_after,
                    "lock_off_after": self.lock_off_after, "dim_pct": self.dim_pct}

    # ── the clock ────────────────────────────────────────────────────────────
    def tick(self) -> None:
        with self.lock:
            now = time.monotonic()
            if self.locked:
                # Locked: bright only briefly after a touch, then dark again.
                if (self.lock_off_after > 0
                        and now - self.locked_at >= self.lock_off_after
                        and self.state != "off"):
                    self._apply("off")
                return
            if self.keepawake:
                return                      # live operation — stay bright
            idle = now - self.last_input
            if self.off_after > 0 and idle >= self.off_after:
                if self.state != "off":
                    self._apply("off")
                # Going fully dark IS the lock. A screen you cannot see should
                # not still be showing a machine's controls to the next person.
                self.locked = True
                self.locked_at = now
            elif self.dim_after > 0 and idle >= self.dim_after:
                if self.state == "full":
                    self._apply("dim")


PANEL: Panel | None = None


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        # The page is served by the Hub, so it is a different origin to this
        # loopback agent and needs CORS to read the reply at all.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):          # noqa: N802
        self._json(204, {})

    def do_GET(self):              # noqa: N802
        if self.path.startswith("/state"):
            self._json(200, PANEL.snapshot())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):             # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            body = {}
        p = self.path.split("?")[0]
        if p == "/lock":
            PANEL.do_lock()
        elif p == "/unlock":
            PANEL.do_unlock()
        elif p == "/wake":
            PANEL.touched()
        elif p == "/keepawake":
            PANEL.set_keepawake(bool(body.get("on")))
        elif p == "/timings":
            PANEL.set_timings(**{k: v for k, v in body.items() if isinstance(v, int)})
        else:
            return self._json(404, {"error": "not found"})
        self._json(200, PANEL.snapshot())

    def log_message(self, *a):     # quiet; journald has enough to say
        return


def _input_loop(panel: Panel) -> None:
    devs = []
    for d in sorted(glob.glob("/dev/input/event*")):
        try:
            devs.append(os.open(d, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
    if not devs:
        print("panel: no readable input devices", file=sys.stderr, flush=True)
    print(f"panel: watching {len(devs)} input devices", flush=True)
    while True:
        r, _, _ = select.select(devs, [], [], 1.0) if devs else ((), (), ())
        for fd in r:
            try:
                while os.read(fd, _EV_SIZE):
                    pass
            except OSError:
                pass
            panel.touched()
        panel.tick()


def main() -> int:
    global PANEL
    PANEL = Panel()
    s = PANEL.snapshot()
    print(f"panel: backlight={PANEL.bl} max={PANEL.maxb} "
          f"dim@{s['dim_after']}s off@{s['off_after']}s "
          f"locked-off@{s['lock_off_after']}s", flush=True)

    srv = ThreadingHTTPServer(_BIND, Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="panel-api").start()
    print(f"panel: api on http://{_BIND[0]}:{_BIND[1]}", flush=True)

    _input_loop(PANEL)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
