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
4. Add the map: a dashboard card of type `custom:wisp-map-card`.

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

Wisp works with any access point, and uses it as an extra fixed point. Homes with several access points work too: nodes agree on one channel by themselves and join the best access point on it.

These settings help, none is required (names from TP-Link Omada):

| Setting | Why |
|---|---|
| The same 2.4 GHz channel on every access point (1, 6 or 11, 20 MHz) | Every access point then becomes a live sensing point for the whole grid |
| Fixed 2.4 GHz transmit power, not Auto | Automatic power changes look like people moving |
| Disable 802.11b (CCK) rates (Omada: WLAN > SSID > 802.11 Rate Control) | Beacons then carry the signal detail Wisp uses |
| No automatic channel optimisation on 2.4 GHz | Each channel change forces the grid to move |
| No minimum-signal kick or load balancing for the nodes' network | Stops access points from dropping a weak node |

2.4 GHz must be on for the network the nodes use: ESP32 boards only use 2.4 GHz.

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
| Motion with nobody around | Pets, fans, curtains in a draft, or a washing machine on the line; or raise the motion threshold |
| A node is "unavailable" in Home Assistant | It is offline or rebooting; the grid keeps working without it |
