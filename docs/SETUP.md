# Setting up Wisp

A practical guide: boards, flashing, Home Assistant, where to put the nodes, and how to check that it works. The design behind it is in [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Boards

- **How many:** at least 3 per floor; 4 to 6 give clearly better positions. Use the same model for every node.
- **Which:** ESP32-S3, ESP32-C3 or the original ESP32. Boards with a decent printed or external antenna give steadier readings than the tiniest ones with a ceramic chip antenna.
- **Power:** a good USB supply. Brownouts (a weak charger or cable) show up as "Reset reason: brownout" and reboots.

## 2. Flashing

Until the web flasher is online (first release), build with ESPHome:

```bash
python3.13 -m venv .venv && .venv/bin/pip install esphome
.venv/bin/esphome run firmware/wisp-node-esp32s3.yaml   # or wisp-node-esp32c3.yaml, wisp-node-esp32.yaml
```

- **Two-port S3 boards** (USB and COM): either port can flash. Logs and Improv use the native USB port; `firmware/wisp-node-esp32s3-com.yaml` moves them to the COM port.
- **A board that ran other firmware before:** if it does not start, erase it first: `.venv/bin/esptool --port <port> erase-flash`.
- **After an update,** give a node a minute before unplugging it: ESPHome confirms a new firmware only after 60 seconds, and a reset before that rolls it back.

## 3. WiFi and Home Assistant

1. A new node opens a hotspot named `wisp-xxxxxx` (the last six digits of its MAC). Join it from a phone, pick your network and enter its password.
2. Home Assistant discovers the node under ESPHome. Add it: restart, updates, settings and health appear there.
3. Install the Wisp integration: copy `custom_components/wisp` to Home Assistant's `config/custom_components` (or add this repository to HACS as a custom integration repository) and restart Home Assistant. Wisp discovers the nodes by itself; confirm the first one, the others are added automatically.
4. Open the **Wisp** panel: in the sidebar, or with Open on the Wisp hub device (Settings > Devices & services > Wisp). Floor plans, node placement and room calibration are all there. The hub device also has Nodes online and Hive in sync, to see at a glance (or automate on) whether every node reports.
5. Add the map to a dashboard if you like: a card of type `custom:wisp-map-card`.

## 4. Where to put the nodes

Wisp sees people who cross the straight lines between nodes, and between nodes and your access points. So:

- **Spread them out** around the room or floor, near the walls or corners, so their lines cross the areas you care about (doorways, the sofa, the desk).
- **Height about 1 to 1.5 m** (a shelf or a sideboard): lines then cross people at chest height.
- **Away from metal and big screens,** which reflect and block the signal.
- **Not in a cluster.** Two nodes side by side see almost nothing between them.
- **Keep them still.** A node that moves needs its place updated (the hive notices big changes).

Then mark them on a floor plan, so the map and positions match your home:

1. Put an image of each floor seen from above in Home Assistant's `www` folder, for example `config/www/wisp/ground.png` (it is then `/local/wisp/ground.png`). Any web address works too. A plain drawing on white looks best.
2. In the Wisp panel, under the floor, tap Add floor plan, give the address and the floor's width in metres (measure one wall; the height follows the image).
3. Drag each node to where it stands, and the access points you know, then Save. Two placed nodes are enough for the rest to follow; a third one, away from the line between the first two, tells Wisp which way round its layout goes.

## 5. Access points

Wisp works with any access point, and uses it as an extra fixed point. Each node pings the access point it joins 20 times a second, and the replies carry the signal detail (CSI) for that link. A node only uses its own access point, so its motion score depends on that access point transmitting steadily.

Nodes reach each other on one 2.4 GHz channel, the grid channel, and every node picks it with the same rule: the channel of the lowest BSSID (radio MAC address) of your network heard at -80 dBm or better. Each node then joins the strongest access point on that channel. With every access point on the same channel, each node joins its nearest one; with access points on different channels, all nodes crowd onto the access points of one channel. Nodes choose their access point themselves (ESPHome's roaming is off), so an access point that kicks weak clients or balances load can drop a node.

These 2.4 GHz settings help, none is required:

| Setting | Why |
|---|---|
| The same channel on every access point: 1, 6 or 11, the quietest one | Each node then joins its nearest access point; nodes follow the channel by themselves |
| 20 MHz channel width | 40 MHz overlaps the other 2.4 GHz channels and can fall back to 20 MHz on its own |
| Fixed transmit power, the same on every access point, never Auto (lower it if access points are close) | Power changes look like motion; with equal power the strongest access point is the nearest |
| No automatic channel or power optimisation | A channel change moves the whole grid; a power change looks like motion |
| No minimum RSSI, load balancing or client limit on the nodes' network | A kicked node stops sensing until it reconnects |
| Optional: no 802.11b (CCK) rates, 2.4 GHz minimum rate 6 Mbps | Beacons then carry usable CSI too; ping replies already do |

OFDMA, band steering and the wireless mode do not matter for the nodes, as long as their network keeps 2.4 GHz with 802.11n: ESP32 boards only use 2.4 GHz, up to 802.11n. Without 802.11b rates, only devices that support nothing newer lose access, and those are rare today.

The trade-off: access points sharing one 2.4 GHz channel share its airtime. Most phones and laptops use 5 or 6 GHz anyway, so the cost is usually small.

### TP-Link Omada

- **Channel, width and power:** Devices > the access point > Config > Radios (Wireless on some versions): 2.4 GHz channel, Channel Width 20 MHz, Tx Power Custom. Repeat on every access point.
- **Optimisation:** in the site settings, WLAN Optimization off.
- **802.11b rates:** edit the SSID, Advanced Settings > 802.11 Rate Control: 2.4 GHz minimum rate 6 Mbps.
- **Kicks and limits:** RSSI Threshold off, and Maximum Associated Clients off on each access point.

### Ubiquiti UniFi

- **Channel, width and power:** Radio Manager, or each access point's radio settings: 2.4 GHz channel, Channel Width 20 MHz (HT20), Transmit Power Custom.
- **Optimisation:** Nightly Channel Optimization off, and Auto-Optimize Network off.
- **Kicks:** Minimum RSSI off on the 2.4 GHz radios.
- **802.11b rates:** in the WiFi network's settings, Legacy Support off, or a 2.4 GHz minimum data rate of 6 Mbps.
- Band Steering does not matter.

### Consumer routers and mesh systems

- Pick a fixed 2.4 GHz channel (1, 6 or 11) instead of Auto, and 20 MHz width. If transmit power has a setting, pick a fixed level.
- Smart Connect (one network name for both bands) can stay on; turn it off only if it leaves the nodes without 2.4 GHz.
- Some mesh systems (Eero, Deco, Orbi) choose their channels themselves and cannot be fixed. Wisp still works: nodes follow a channel change by themselves, with a short pause while they move.

## 6. Checking that it works

On each node's page (or in Home Assistant):

| Entity | Expect |
|---|---|
| Grid nodes | the number of nodes on this channel |
| Hive in sync | on, a few seconds after any change |
| Grid channel | the same on every node of a floor |
| AP CSI rate | about 20 to 30 Hz |
| CSI dropped | 0, or slowly growing at most |
| AP motion score | about 1 in a still room, higher when someone moves |

Then run the guided walk test from a computer on the same network:

```bash
.venv/bin/python firmware/tools/walk_test.py wisp-xxxxxx.local wisp-yyyyyy.local
```

It asks you to stand still, walk across links and leave the room, and prints for every link how strongly and how often it saw you. "still" and "away" should sit near 1 with no motion, "across" clearly higher.

## 7. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Grid nodes stays at 1 | Nodes on different channels: check Grid channel on each; with several access points they settle within a minute |
| Hive in sync flickers | A node keeps dropping out: check its WiFi signal and power supply |
| AP CSI rate 0 | The node is not connected, or the gateway does not answer pings |
| Motion with nobody around | Pets, fans, curtains in a draft, or a washing machine on the line; or raise the node's Motion threshold (a setting on its device page) |
| A node is "unavailable" in Home Assistant | It is offline or rebooting; the grid keeps working without it |
| "Few Wisp nodes on ..." in Settings > Repairs | That floor has fewer than 3 nodes: motion per link works, rooms and positions need 3 or more. It goes once the floor has them (a node's floor is the floor of its area) |

## 8. Privacy and your network

Everything stays in your home network: nodes talk to Home Assistant directly, with no cloud. Still, motion data says when and where someone moves at home, so it is worth knowing who on the network can read it:

| What | Who can read it | To limit it |
|---|---|---|
| ESPHome API (Home Assistant) | Whoever has the node's API key | Keep the encryption key ESPHome sets up when adopting a node |
| The node's web page | Anyone on the network | `web_server: auth: {username: ..., password: ...}` in the node's YAML |
| Wisp's link reports (UDP port 47010) | Any device on the network that asks: motion score and flag per link, 5 times a second | A network only trusted devices use, for example a VLAN for Home Assistant and its devices |
| Raw CSI stream | The same, while its switch is on (off by default) | Leave it off unless you record data |
| Fallback hotspot | Anyone nearby, while the node has no WiFi | `wifi: ap: {password: ...}` in the node's YAML |

The link reports carry no key yet: adding one is on the list for a later version.
