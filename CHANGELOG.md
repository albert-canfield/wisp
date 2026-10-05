# Changelog

## 0.1.0 (unreleased)
First working version: the grid, the hive and motion per link, with the Home Assistant integration and the map card.

### Nodes (`wisp-node`)
- Built on ESPHome (ESP-IDF): restart, safe mode, factory reset, firmware updates from Home Assistant, a fallback hotspot with a setup page, Improv over USB, a web page, health sensors. Builds for ESP32-S3, ESP32-C3 and the original ESP32.
- Self-forming ESP-NOW grid: every node sends a beacon in its own time slot (16 slots, ranked by MAC, aligned to the access point's clock), with no leader. Nodes move through active, quiet (1.5 s), missing (5 min) and forgotten (24 h) by themselves, so a node can be unplugged, rebooted or added at any time.
- Homes with several access points: every node moves to one grid channel (the channel of the lowest BSSID of the network heard well enough) and the best access point on it, remembered across reboots, never stranded on a dead one. ESPHome's own roaming is off.
- CSI from the access point (pings to the gateway, 20 a second) and from every other node (each beacon and relayed row), captured only from known transmitters.
- Motion per link: the shape of the signal across 51 subcarriers, a running spread, a quiet baseline that settles for 20 s and then learns quiet fast and noise slowly; a score where 1 means as quiet as usual, and motion with hysteresis (default threshold 2). The link to the access point is also on the node as "AP motion" and "AP motion score". The threshold is a setting on each node ("Motion threshold", 1.2 to 6), changed live from Home Assistant and kept across reboots.
- The hive: every node keeps the latest row of every node (relayed, so nodes out of range of each other still learn them), agrees on a hash, and solves the same relative layout of the nodes.
- UDP streams for subscribers: raw CSI (developer switch), link reports 5 times a second, hive reports every 5 s. See docs/PROTOCOL.md.
- The core task runs under the task watchdog. Optional Wi-Fi FTM probe towards the access point.

### Home Assistant integration
- Wisp hub: finds nodes through ESPHome's discovery, subscribes to their link and hive reports, and adds per link a motion score sensor and a motion binary sensor (signal and spread as diagnostics, disabled by default). Nodes are subentries; works with Home Assistant 2026.3 and the per-entry devices of 2026.9.
- `wisp-map-card`: a parchment map of the nodes, access points and links, with footprints along a link while it sees motion, and a pair of footprints where someone moves (the engine's best fit on the hive's layout, first version).
- Room presence (first version): calibrate rooms (Home Assistant areas) and the empty floor with services; a room sensor per floor and a presence sensor per room.
- Wisp panel in the sidebar (admins): the live map, nodes with their ESPHome device, rooms per floor with Calibrate, Stop and Clear, a countdown with instructions while recording (an empty floor gives 30 s to leave first), and the hive's status. Services: calibrate_room, calibrate_empty (optional delay), stop_calibration, clear_calibration.
- Floor plans and node placement: an image per floor with its size in metres, nodes and access points dragged onto it in the panel (touch and keyboard too), stored per hub. The hive's layout is fitted onto the placed nodes (least squares similarity: rotation, mirror, scale, shift), so unplaced nodes follow and every position on that floor, someone moving included, is in the plan's metres. The map card draws the plan under the drawing, with a scale bar, and takes a `floor` option for homes with several floors. Admin websocket commands `wisp/floor/set_plan`, `wisp/floor/place` and `wisp/floor/clear`.
- Position sensors: on a floor with a plan, x and y of someone moving in the plan's metres (from its top left corner), with the fit's quality as an attribute; unknown while nobody moves.

### Tools
- `walk_test.py` (guided walk test per link), `csi_recorder.py` (live view), `csi_logger.py` (overnight recording), `replay.py` (the motion score offline).
