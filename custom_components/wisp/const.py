"""Constants for Wisp."""
from datetime import timedelta

DOMAIN = "wisp"
VERSION = "0.1.20"  # keep in sync with manifest.json (cache-busts the card)
PLATFORMS = ["binary_sensor", "button", "sensor"]
TITLE = "Wisp"

# ESPHome project of the node firmware (firmware/common/base.yaml), advertised over mDNS
PROJECT_NAME = "albert-canfield.wisp-node"
MANUFACTURER, MODEL = PROJECT_NAME.split(".")  # as the ESPHome integration names the device

SUBENTRY_NODE = "node"  # node subentry data: mac, host, name, and area when set
CONF_AREA = "area"  # the area a node stands in; its floor is the node's floor
CONF_FLOOR = "floor"
CONF_DURATION = "duration"
CONF_DELAY = "delay"  # seconds before a calibration records

NODE_PORT = 47010
SUBSCRIBE_INTERVAL = timedelta(seconds=3)  # the node's lease lasts 10 s
LINK_TIMEOUT = 10.0  # s without a report before a link's entities go unavailable
HIVE_TIMEOUT = 15.0  # s a node's hive report stays current; they come every 5 s
WRITE_INTERVAL = 1.0  # s between state writes per entity; reports come 5 times a second
QUIET_WRITE_INTERVAL = 60.0  # s: a change too small to matter waits this long (keeps the recorder light)
RESOLVE_INTERVAL = 60.0  # s to keep a resolved host name
PROBE_TRIES = 3  # subscriptions, 1 s apart, when adding a node by address
MAP_INTERVAL = timedelta(seconds=1)  # at most one map update a second per card

# Rooms (thresholds in engine/rooms.py)
ROOMS_INTERVAL = timedelta(seconds=1)  # one feature vector and decision per floor a second
ROOM_LINK_AGE = 3.0  # s: older scores are left out of a floor's features
NO_FLOOR = ""  # nodes and areas without a Home Assistant floor share one, named after the hub
MIN_FLOOR_NODES = 3  # fewer on a floor: a repair issue, since rooms and positions need them
CALIBRATION_SECONDS = 60
STORE_VERSION = 1  # calibration samples in .storage
PLANS_STORE_VERSION = 1  # floor plans and placements in .storage
