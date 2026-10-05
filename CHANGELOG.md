# Changelog

## 0.1.0 (unreleased)
First working version: the grid, the hive and motion per link, with the Home Assistant integration and the map card.

### Nodes (`wisp-node`)
- Built on ESPHome (ESP-IDF): restart, safe mode, factory reset, firmware updates from Home Assistant, a fallback hotspot with a setup page, Improv over USB, a web page, health sensors. Builds for ESP32-S3, ESP32-C3 and the original ESP32.
- Self-forming ESP-NOW grid: every node sends a beacon in its own time slot (16 slots, ranked by MAC, aligned to the access point's clock), with no leader. Nodes move through active, quiet (1.5 s), missing (5 min) and forgotten (24 h) by themselves, so a node can be unplugged, rebooted or added at any time.
- Homes with several access points: every node moves to one grid channel (the channel of the lowest BSSID of the network heard well enough) and the best access point on it, remembered across reboots, never stranded on a dead one. ESPHome's own roaming is off.
- CSI from the access point (pings to the gateway, 20 a second) and from every other node (each beacon and relayed row), captured only from known transmitters.
- Motion per link: the shape of the signal across 51 subcarriers, a running spread, a quiet baseline that settles for 20 s and then learns quiet fast and noise slowly; a score where 1 means as quiet as usual, and motion with hysteresis (default threshold 2). The link to the access point is also on the node as "AP motion" and "AP motion score".
- The hive: every node keeps the latest row of every node (relayed, so nodes out of range of each other still learn them), agrees on a hash, and solves the same relative layout of the nodes.
- UDP streams for subscribers: raw CSI (developer switch), link reports 5 times a second, hive reports every 5 s. See docs/PROTOCOL.md.
- The core task runs under the task watchdog. Optional Wi-Fi FTM probe towards the access point.

### Home Assistant integration
- Wisp hub: finds nodes through ESPHome's discovery, subscribes to their link and hive reports, and adds per link a motion score sensor and a motion binary sensor (signal and spread as diagnostics, disabled by default). Nodes are subentries; works with Home Assistant 2026.3 and the per-entry devices of 2026.9.
- `wisp-map-card`: a parchment map of the nodes, access points and links, with footprints along a link while it sees motion.

### Tools
- `walk_test.py` (guided walk test per link), `csi_recorder.py` (live view), `csi_logger.py` (overnight recording), `replay.py` (the motion score offline).
