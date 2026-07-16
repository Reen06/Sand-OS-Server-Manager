"""Per-app PWA manifest + icon.

So a streamed app popped out via the dashboard's "Open in window" installs as its
OWN desktop PWA — its own name, icon and scope (`/apps/stream/{id}/`) — distinct
from the whole-SandOS dashboard PWA (scope `/`). This lets you keep a row of
different-looking, different-scoped app shortcuts on your desktop.

The manifest + icon are non-sensitive (the same id/label/icon/color already ship
unauthenticated via `/api/sm/info`), so they're served without auth — Chrome fetches
a page's manifest/icons without credentials, and gating them would break install.
The proxy injects the `<link rel="manifest">` into each app's entry HTML.
"""
from __future__ import annotations

import html
import os
import threading

from .models import AppDef

ICON_DIR = os.path.join(os.path.dirname(__file__), "appicons")

# AppDef color name → hex (mirrors the dashboard app-card accent colors).
_COLORS = {
    "blue": "#3b82f6",
    "green": "#22c55e",
    "amber": "#f59e0b",
    "red": "#ef4444",
    "purple": "#8b5cf6",
    "cyan": "#06b6d4",
    "pink": "#ec4899",
    "slate": "#64748b",
}
_DEFAULT_COLOR = "#3b82f6"
_BG = "#0f1419"  # dashboard theme background


def color_hex(app: AppDef) -> str:
    return _COLORS.get((app.color or "").lower(), _DEFAULT_COLOR)


def _initials(label: str) -> str:
    parts = [p for p in label.replace("/", " ").replace("-", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def icon_svg(app: AppDef) -> str:
    """The app's real brand SVG if one is shipped (appicons/{id}.svg), else a
    generated tile (app color + initials) so every app gets a distinct icon."""
    brand = os.path.join(ICON_DIR, f"{app.id}.svg")
    if os.path.isfile(brand):
        with open(brand, encoding="utf-8") as f:
            return f.read()
    fg = color_hex(app)
    text = html.escape(_initials(app.label))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
        f'<rect width="512" height="512" rx="112" fill="{_BG}"/>'
        f'<rect x="56" y="56" width="400" height="400" rx="96" fill="{fg}"/>'
        '<text x="256" y="270" text-anchor="middle" dominant-baseline="central" '
        'font-family="Segoe UI, Roboto, Helvetica, Arial, sans-serif" '
        f'font-size="210" font-weight="700" fill="#ffffff">{text}</text>'
        "</svg>"
    )


# iOS Safari's "Add to Home Screen" specifically requires a PNG apple-touch-icon
# — unlike the regular favicon <link>, it does NOT accept SVG (a WebKit
# limitation, not a bug on our end), so icon_svg() above can't serve it
# directly. Rasterized once per app then cached in memory: icons are static
# for the process lifetime (derived only from app.id/color/label, none of
# which change at runtime), and cairosvg's render cost (~tens of ms) isn't
# worth paying on every request from every visitor's home-screen install.
_png_cache: dict[str, bytes] = {}
_png_lock = threading.Lock()


def icon_png_180(app: AppDef) -> bytes | None:
    """180x180 PNG rendering of icon_svg(app), Apple's recommended apple-touch-icon
    size. None on any rendering failure (missing cairosvg, malformed SVG, etc.) —
    callers fall back to the SVG link, which every OTHER consumer still uses fine."""
    with _png_lock:
        cached = _png_cache.get(app.id)
    if cached is not None:
        return cached
    try:
        import cairosvg
        png = cairosvg.svg2png(bytestring=icon_svg(app).encode("utf-8"),
                               output_width=180, output_height=180)
    except Exception:  # noqa: BLE001 — best-effort; missing/broken cairosvg isn't fatal
        return None
    with _png_lock:
        _png_cache[app.id] = png
    return png


def _scope(app: AppDef, external_base: str) -> str:
    # An app with own_subdomain is reached via a dedicated Caddy host
    # (calc.<domain>, pdf.<domain>, ...) whose rewrite already lands
    # requests at /stream/{app}/... directly — the /apps prefix only
    # exists for the ordinary dashboard-hosted /apps/stream/ subpath, which
    # doesn't apply on that separate origin. Using the normal prefix here
    # would point the manifest's own start_url/scope, and every injected
    # <link>, at a path that 404s on that host — the same class of bug
    # own_subdomain's proxy.py check already fixes for the base-href case.
    base = "" if app.own_subdomain else external_base
    return f"{base}/stream/{app.id}/"


def manifest(app: AppDef, external_base: str) -> dict:
    scope = _scope(app, external_base)
    icon_url = f"{scope}sm-icon.svg"
    return {
        "id": scope,
        "name": app.label,
        "short_name": app.label,
        "description": app.desc,
        "start_url": scope,
        "scope": scope,
        "display": "standalone",
        "background_color": _BG,
        "theme_color": color_hex(app),
        "icons": [
            {
                "src": icon_url,
                "sizes": "192x192 512x512 any",
                "type": "image/svg+xml",
                "purpose": "any",
            },
        ],
    }


def head_tags(app: AppDef, external_base: str) -> str:
    """The <head> tags injected into an app's entry page so the browser installs
    THIS app (its manifest/icon/scope), not the whole dashboard."""
    scope = _scope(app, external_base)
    return (
        f'<link rel="manifest" href="{scope}sm-app.webmanifest">'
        f'<link rel="icon" type="image/svg+xml" href="{scope}sm-icon.svg">'
        # iOS Safari's "Add to Home Screen" mostly ignores the JSON manifest
        # above and specifically wants this tag (PNG only — unlike the icon
        # link above, Safari does not accept SVG here) — without it, Safari
        # falls back to a low-res favicon or a page screenshot instead of the
        # app's real icon. This was the actual fix that made Open WebUI's
        # installed shortcut icon work; every OTHER app was missing it too,
        # just unreported since fewer people had tried "Add to Home Screen"
        # on them yet. Both routes are already public/unauthenticated
        # (_PWA_ASSETS) so no extra auth-bypass plumbing is needed here.
        f'<link rel="apple-touch-icon" href="{scope}sm-icon-180.png">'
        f'<meta name="theme-color" content="{color_hex(app)}">'
    )
