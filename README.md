<p align="center">
  <img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/docs/images/hero.png" alt="Wisp: a parchment floor plan with ESP32 nodes, WiFi links and a trail of footprints walking from one room into the next" width="100%">
</p>

<p align="center">
  <a href="https://github.com/albert-canfield/wisp/releases"><img src="https://img.shields.io/github/v/release/albert-canfield/wisp?style=flat-square&color=8b5a2b" alt="Latest release"></a>
  <a href="https://hacs.xyz/docs/faq/custom_repositories/"><img src="https://img.shields.io/badge/HACS-custom%20repository-41BDF5?style=flat-square" alt="HACS custom repository"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2026.3%2B-18BCF2?style=flat-square&logo=homeassistant&logoColor=white" alt="Home Assistant 2026.3 or newer">
  <a href="https://github.com/albert-canfield/wisp/actions/workflows/validate.yml"><img src="https://img.shields.io/github/actions/workflow/status/albert-canfield/wisp/validate.yml?branch=main&style=flat-square&label=HACS%20%26%20hassfest" alt="HACS and hassfest validation"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/albert-canfield/wisp?style=flat-square&color=6b4f2a" alt="MIT licence"></a>
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=albert-canfield&repository=wisp&category=integration"><img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and add Wisp to HACS"></a>
</p>

**Wisp** (WiFi Spatial Presence) is a Marauder's Map for your home, with physics instead of magic. A handful of cheap ESP32 boards listen to the WiFi between each other and your router, and Home Assistant draws a living parchment map: who is in which room, and footprints where someone walks. No cameras, no wearables, no phone to carry, and nothing leaves your network.

> **Early release.** Wisp runs every day in its author's home (4 nodes, one floor, one access point). It works, it is measured, and it is still young: expect rough edges, and please [open an issue](https://github.com/albert-canfield/wisp/issues) when something looks wrong.

## Contents

[See it](#see-it) · [How it works](#how-it-works) · [How well it works](#how-well-it-works) · [What you need](#what-you-need) · [Install](#install) · [Set up your home](#set-up-your-home) · [What you get](#what-you-get) · [Tips](#tips-for-the-best-results) · [Limits and roadmap](#limits-and-roadmap) · [Documentation](#documentation)

## See it

<p align="center"><img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/docs/images/map-card.png" alt="The Wisp map card in light and dark: a floor plan with rooms, nodes, the access point, links that redden with motion and a trail of footprints" width="100%"></p>

<table>
  <tr>
    <td width="68%"><img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/docs/images/panel.png" alt="The Wisp panel: the map, rooms per floor with presence and calibration, nodes and the hive"></td>
    <td width="32%"><img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/docs/images/panel-phone.png" alt="The Wisp panel on a phone in dark mode"></td>
  </tr>
  <tr>
    <td><b>The Wisp panel.</b> Draw your floor plan, place the nodes, and teach Wisp your rooms with a short walk in each.</td>
    <td><b>On a phone,</b> light or dark: take it along on the calibration walk.</td>
  </tr>
</table>

<p align="center"><sub>Rendered from the real card and panel code with sample data.</sub></p>

## How it works

<p align="center"><img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/docs/images/how-it-works.png" alt="Three steps: nodes on each floor measure every WiFi link; the hive confirms motion; Home Assistant follows one person per floor and draws footprints on the plan" width="100%"></p>

People absorb and reflect WiFi. When you walk between two radios, the signal between them changes in a way a radio can measure: its channel state information (CSI): how strongly each of the dozens of subcarriers in a WiFi frame arrives.

1. **Nodes measure every link.** Three to six ESP32 boards per floor find each other and form a grid over ESP-NOW, with no leader and no setup. They take turns sending short beacons, ping your access point 20 times a second, and score how much each link moves against its own quiet baseline. Each link learns its own threshold from its quiet minutes (CFAR, as in radar), so a noisy link flags as rarely as a calm one.
2. **The hive confirms.** Every node keeps the same picture of the whole grid. A link counts as motion only when it moves both ways and a nearby node agrees: a body changes a link in both directions and the links around it, while one node's own noise does not. Nodes also look for breathing, the slow rise and fall of a chest, so someone sitting perfectly still is not lost.
3. **Home Assistant follows you.** The Wisp integration learns each room from a minute of walking in it, and follows one person per floor with a hidden Markov model: walking, busy or resting in a room, or off the floor. Nobody changes room without walking, nobody leaves the floor without walking out, and a room is shown only once Wisp is sure. On your floor plan, footprints follow the walker at walking speed.

Nodes are ESPHome devices running Wisp's own component (`wisp-node`), so updates, settings and health come from ESPHome as usual. Link data goes to Home Assistant over UDP on your local network. Your WiFi access point is part of the sensing and a fixed point on the map.

## How well it works

Measured in the author's home: 4 nodes and one access point on one floor, replayed second by second through the same code Home Assistant runs (`firmware/tools/study/`).

| What | Result |
|---|---|
| Room right, over a labelled evening (desk, play room, empty floor, walks) | **100%** of the seconds someone was there; 0.0% of all labelled seconds wrong (the rules it replaced: 12.5%) |
| Empty floor, 8 hours overnight | **Nobody shown** in any second |
| Sitting and working at a desk for 52 minutes | Held in the right room throughout |
| Walking recognised as walking | 84% of walking seconds (57% before the tracker) |
| Cost in Home Assistant | About 50 µs per floor per second |

One house and a few evenings of labels: treat these as a direction, not a promise. Your rooms, walls and node placement will differ, and the calibration walk is what makes Wisp fit your home.

## What you need

| | |
|---|---|
| **Nodes** | 3 or more ESP32 boards per floor, 4 to 6 for the best results, all the same model: ESP32-S3, ESP32-C3 or the original ESP32 (a few euros each). USB power. |
| **WiFi** | Your existing router or access points, on 2.4 GHz. Nothing to install on them. |
| **Home Assistant** | 2026.3 or newer, with [HACS](https://hacs.xyz) for the easiest install. |
| **A browser** | Chrome or Edge, to flash the nodes over USB from the web flasher. |

## Install

### 1. Flash the nodes

Open the **[Wisp web flasher](https://albert-canfield.github.io/wisp/)** in Chrome or Edge, plug a board in by USB, click Install and enter your WiFi details. Repeat for each node. (Prefer the command line? Build with ESPHome: see [docs/SETUP.md](docs/SETUP.md#2-flashing).)

Home Assistant then discovers each node under **ESPHome**. Add them: that gives you restarts, firmware updates and health for every node.

### 2. Install the integration

**With HACS (recommended):**

[![Open your Home Assistant instance and add Wisp to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=albert-canfield&repository=wisp&category=integration)

Or by hand in HACS: open the menu (⋮) and choose **Custom repositories**, add `https://github.com/albert-canfield/wisp` with the type **Integration**, then find **Wisp**, download it and restart Home Assistant.

**Manually:** download the source of the [latest release](https://github.com/albert-canfield/wisp/releases/latest), copy `custom_components/wisp` into your Home Assistant `config/custom_components` folder and restart.

Wisp finds your nodes by itself: confirm the first one under **Settings > Devices & services**, and the others join automatically.

## Set up your home

Everything happens in the **Wisp** panel in the sidebar, and works on a phone too.

1. **Give each node its room.** Nodes take their floor from the Home Assistant area you put them in.
2. **Add a floor plan.** Draw it on a grid in metres (rooms as rectangles), or upload an image of your plan. Then drag each node, and your access point, to where it stands.
3. **Mark the ways out.** Tick the rooms with stairs or a door outside (**Way off the floor**). Rooms you leave undrawn count as the hallway.
4. **Teach Wisp your rooms.** Tap **Calibrate** in a room and walk around in it until the countdown ends (about a minute). For rooms where people sit, add a **still** calibration at the desk or sofa. Then **Calibrate empty floor** with nobody on the floor: Wisp gives you 30 seconds to leave first.
5. **Add the map to a dashboard:** the card **Wisp map** is in the card picker.

## What you get

| Entity | What it tells you | Example |
|---|---|---|
| Room, per floor | The room the floor's person is in, or `none`; with its confidence and whether they walk | `sensor.wisp_ground_floor_room` |
| Presence, per room | Occupancy for automations, with `still` while someone sits there | `binary_sensor.wisp_office_presence` |
| Position, per floor with a plan | x and y in metres on your plan, while someone moves | `sensor.wisp_ground_floor_position_x` |
| Motion, per link | A motion score and on/off for every link (disabled by default) | `sensor.hall_ap_..._motion_score` |
| Each node (ESPHome) | Motion confirmed by the hive, breathing (experimental), grid size, hive in sync, motion threshold, firmware update | `binary_sensor.wisp_a8c77c_motion` |
| The hub | Nodes online, hive in sync, calibration progress | `sensor.wisp_nodes_online` |

The map card in YAML:

```yaml
type: custom:wisp-map-card
floor: Ground floor  # optional: floor id or name; the first floor with nodes by default
rotate: 0            # optional: 0, 90, 180 or 270 degrees
flip: false          # optional: mirror left to right
labels: true         # optional: names of nodes and access points
lines: true          # optional: the links
plan_photo: false    # optional: the plan is a photo (dimmed, not inverted in dark mode)
```

A home without floors in Home Assistant is one floor, shown as **Areas**: a flat with one router works as well as a house with several floors and access points.

## Tips for the best results

- **Spread the nodes out** near walls and corners, about 1 to 1.5 m high, so the lines between them cross the places you care about: doorways, the desk, the sofa. Two nodes side by side see almost nothing.
- **Calibrate the hallway walk with the first steps of the stairs** and the space by the front door, so leaving looks like leaving.
- **Keep your access point steady:** one fixed 2.4 GHz channel (1, 6 or 11), 20 MHz width, fixed transmit power, no automatic channel or power optimisation. Settings for TP-Link Omada, Ubiquiti UniFi and consumer routers are in [docs/SETUP.md](docs/SETUP.md#5-access-points).
- **Breathing detection** is a switch on each node (experimental, off by default). Turned on, it keeps someone reading or watching TV without moving in their room.
- **Recalibrate after moving or adding a node:** new links count once the rooms have samples of them.

## Limits and roadmap

- **One person per floor.** Two people on one floor are followed as one. More than one person is the next big step.
- **A walk is what places you.** After a restart, Wisp remembers who was where for 10 minutes; beyond that, someone already sitting is found once they move.
- **Breathing is experimental,** measured on a handful of sittings so far.

Next: several people per floor, walls and doors in the tracking, and calibration from everyday walks. The design and every decision behind it are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Privacy

Wisp sees disturbances in radio links, not images or sound. It cannot tell who someone is. Link data stays on your local network between the nodes and Home Assistant, and nothing is sent to any cloud.

## Documentation

- [Setup guide](docs/SETUP.md): boards, flashing, node placement, access point settings, checking that it works, troubleshooting.
- [Architecture](docs/ARCHITECTURE.md): the firmware, the hive, the tracker and the map, with the measurements behind each choice.
- [Protocol](docs/PROTOCOL.md): the UDP wire format between nodes and Home Assistant.
- [Tuning](docs/TUNING.md): how the motion and breathing defaults were chosen.
- [Changelog](CHANGELOG.md).
- [Tools](firmware/tools): a guided walk test per link, live CSI recorder, overnight logger, and replays of the motion score, positions and room presence.

## Licence

MIT, see [LICENSE](LICENSE). Made by [Albert Canfield](https://github.com/albert-canfield).
