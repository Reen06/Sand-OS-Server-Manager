"""Server Manager configuration (env-overridable)."""
import os

# This host's LAN IP — used to point each instance's internal TURN at a reachable
# address so other LAN devices can connect. (Behind the Hub TLS proxy later this
# becomes the Hub hostname.)
LAN_IP = os.environ.get("SM_LAN_IP", "10.0.0.164")

# Where the Server Manager UI/API itself listens.
SM_HOST = os.environ.get("SM_HOST", "0.0.0.0")
SM_PORT = int(os.environ.get("SM_PORT", "8170"))

# Default image for the FreeCAD app.
FREECAD_IMAGE = os.environ.get("SM_FREECAD_IMAGE", "freecad-streamer:dev")

# Per-instance port allocation. Each instance gets a slot; from the slot we derive
# a unique web port, TURN port, and a small UDP relay range so concurrent
# instances never collide.
SLOT_COUNT = int(os.environ.get("SM_SLOT_COUNT", "16"))
WEB_PORT_BASE = int(os.environ.get("SM_WEB_PORT_BASE", "8100"))   # 8100, 8101, ...
TURN_PORT_BASE = int(os.environ.get("SM_TURN_PORT_BASE", "13478")) # 13478, 13479, ...
RELAY_BASE = int(os.environ.get("SM_RELAY_BASE", "40000"))         # 8 ports per slot
RELAY_PER_SLOT = 8

# Instance basic-auth (internal; the Hub proxy will own real auth later).
INSTANCE_USER = os.environ.get("SM_INSTANCE_USER", "user")
INSTANCE_PASSWD = os.environ.get("SM_INSTANCE_PASSWD", "freecad")
