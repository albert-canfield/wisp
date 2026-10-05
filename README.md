<p align="center">
  <img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/custom_components/wisp/brand/icon@2x.png" alt="Wisp: footprints walking across a parchment floor plan" width="160">
</p>

<h1 align="center">Wisp</h1>
<p align="center"><b>WiFi Spatial Presence for Home Assistant</b></p>

<p align="center">
See who is where in your home, room by room and as footprints moving across your floor plan, using only the WiFi signals between small ESP32 nodes. No cameras, no wearables, no phone to carry.
</p>

> **Status: early development, phase 1 working.** Nodes form their grid, measure every link and report motion per link to Home Assistant. Rooms and positions come next. The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), the wire format in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## See it

<p align="center"><img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/docs/images/map-card.png" alt="The Wisp map card in light and dark: nodes, an access point, links that redden with motion, footprints walking along them and a mark where someone moves" width="100%"></p>

<table>
  <tr>
    <td width="70%"><img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/docs/images/panel.png" alt="The Wisp panel: recording banners with countdowns, the map, and rooms per floor with Calibrate and Clear"></td>
    <td width="30%"><img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/docs/images/panel-phone.png" alt="The Wisp panel on a phone in dark mode"></td>
  </tr>
  <tr>
    <td><b>The Wisp panel.</b> Teach it your rooms: tap Calibrate and walk around until the countdown ends.</td>
    <td><b>On a phone,</b> light or dark.</td>
  </tr>
</table>

<p align="center"><i>Rendered from the real card and panel code with sample data.</i></p>

## How it works

People absorb and reflect WiFi. Wisp places three or four cheap ESP32 nodes around each floor. They find each other and form a grid on their own, take turns sending short pings to each other and to your WiFi access point, and every node measures how the signal on each link changes (its channel state information, or CSI). The access point is a fixed point on your floor plan, so it adds links and precision. When someone walks between two nodes, that link reacts.

- **Nodes** (`wisp-node` firmware, built on ESPHome) are the core: a lean, self-healing ESP-NOW grid that measures every link and sends a small disturbance score per link to Home Assistant over UDP several times a second. Nodes can be added or removed at any time; the grid adapts on its own.
- **The Wisp integration** is the brain. It combines every link on a floor, learns your rooms from a short calibration walk, and turns it all into presence per room and a position on the floor.
- **The map card** (`wisp-map-card`) shows the footprints moving across your own floor plan.

Everything stays on your local network.

## What works today

- **Self-forming grid.** Nodes find each other over ESP-NOW, take time slots without a leader, and heal by themselves when a node reboots, leaves or joins.
- **Homes with several access points.** Nodes agree on one channel and join the best access point on it, so the grid stays together.
- **The hive.** Every node keeps the same picture of the whole grid and works out the same layout of the nodes.
- **Motion per link.** Each link from an access point or another node gets a motion score (1 means as quiet as usual) and a motion on/off state.
- **Home Assistant.** Nodes are ESPHome devices (restart, updates, settings, health); the Wisp integration adds the link sensors.

## Entities

| From | What | Example |
|---|---|---|
| Each node (ESPHome) | Motion on the link to its access point, grid size, hive in sync, AP CSI rate, motion threshold, restart, safe mode, identify, firmware update | `binary_sensor.wisp_a8c77c_ap_motion` |
| Wisp integration | Motion score and motion per link, signal and spread (disabled by default) | `sensor.hall_ap_58_04_4f_1d_12_f9_motion_score` |
| Wisp integration, once calibrated | Room per floor, presence per room, calibration progress per floor | `sensor.wisp_floor_2_room`, `binary_sensor.wisp_office_presence` |
| Wisp integration, on a floor with a plan | Position x and y of someone moving, in the plan's metres, with the fit's quality | `sensor.wisp_floor_2_position_x` |

## Try it

The full guide, including where to put the nodes and recommended access point settings, is in [docs/SETUP.md](docs/SETUP.md); the measurements behind the motion defaults are in [docs/TUNING.md](docs/TUNING.md). In short, until the first release puts the web flasher online, nodes are built with ESPHome:

```bash
python3.13 -m venv .venv && .venv/bin/pip install esphome
.venv/bin/esphome run firmware/wisp-node-esp32s3.yaml   # or wisp-node-esp32c3.yaml, wisp-node-esp32.yaml
```

1. Join the node's hotspot `wisp-xxxxxx` from a phone and pick your WiFi.
2. Add the node in Home Assistant under ESPHome when it is discovered.
3. Copy `custom_components/wisp` into Home Assistant's `config/custom_components` (or add this repository to HACS as a custom integration repository) and restart. Wisp finds the nodes by itself.

Tools are in [firmware/tools](firmware/tools): a guided walk test that reports how well each link sees you (`walk_test.py`), a live recorder with a plot, an overnight logger, and replays of the motion score (`replay.py`) and of positions (`replay_floor.py`) from its recordings.

## Hardware

- At least 3 cheap ESP32 boards per floor, powered by USB. More boards give better precision. ESP32-S3, ESP32-C3 and the original ESP32 are supported; the original ESP32 cannot range with Wi-Fi FTM.
- Your existing WiFi router or access point, which also takes part in the sensing.
- Home Assistant 2026.3 or newer.

## Web flasher

Nodes are flashed from the browser, with nothing to install. Open the Wisp web flasher in Chrome or Edge, plug the board in by USB, click Install and enter your WiFi details. Home Assistant then discovers the node by itself. The flasher goes live with the first firmware release at [albert-canfield.github.io/wisp](https://albert-canfield.github.io/wisp).

## Map card

The integration brings its own dashboard card and loads it by itself. Add it from the card picker ("Wisp map") or in YAML:

```yaml
type: custom:wisp-map-card
title: Wisp     # optional
floor: Upstairs # optional, floor id or name; the first floor with nodes by default
rotate: 0       # optional, degrees clockwise (not on a floor plan)
flip: false     # optional, mirror left to right (not on a floor plan)
plan_photo: false # optional, the floor plan is a photo: dimmed, not inverted, in dark mode
```

Nodes sit where the grid's own layout puts them, access points beside the nodes that hear them best, and footprints walk along a link while it sees motion. Where someone moves, a pair of footprints stands at the best guess of the spot (it needs three or more nodes to be meaningful). With several floors, a card shows one floor; add a card per floor.

Without a floor plan the layout is relative, so turn and mirror it to match your home. With one (set in the Wisp panel, below), the card draws the plan's image inked onto the parchment, at its size in metres, with a scale bar, and the nodes, access points, links and footprints where they are on it. Nodes you have not placed follow the placed ones and are drawn dashed.

## Room presence (first version)

Rooms are your Home Assistant areas and floors are your Home Assistant floors. Give each node the area it stands in (Wisp, the node, Change node): its floor is the node's floor, and nodes without one share a floor named after the hub. Then teach Wisp your rooms, one at a time, with the actions under Developer tools:

1. `wisp.calibrate_room` with the area, standing in that room: walk around in it until the floor's calibration sensor is idle again (60 seconds by default). A room needs 20 seconds of movement before it counts.
2. `wisp.calibrate_empty` with nobody moving on the floor (optional, it helps against a fan or an access point that changes power).
3. `wisp.clear_calibration` forgets a room, or everything.

Each floor then gets a room sensor (the room someone moves in, `none` while nobody moves, with a confidence) and each calibrated room an occupancy sensor that stays on for 60 seconds after its last movement, since someone sitting still makes little signal. The hold time is in the Wisp options. Calibrate again after adding nodes: new links only count once the rooms have samples of them.

## Wisp panel

The integration adds a Wisp page to the sidebar for administrators, so calibration needs no Developer tools. It shows, live:

- **The map**, the same as the card, a button per floor when there are several, and without a floor plan buttons to turn and mirror it (remembered per browser).
- **Rooms per floor:** every area with a node or a calibration, its samples and whether it is occupied, and the room someone moves in now. Calibrate starts `wisp.calibrate_room` (60 seconds by default) and counts down with what to do; Calibrate empty floor gives 30 seconds to leave the floor before it records (`wisp.calibrate_empty` with `delay`); Clear asks first. Areas on a floor without a node can be picked and calibrated too.
- **A floor plan per floor:** Add floor plan takes an image address and the floor's width in metres (the height follows the image's proportions, or give it). Place nodes then shows the plan: drag each node, and the access points you know, to where it stands (by touch too, or with the arrow keys), then Save. Nodes you leave are fitted to the placed ones: from two placed nodes Wisp turns, scales and if needed mirrors its own layout to match, and a third one away from the line between the first two settles the mirror. Change sets another image or size and keeps the positions; Remove asks first.
- **Nodes:** name, area, floor, online, whether the hive has placed it, whether it is placed on its floor's plan, and a link to its ESPHome device.
- **The hive:** hash, in sync, nodes and when it was last heard.

It works on a phone: take it along on the calibration walk, the screen stays on while a recording counts down. Nodes without a floor share one named after the hub, and `wisp.calibrate_empty` takes that name as its floor.

Floor plan images are best kept in Home Assistant's `www` folder: `config/www/wisp/ground.png` is `/local/wisp/ground.png`. A plain drawing with a white background looks best: white turns to parchment, and in dark mode the drawing is inverted. Once a floor has a plan, every position on it, including where someone moves, is in the plan's metres from its top left corner.

## Roadmap

1. **Prove the signal (done).** Node firmware with the self-forming grid, the hive and per-link motion, the integration with link sensors, CI and the release pipeline.
2. **Room presence (first version in).** Floor plans and node placement, calibration per room, presence per room. Next: a confidence score per room and calibration from a walk.
3. **Position and map (first version in).** A best fit per floor on the plan, a tracking filter, the map card and position sensors. Next: tomographic imaging on more nodes, walls in the tracking filter, more than one person.
4. **Ready for HACS.** The first release (web flasher and node updates from GitHub Pages), measured accuracy and full documentation.

## License

MIT, see [LICENSE](LICENSE).
