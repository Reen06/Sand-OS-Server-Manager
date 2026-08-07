"""What is physically plugged into this machine's USB ports.

Two different questions share one answer here, because the map draws them the
same way — a thick wire off the machine that owns the port:

  storage      — USB disks, reported PER PARTITION. A single drive split in
                 two is two usable volumes to anyone looking at the map, and
                 they carry different labels and fill independently. The
                 physical disk is still named on each, so "one cable, two
                 volumes" stays visible rather than being flattened away.

  peripherals  — dev boards, serial adapters and robot controllers. These are
                 the ones with a future: a board listed here is a board that
                 can later be flashed or bridged into a simulator, so each
                 record carries what a targeting step would need — vendor and
                 product id, the serial port it claimed, and a stable serial
                 number where the device offers one.

DELIBERATELY NOT EVERYTHING. `lsusb` on any real machine is mostly root hubs,
billboard descriptors, HID and the machine's own WiFi dongle. Drawing those
would bury the one board someone actually plugged in, and the WiFi adapter in
particular is already on the map as a network path — showing it again as a USB
peripheral draws one physical thing twice. The filter is by design, and
`ignored` reports how many were dropped so the UI can say so honestly rather
than implying nothing else is attached.

Cached, because the Hub polls the endpoint that carries this every few seconds
and neither lsusb nor lsblk is free on a Pi-class board.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time

from .usb_storage import _NON_POSIX_FSTYPES

_TTL = 15.0                      # seconds; a plug/unplug shows up within this
_lock = threading.Lock()
_cache: dict = {}
_cache_at = 0.0

# USB-serial bridges and native-USB MCUs worth surfacing. Keyed by vendor id,
# with the well-known product ids that identify a specific board where it
# helps. Anything matched here is a thing someone deliberately attached.
_INTERESTING_VENDORS = {
    "303a": "Espressif",          # ESP32-S2/S3/C3 native USB
    "10c4": "Silicon Labs",       # CP210x — the classic ESP32 devkit bridge
    "1a86": "QinHeng",            # CH340/CH9102 — common on cheap ESP32 boards
    "0403": "FTDI",
    "2341": "Arduino",
    "2a03": "Arduino",
    "16c0": "Teensy",
    "239a": "Adafruit",
    "1b4f": "SparkFun",
    "2e8a": "Raspberry Pi",       # RP2040 / Pico
    "0483": "STMicroelectronics",  # ST-Link, many robot controllers
    "1ffb": "Pololu",             # servo/motor controllers
    "0694": "LEGO",
}

# Classes that are never interesting on their own: hubs and the USB-C
# billboard descriptors a dock advertises. Matched on the lsusb class string.
_BORING_CLASS = re.compile(r"\b(hub|billboard)\b", re.I)


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def _tty_map() -> dict[str, str]:
    """usb device path → the /dev/tty* it claimed, when it claimed one.

    A board with no driver bound (a bad cable, or a missing kernel module)
    appears with no tty — which is exactly the state worth showing, because it
    is the difference between "plugged in" and "usable".
    """
    out: dict[str, str] = {}
    for line in _run(["bash", "-c",
                       "for d in /sys/class/tty/tty{USB,ACM}*; do "
                       "[ -e \"$d\" ] || continue; "
                       "echo \"$(basename $d) $(readlink -f $d)\"; done"]).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def peripherals() -> tuple[list[dict], int]:
    """(interesting USB peripherals, count of devices deliberately ignored)."""
    devices, ignored = [], 0
    ttys = _tty_map()
    for line in _run(["lsusb"]).splitlines():
        m = re.match(r"Bus (\d+) Device (\d+): ID ([0-9a-f]{4}):([0-9a-f]{4})\s*(.*)",
                     line.strip(), re.I)
        if not m:
            continue
        bus, dev, vid, pid, name = m.groups()
        name = (name or "").strip()
        vid, pid = vid.lower(), pid.lower()
        if vid == "1d6b":                       # linux foundation root hubs
            continue                            # not a plugged-in thing at all
        if vid not in _INTERESTING_VENDORS or _BORING_CLASS.search(name):
            ignored += 1
            continue
        # Which tty this device claimed, if any. Matched on the bus/device pair
        # appearing in the tty's sysfs path, which is how the kernel links them.
        tty = ""
        for tname, path in ttys.items():
            if f"/usb{int(bus)}/" in path or f"{int(bus)}-" in path:
                tty = f"/dev/{tname}"
                break
        devices.append({
            "kind": "peripheral",
            "vendor_id": vid,
            "product_id": pid,
            "vendor": _INTERESTING_VENDORS.get(vid, ""),
            "name": name or f"{vid}:{pid}",
            "bus": int(bus),
            "device": int(dev),
            "tty": tty,
            # No tty means no driver bound — usually a charge-only cable or a
            # missing bridge module. Surfaced so the UI can say why a board
            # that is clearly plugged in still cannot be talked to.
            "usable": bool(tty),
        })
    return devices, ignored


def storage() -> list[dict]:
    """USB disks with their partitions, shaped for the map.

    Reuses usb_storage's own detection so there is one definition of "is this
    a USB disk" rather than two that can disagree.
    """
    try:
        from . import usb_storage
        disks = usb_storage.usb_disks()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for d in disks:
        parts = []
        for p in d.get("partitions") or []:
            parts.append({
                "kind": "volume",
                "name": p.get("name", ""),
                "uuid": p.get("uuid", ""),
                "label": p.get("label") or p.get("name", ""),
                "size": p.get("size", ""),
                "fstype": p.get("fstype", ""),
                "mountpoint": p.get("mountpoint") or "",
                "mounted": bool(p.get("mountpoint")),
            })
        out.append({
            "kind": "disk",
            "name": d.get("name", ""),
            "size": d.get("size", ""),
            # A blank, unpartitioned drive is still plugged in and still worth
            # drawing — it just has nothing on it yet.
            "partitions": parts,
        })
    return out


def all_disks() -> list[dict]:
    """EVERY real disk on this machine, not just the removable ones.

    The USB view above answers "what is plugged in". This answers "what storage
    does this machine have", which is the question the Hub's NAS settings need:
    a bulk node's capacity is usually an internal SATA array that no USB scan
    will ever see, and choosing where a pool lives is impossible without it.

    Two things are reported that the raw device list does not give you, because
    both are decisions the UI would otherwise have to make wrongly:

      posix_ok  — whether the filesystem can hold Unix ownership. vfat/exfat/
                  ntfs cannot, so they can carry bulk media but never user
                  homes, and can never back a second dockerd. Surfaced per
                  partition so the UI can say why a 2 TB drive is not offered
                  as pool storage instead of silently omitting it.

      is_system — whether this disk carries / or /boot. Never offer to
                  repartition the disk the OS is running from.

    loop and rom devices are excluded: a snap-heavy Ubuntu box has thirty-odd
    loop mounts that are squashfs images of installed software, not storage.
    """
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,TRAN,SIZE,LABEL,UUID,MOUNTPOINT,TYPE,HOTPLUG,FSTYPE,ROTA,RM,FSAVAIL,FSSIZE,MODEL"],
            capture_output=True, text=True, timeout=10).stdout
        devices = json.loads(out).get("blockdevices", [])
    except Exception:  # noqa: BLE001
        return []

    def _part(p: str | None) -> str:
        return p or ""

    def _num(v) -> int:
        """lsblk's numeric fields, whatever type this util-linux hands back.

        With -b these are byte counts, but older util-linux emits them as JSON
        STRINGS while newer versions emit numbers — so arithmetic on them threw
        TypeError on one machine and worked on another running the same code.
        Coerce rather than trust the type.
        """
        if v is None or v == "":
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    disks = []
    for d in devices:
        if d.get("type") != "disk":
            continue
        parts, is_system = [], False
        for p in d.get("children") or []:
            if p.get("type") not in ("part", "lvm", "crypt"):
                continue
            mp = _part(p.get("mountpoint"))
            if mp in ("/", "/boot", "/boot/efi"):
                is_system = True
            fs = (p.get("fstype") or "").lower()
            parts.append({
                "name": p.get("name", ""),
                "uuid": p.get("uuid") or "",
                "label": p.get("label") or "",
                "size_bytes": _num(p.get("size")),
                "fstype": fs,
                "mountpoint": mp,
                "mounted": bool(mp),
                "avail_bytes": _num(p.get("fsavail")),
                "used_bytes": max(0, _num(p.get("fssize")) - _num(p.get("fsavail"))),
                # The single fact that decides what this partition may be used
                # for. Unformatted ("") is not POSIX-capable yet, but is a
                # candidate for formatting, which the UI treats differently.
                "posix_ok": bool(fs) and fs not in _NON_POSIX_FSTYPES,
                "formatted": bool(fs),
            })
        disks.append({
            "name": f"/dev/{d.get('name','')}",
            "model": (d.get("model") or "").strip(),
            "size_bytes": _num(d.get("size")),
            "transport": (d.get("tran") or "").lower(),   # sata / usb / nvme
            "rotational": bool(d.get("rota")),            # spinning rust vs SSD
            "removable": bool(d.get("rm")) or bool(d.get("hotplug")),
            "is_system": is_system,
            "partitions": parts,
        })
    return disks


def list_all(force: bool = False) -> dict:
    global _cache, _cache_at
    with _lock:
        now = time.monotonic()
        if not force and _cache and (now - _cache_at) < _TTL:
            return _cache
        periph, ignored = peripherals()
        _cache = {
            "storage": storage(),
            # Every disk, not just the removable ones — what the Hub's NAS
            # settings need in order to offer a real choice of where storage
            # lives.
            "disks": all_disks(),
            "peripherals": periph,
            "ignored": ignored,
            "at": int(time.time()),
        }
        _cache_at = now
        return _cache


if __name__ == "__main__":       # quick manual check on any machine
    print(json.dumps(list_all(force=True), indent=2))
