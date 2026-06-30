"""Core data models: App Definitions and Instances.

An App Definition is the declarative 'wiring' for an app (image, mode, gpu,
lifecycle). An Instance is one running (or allocated) copy of an app for a user.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class AppDef:
    id: str
    label: str
    icon: str
    color: str
    desc: str
    image: str
    kind: str = "server-streamed"        # local | server-shared | server-streamed
    mode: str = "per-user"               # per-user | shared | ephemeral | group
    gpu: bool = True
    encoder: str = "nvh264enc"           # nvh264enc (NVENC) | x264enc (CPU)
    # Auto-resize the remote display to the browser window. Fills the viewport,
    # but breaks Selkies' client-rendered cursor at odd sizes — default off so the
    # cursor stays visible (the browser still scales the 1080p stream to fit).
    resize: bool = False
    # keep-alive after disconnect before the instance is stopped; 0 = close right away
    keepalive_seconds: int = 600


@dataclass
class Instance:
    """A port/volume allocation for one (app, user). 'status' is computed live
    from Docker, not stored here."""
    app_id: str
    user: str
    slot: int
    name: str
    web_port: int
    turn_port: int
    relay_min: int
    relay_max: int
    volume: str
