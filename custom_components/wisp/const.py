"""Constants for Wisp."""
from datetime import timedelta

DOMAIN = "wisp"
VERSION = "0.1.0"  # keep in sync with manifest.json
PLATFORMS = ["binary_sensor", "sensor"]
TITLE = "Wisp"

# ESPHome project of the node firmware (firmware/common/base.yaml), advertised over mDNS
PROJECT_NAME = "albert-canfield.wisp-node"
MANUFACTURER, MODEL = PROJECT_NAME.split(".")  # as the ESPHome integration names the device

SUBENTRY_NODE = "node"  # node subentry data: mac, host, name

NODE_PORT = 47010
SUBSCRIBE_INTERVAL = timedelta(seconds=3)  # the node's lease lasts 10 s
LINK_TIMEOUT = 10.0  # s without a report before a link's entities go unavailable
WRITE_INTERVAL = 1.0  # s between state writes per entity; reports come 5 times a second
RESOLVE_INTERVAL = 60.0  # s to keep a resolved host name
PROBE_TRIES = 3  # subscriptions, 1 s apart, when adding a node by address
