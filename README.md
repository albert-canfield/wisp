<p align="center">
  <img src="https://raw.githubusercontent.com/albert-canfield/wisp/main/custom_components/wisp/brand/icon@2x.png" alt="Wisp: footprints walking across a parchment floor plan" width="160">
</p>

<h1 align="center">Wisp</h1>
<p align="center"><b>WiFi Spatial Presence for Home Assistant</b></p>

<p align="center">
See who is where in your home, room by room and as footprints moving across your floor plan, using only the WiFi signals between small ESP32 nodes. No cameras, no wearables, no phone to carry.
</p>

> **Status: early development.** There is nothing to install yet. The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and progress follows the roadmap below.

## How it works

People absorb and reflect WiFi. Wisp places three or four cheap ESP32 nodes around each floor. They find each other and form a grid on their own, take turns sending short pings to each other, and every node measures how the signal on each link changes (its channel state information, or CSI). When someone walks between two nodes, that link reacts.

- **Nodes** (`wisp-node` firmware) are the core: a lean, self-healing ESP-NOW grid that measures every link and sends a small disturbance score per link to Home Assistant over UDP several times a second. Nodes can be added or removed at any time.
- **The Wisp integration** is the brain. It combines every link on a floor, learns your rooms from a short calibration walk, and turns it all into presence per room and a position on the floor.
- **The map card** (`wisp-map-card`) shows the footprints moving across your own floor plan.

Everything stays on your local network.

## Planned entities

| What | Example |
|---|---|
| Room per floor | `sensor.wisp_floor_2_room` |
| Presence per room | `binary_sensor.wisp_office_presence` |
| Position per floor | `sensor.wisp_floor_1_x`, `sensor.wisp_floor_1_y` |
| Node health | signal, link quality, locked access point, firmware version |

## Hardware

- 3 to 4 cheap ESP32 boards per floor, powered by USB. ESP32-C3 and ESP32-S3 first.
- Your existing WiFi router or access point.
- Home Assistant 2026.3 or newer.

## Web flasher

Nodes are flashed from the browser, with nothing to install. Open the Wisp web flasher in Chrome or Edge, plug the board in by USB, click Install and enter your WiFi details. Home Assistant then discovers the node by itself. The flasher goes live with the first firmware release at [albert-canfield.github.io/wisp](https://albert-canfield.github.io/wisp).

## Roadmap

1. **Prove the signal.** Node firmware and an integration skeleton: per-link disturbance sensors that react when you walk between nodes.
2. **Room presence.** Floor plans and node placement, a calibration walk per room, presence per room with a confidence score.
3. **Position and map.** Tomographic imaging, a tracking filter that respects walls, and the map card.
4. **Ready for HACS.** OTA updates for nodes, diagnostics, automatic baseline and full documentation.

## License

MIT, see [LICENSE](LICENSE).
