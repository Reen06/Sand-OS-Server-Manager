#!/usr/bin/env python3
"""Apply the full-UI scale slider to KiCad's own settings.

KiCad ignores GDK_SCALE — verified live 2026-08-17: the container AND the
running kicad process both had GDK_SCALE=3 and the UI did not change size at
all. It is wxWidgets and scales its chrome from its own config, so the slider
has to be written where KiCad actually reads it: appearance.toolbar_icon_size
in kicad_common.json, in pixels.

SM_UI_SCALE carries the full-UI slider's value (see ui_prefs.env_for). Text
stays on GDK_DPI_SCALE from the FINE slider, so the two remain independent:
this sizes the chrome, that sizes the text.

Run before every launch, so closing and reopening KiCad inside the streamed
desktop picks up a new value without recreating the container.
"""
import glob
import json
import os

# KiCad's own default. Scaling from this constant rather than from whatever is
# currently in the file matters: reading the current value back would compound
# on every launch, so 2x would become 4x, then 8x.
BASE_ICON_PX = 24

# Where the scale comes from, freshest first:
#
#   /tmp/sm-ui-scale  written by ui_prefs.apply_live when the slider moves
#   SM_UI_SCALE       container env, FROZEN when the container was created
#
# The file has to win. Container env cannot change under a running container,
# so after moving the slider the env still holds the OLD number — and since
# this script runs on every app launch, reopening KiCad would faithfully write
# the stale value back and undo the change. That is exactly the "works, but
# only after a container restart" behaviour this ordering fixes.
_raw = ""
try:
    with open("/tmp/sm-ui-scale") as fh:
        _raw = fh.read().strip()
except OSError:
    pass
scale = float(_raw or os.environ.get("SM_UI_SCALE") or 1)
size = max(16, int(round(BASE_ICON_PX * scale)))

for f in glob.glob("/home/ubuntu/.config/kicad/*/kicad_common.json"):
    try:
        with open(f) as fh:
            cfg = json.load(fh)
        cfg.setdefault("appearance", {})["toolbar_icon_size"] = size
        tmp = f + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, f)          # atomic: never leave a half-written config
        print(f"[kicad-scale] toolbar_icon_size={size} (scale {scale}) in {f}")
    except Exception as e:          # noqa: BLE001 - never block the launch
        print(f"[kicad-scale] could not scale {f}: {e}")
