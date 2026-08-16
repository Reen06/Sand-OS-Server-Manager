#!/usr/bin/env python3
"""Backlight dimming for a Sand-OS display panel.

Dims the screen after a period with no touch, and brings it straight back the
instant anyone touches it.

WHY THIS IS NOT DPMS. The obvious approach is to let the compositor blank the
display. On a wall panel that is wrong twice over: a blanked DSI panel takes a
noticeable moment to come back and often shows a flash of black or a cursor,
and — worse — waking it usually requires the compositor to still be healthy. A
backlight is a single integer in sysfs. Writing it is instant, survives the
compositor misbehaving, and dimming to a low level rather than off means the
panel still reads as "on, asleep" from across a room, which is what you want
from something mounted on a wall.

It also stays local ON PURPOSE. A panel whose backlight depends on the Hub being
reachable is worse than one that never dims at all: the first thing you would do
when the network is down is walk up to the screen to find out why.

Reads input events directly rather than asking the compositor, so it works the
same under labwc, X, or a bare framebuffer.
"""
from __future__ import annotations

import errno
import glob
import os
import select
import struct
import sys
import time

# struct input_event: (time_sec, time_usec, type, code, value) — long is 64-bit
# on the aarch64 Pi images this runs on.
_EV_SIZE = struct.calcsize("llHHi")

_BL_ROOT = "/sys/class/backlight"


def _backlight() -> tuple[str, int] | None:
    """(path, max_brightness) for the panel's backlight, or None."""
    for d in sorted(glob.glob(os.path.join(_BL_ROOT, "*"))):
        try:
            with open(os.path.join(d, "max_brightness")) as f:
                return d, int(f.read().strip())
        except (OSError, ValueError):
            continue
    return None


def _touch_devices() -> list[str]:
    """Every input device that could mean 'a person is here'.

    All of them, not just the touchscreen: a panel may have a mouse or keyboard
    plugged in for setup, and waking only on touch would make those feel broken.
    """
    return sorted(glob.glob("/dev/input/event*"))


def _set(path: str, value: int) -> None:
    try:
        with open(os.path.join(path, "brightness"), "w") as f:
            f.write(str(value))
    except OSError as e:
        # EACCES here is the one real deployment trap: the unit must run as a
        # user in the `video` group, or via the udev rule the installer adds.
        if e.errno == errno.EACCES:
            print("dim: no permission to set brightness — is the user in 'video'?",
                  file=sys.stderr, flush=True)
        raise


def main() -> int:
    dim_after = int(os.environ.get("SANDOS_DIM_AFTER", "90"))
    dim_pct = max(1, min(int(os.environ.get("SANDOS_DIM_LEVEL", "12")), 100))

    bl = _backlight()
    if not bl:
        print("dim: no backlight found — nothing to do", file=sys.stderr, flush=True)
        return 0
    path, maxb = bl
    full, dim = maxb, max(1, (maxb * dim_pct) // 100)

    devs = []
    for d in _touch_devices():
        try:
            devs.append(os.open(d, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass          # a device we may not read is not a reason to give up
    if not devs:
        print("dim: no readable input devices", file=sys.stderr, flush=True)
        return 0

    _set(path, full)
    last, dimmed = time.monotonic(), False
    print(f"dim: {path} full={full} dim={dim} after={dim_after}s "
          f"({len(devs)} input devices)", flush=True)

    while True:
        # The timeout is what makes this cost nothing while idle: no polling
        # loop, just a blocking wait that also happens to expire on schedule.
        r, _, _ = select.select(devs, [], [], 1.0)
        for fd in r:
            try:
                while os.read(fd, _EV_SIZE):
                    pass          # drain; the event's content does not matter
            except OSError:
                pass
            last = time.monotonic()
            if dimmed:
                _set(path, full)
                dimmed = False
        if not dimmed and (time.monotonic() - last) >= dim_after:
            _set(path, dim)
            dimmed = True


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
