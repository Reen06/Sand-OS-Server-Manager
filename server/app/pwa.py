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


def manifest(app: AppDef, external_base: str) -> dict:
    scope = f"{external_base}/stream/{app.id}/"
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
    scope = f"{external_base}/stream/{app.id}/"
    return (
        f'<link rel="manifest" href="{scope}sm-app.webmanifest">'
        f'<link rel="icon" type="image/svg+xml" href="{scope}sm-icon.svg">'
        f'<meta name="theme-color" content="{color_hex(app)}">'
    )
