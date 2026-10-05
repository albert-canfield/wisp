# Wisp architecture

Decisions taken before the first line of code. Sections marked **proposed** are designs still to be agreed; open items are listed at the end.

## Goal

Room presence and an x/y position per floor, from the WiFi links between cheap ESP32 nodes, inside Home Assistant. Local only, installable from HACS, nodes flashed from the browser.

**The firmware is the core.** It must be as light and fast as possible, run on the cheapest ESP32 boards, and form a robust grid over ESP-NOW by itself, with no pairing and no leader. Nodes join, leave and heal on their own, without Home Assistant.

- **At least 3 nodes per floor.** More nodes give more links and better precision.
- **The access point takes part.** Nodes capture CSI from the WiFi access point as well as from each other. The AP is a fixed point on the floor plan, so it adds links and makes positions more precise.
- **Built on ESPHome.** Home Assistant restarts, updates and configures nodes through the ESPHome API and OTA, like any ESPHome device. Wisp's core runs inside as an ESPHome external component.

## Overview

```mermaid
flowchart LR
  AP[(WiFi access point)]
  subgraph Floor["Each floor: 3 or more nodes, self-forming ESP-NOW grid"]
    N1[wisp-node] <-- ESP-NOW pings --> N2[wisp-node]
    N2 <--> N3[wisp-node]
    N1 <--> N3
  end
  AP -. "CSI from ping replies, shared clock, fixed anchor" .-> Floor
  Floor -- "UDP: per-link scores, 5 to 10 Hz" --> L
  Floor -- "ESPHome API: restart, OTA, health, settings" --> EH
  subgraph HA["Home Assistant"]
    EH[ESPHome integration]
    L[Wisp: UDP listener] --> E[engine] --> S[entities]
    E --> UI[map card and calibration panel]
  end
```

## Where the processing happens

Sensing on each node, fusion in one central place.

| On each node (the core) | Central (the engine) |
|---|---|
| Join the grid, keep its slot, share the hive knowledge and layout | Combine every link of a floor at once |
| Capture CSI from the other nodes and the AP | Floor plan, node positions, link geometry |
| Per-link disturbance score and baseline | Calibration and training data |
| Optional local motion yes/no | Tracking, room snapping, entities, UI |
| Shrink everything to tiny packets | |

Fusing links into a position needs all links together, plus the map and calibration data. A node acting as "leader" could do it, but calibration, UI and updates would become painful.

## Options considered

| | A. Nodes + HA integration | B. Nodes + Docker + MQTT + HA | C. Brain on the nodes only |
|---|---|---|---|
| Moving parts | Fewest | Most | Few, but complex firmware |
| Setup for users | HACS install, auto-discovery | Docker, MQTT and HA | Flash and go, limited setup |
| Map and calibration UI | HA panel and card | Own web UI | Very hard |
| Compute headroom | Fine for this maths | Most (heavy ML) | Limited |
| HA restart | Sensing pauses | Keeps running | No impact |
| Works without HA | No | Yes | Yes |
| Reusable via HACS | Yes | Partly | No |

**Decision: A, built to allow B.** The engine is a pure Python library with no Home Assistant imports, so the same code can later run as a Docker service with MQTT for non-HA users or heavier ML.

## Node firmware (`wisp-node`)

Built on [ESPHome](https://esphome.io) with the ESP-IDF framework. ESPHome handles everything around the core; Wisp's own code is an ESPHome external component (`wisp`) in C++.

| From ESPHome | Wisp's core (external component) |
|---|---|
| WiFi, fallback hotspot and captive portal | Grid formation, slots and node lifecycle |
| Native API to Home Assistant: entities, restart, settings | ESP-NOW pings and the hive |
| OTA from Home Assistant, the ESPHome dashboard or a manifest on GitHub Pages | CSI capture from nodes and APs, AP grouping |
| Web server, Improv Serial (and Improv over Bluetooth) | Per-link scores and baseline |
| Safe mode, status LED, logger, mDNS | UDP link stream to the Wisp integration |

- Any cheap ESP32 with WiFi CSI. Builds for ESP32-S3, ESP32-C3 and the original ESP32; others may follow. First test nodes: two ESP32-S3 N16R8 (16 MB flash, 8 MB PSRAM).
- CSI from the other nodes (ESP-NOW) and from the access points (ping replies and beacons), each AP told apart by its BSSID.
- BSSID lock to its home access point.

**Why ESPHome.** Home Assistant already discovers, restarts, updates and configures ESPHome devices, and users know the flow. It costs some flash and RAM, but saves writing and maintaining WiFi handling, OTA, an API, a web server and the fallback hotspot. The core stays lean inside it.

### Lean by design

- The core runs in its own FreeRTOS task, not in ESPHome's main loop, so slot timing does not depend on what else ESPHome is doing.
- The CSI callback only copies the subcarriers it needs into a ring buffer and returns.
- A low-priority task turns them into amplitudes (integer maths), a running baseline and a disturbance score per link.
- One UDP packet per round carries all of a node's links, a few bytes each.
- Link data goes over UDP, not the ESPHome API: 10 updates a second for every link would flood Home Assistant's state machine and recorder. The API only carries slow things: health, settings and buttons.
- Budget: ESPHome plus the core comfortable on a single-core ESP32-C3.

### Grid formation (proposed)

The grid forms and heals itself. Nothing to pair, no master node.

- **One channel.** ESP-NOW only works between nodes on the same channel, so the grid has one 2.4 GHz channel. Every node joins an access point on that channel (with several APs, not necessarily the same one).
- **Discovery.** Each node broadcasts a tiny ESP-NOW beacon: node id, chip, firmware version, health. The nodes heard recently are the members; a node silent for a few rounds drops out.
- **Shared clock without a leader.** An access point's beacon timer (TSF) is the same on every node that hears it. With several APs each has its own, so the hive picks one clock AP: the one heard by every node, lowest BSSID on a tie. Time slots come straight from it: a fixed round (for example 100 ms), and each node's slot is its rank in the member list sorted by MAC. If two nodes briefly disagree on the list, the worst case is a collision that the radio's own carrier sense and the next round absorb.
- **One broadcast per node per round.** Every other node captures CSI from it, so N broadcasts measure N × (N - 1) directed links. Three nodes at 10 Hz is 30 small frames a second for 6 node links, plus 3 access point links (below).
- **Self-healing.** Nodes join, leave and recover on their own; see the node lifecycle and self-healing below.

### The access point as a fixed node (proposed)

The access point is a free extra point in the grid: always on, never moves, and its place on the floor plan is marked once. Three nodes plus the AP make four points and six lines across the floor instead of three.

- **Its signal.** In its own slot, each node pings the router. The reply comes from the locked access point and carries CSI for the AP to node link, the method Espressif's own `csi_recv_router` example uses. Beacons alone are not enough: they come only about 10 times a second, and many access points send them at the slowest legacy rate, which carries little or no CSI.
- **Optional extra.** Frames the AP sends to other devices can be captured too (promiscuous mode, filtered by its BSSID). One such frame measures every AP link at the same instant.
- **In the hive.** The AP is a column without a row: every node reports what it hears from the AP, the AP reports nothing. Ranks are taken per sender, so the AP's different transmit power does not matter.
- **Better anchoring.** With the AP's position known, placing one node (plus a mirror flip) fixes the whole layout, instead of two.
- **Distance in metres.** If the access point is an FTM responder (802.11mc; some recent routers are), nodes range to it directly, the strongest anchor there is.
- **AP on another floor.** It still gives the clock and the connection, but its links say less about position on this floor, so the engine weighs them down.

**Why it matters most on small grids.** N nodes have N × (N - 1) / 2 lines between them; the AP adds N more, all fanning out from one fixed point at new angles.

| Nodes | Lines without AP | Lines with AP | Gain |
|---|---|---|---|
| 3 | 3 | 6 | double |
| 4 | 6 | 10 | +67% |
| 5 | 10 | 15 | +50% |
| 6 | 15 | 21 | +40% |

**More from the router connection.**

- **Clean snapshots.** One frame from the AP reaches every node at the same instant, so all AP links are measured together, with no slot timing between them.
- **Drift reference.** The AP is mains powered and never moves. When every AP link drifts the same way at once (temperature, humidity, a door left open), that is the environment, not a person, and the engine corrects the baseline.
- **The AP's own view.** Many routers already report the signal they receive from each client (UniFi, OpenWrt and Fritz!Box all have Home Assistant integrations that expose it). That fills in the AP's missing row, slowly but for free.
- **Later: other fixed WiFi devices.** A smart TV, a speaker or a smart plug talks to the AP on the same channel all day. Nodes can capture CSI from their frames too (promiscuous mode, filtered by MAC), turning each fixed device the user picks into another free transmitter with more lines. Only devices the user selects, never phones.

**Caveats.** Modern access points change data rate and transmit power, and beamform towards the clients they serve, so CSI from the AP can change without anyone moving. The engine treats AP links as their own class with their own baseline, and prefers the replies to each node's own pings (an ESP32 does not ask for beamforming) over frames meant for other devices.

### Several access points (proposed)

Many homes have more than one access point under the same WiFi name: a mesh system, or several APs on one controller. Each AP radio has its own MAC address (BSSID), so nodes can tell them apart, and every AP becomes another fixed point.

**Built so far:** the grid channel (below), and each node's own AP as its CSI source, from the replies to its pings. **Planned:** the rest of this list.

- **Telling APs apart.** One physical AP often has several BSSIDs on the same radio (main, guest and IoT networks). Nodes group BSSIDs into one AP when they share the channel, their MACs differ only in the last digits or the "locally administered" bit, and every node hears them at the same strength. Each physical AP gets an id in the hive (AP1, AP2, AP3).
- **Recognising them in Home Assistant.** Each AP is listed with its BSSIDs and the node that hears it best, so the user can tell which is which and mark it on the floor plan once.
- **Home AP per node.** Each node locks to the strongest AP on the grid channel. The hive spreads the nodes' home APs across the APs, so every AP sends regular ping replies.
- **Overhearing.** Ping replies from AP2 to node B also reach nodes A and C (promiscuous mode, filtered by BSSID). So every AP on the grid channel is a live CSI source for every node, not only for the nodes it serves.
- **Signal from all of them.** Every node reports every AP it hears in its hive row. Beacons give signal strength from all APs, and CSI too when the APs send them at OFDM rates (see the settings below): then every AP is a CSI source about 10 times a second per network name, with no pings at all.
- **Full anchoring.** With three APs marked on the floor plan, the layout is fixed with no node dragging and no mirror flip, and new nodes place themselves.
- **Roaming.** Mesh systems push clients to "better" APs. Nodes ignore steering requests and keep their lock. If an AP keeps disconnecting a node, the hive gives the node another home AP on the grid channel.
- **APs come and go too.** An AP rebooting, added or removed goes through the same lifecycle as a node: new, missing, forgotten.

**Grid channel (built).** Every node applies one rule to the APs of its own network that it heard while connecting: the grid channel is the channel of the lowest BSSID heard at -80 dBm or better, and the node joins the strongest AP on that channel. The choice is saved, and that AP gets a one-step priority boost in ESPHome's AP ranking, so later boots join it directly; one failed attempt puts it level with the others again, so a dead AP never strands a node. A node that lands on another channel disconnects once and lets ESPHome reconnect with the boost (at most three times per boot). ESPHome's own roaming is off. Each node's "Fixed grid channel" setting (0 automatic, 1 to 13) overrides the rule at once, without new firmware: the Wisp panel sets it for all nodes of a floor, so a home with an access point per floor can keep each floor's grid on its own access point and channel (nodes otherwise pick up the access point of another floor and sense across floors). `grid_channel` in the YAML gives the setting's first value. In the owner's home (APs on 1, 6 and 11) both test nodes moved to channel 1 and formed a grid.

**Same channel or not.** Best results come when every AP's 2.4 GHz radio uses the same channel: then each node joins its nearest AP, so every AP serves the nodes around it. If they use different channels, nodes stay on the grid channel; APs on other channels only contribute signal strength from a quick scan every few minutes, which helps placement but not live sensing. The setup flow shows which case a home is in and what moving the APs to one channel would gain.

**Access point settings that help.** None are required; each one makes sensing better. Example names are from TP-Link Omada.

| Setting | Why | In Omada |
|---|---|---|
| Same fixed 2.4 GHz channel on every AP (1, 6 or 11, 20 MHz) | Each node joins its nearest AP | Devices > AP > Config > Radios |
| Fixed 2.4 GHz transmit power, not auto | Automatic power changes look like people moving | Same place |
| Disable 802.11b (CCK) rates, keep beacons above 1 Mbps | Beacons and management frames then carry CSI | WLAN > SSID > 802.11 Rate Control |
| No automatic channel changes on 2.4 GHz | Every change forces the grid to move channel | Channel and WLAN optimisation settings |
| No minimum signal kick or load balancing on the nodes' network | Stops APs disconnecting weak nodes | SSID advanced settings |

Disabling 802.11b rates only locks out devices that support nothing newer, which are rare today. A separate 2.4 GHz network just for the nodes (it can be hidden) avoids touching the main WiFi at all, since every network name sends its own beacons.

### Node lifecycle (proposed)

Nodes come and go: a reboot, a dead USB charger, a new node for another room, a board swapped for a better one. The firmware handles all of it by itself; Home Assistant follows and never has to step in.

- **Stable identity.** A node's id comes from its MAC, so it survives reboots and reflashing. A link is the pair (sender, receiver), so its history and calibration stay tied to the same two nodes.
- **New.** A node heard for the first time listens for a few rounds: it copies the hive from its neighbours and picks up the clock. Then it takes a slot and starts pinging. The hive places it against the existing layout straight away.
- **Active.** Heard in recent rounds. Its slot, links and row are live.
- **Quiet** (missed a few rounds, about a second). Its slot is freed and its links are marked stale. Everything else is kept, so a reboot costs nothing.
- **Missing** (quiet for minutes). Its position and calibration are kept and the layout holds it in place. Its health shows missing.
- **Forgotten** (missing for a long time, for example a day). The hive drops its row and links, compacts the slots and re-solves the layout. Every node decides this on the same timers from the shared clock, so they agree without voting.
- **Back.** A node that returns before it is forgotten resumes at once. After that, it joins as new, and Home Assistant can still restore its old position by its MAC.

The grid round has a fixed number of slots, so nodes joining or leaving never change the round length. The engine works on whatever links are live right now and trains room models per link, so a missing link costs a little confidence, never the whole floor.

Home Assistant mirrors the lifecycle: it creates a device for a new node and asks for its floor, shows quiet and missing nodes as offline, and marks forgotten ones. A "replace node" action moves an old node's position, floor and name to a new board; its links still learn their own baseline.

### Self-healing in the firmware (proposed)

Each node keeps itself and the grid running without Home Assistant.

- **Clock.** If the clock AP disappears (a reboot), the hive picks another AP every node hears. If none is left, nodes keep their slots on their own timers and the lowest MAC node's beacon becomes the temporary clock until an AP returns.
- **Channel changes.** If the AP comes back on another channel, nodes reconnect and ESP-NOW follows. A node that has lost the grid scans the channels for hive beacons.
- **WiFi.** Reconnect with backoff, keeping the BSSID lock. Unsent UDP data is dropped when stale: fresh data wins.
- **Memory of the hive.** Rows, layout and anchor are saved to flash (NVS) every few minutes and on big changes, limited to spare the flash. After a reboot a node rejoins with full knowledge in about a second.
- **Watchdogs.** Task and hardware watchdogs on the CSI, radio and network tasks, plus brownout detection. Reboot reasons and counts go into the health data.
- **Safe updates.** ESPHome's safe mode: after repeated failed boots the node starts a minimal firmware mode that only accepts OTA, so a bad update can always be replaced over the air.
- **Fixed memory.** Static buffers, no heap churn. The lowest free heap is reported.
- **Conflicts.** Two nodes claiming the same slot or short id are resolved by MAC order. A hive hash that disagrees for too long triggers a full resync from a neighbour.
- **Capacity.** When every slot is taken, extra nodes stay as listeners: they still capture CSI from the others but do not ping.

### Setup, fallback and remote control (proposed)

A node is never stuck, and never needs a ladder to reach it.

**Getting WiFi details in**, all standard ESPHome:

1. **Web flasher.** Improv Serial over USB, right after flashing.
2. **Fallback hotspot.** A node that cannot connect opens its own hotspot `wisp-xxxxxx` (named after the node, with the last six MAC digits) after a minute (ESPHome `ap` and `captive_portal`). Joining it opens the setup page by itself. The node keeps retrying its saved WiFi in the background and closes the hotspot once connected.
3. **Improv over Bluetooth**, if it fits in the C3's RAM: Home Assistant offers to set up a new node straight from its discovery prompt.
4. **BOOT button.** Hold for 10 seconds for a factory reset.

**Remote control from Home Assistant**, through the ESPHome API:

- **Restart**, **Safe mode** (boot into the OTA-only mode) and **Factory reset** buttons on every node.
- **Firmware update** entity: each node checks the manifest on GitHub Pages (ESPHome `update` with `http_request`), Home Assistant shows "update available" for every node, and one click updates it. OTA from the ESPHome dashboard or command line works too.
- **Settings as entities:** floor, name, lifecycle timers and developer switches, so they appear in Home Assistant and on the node's web page alike.

**Security.** ESPHome API encryption key and OTA password, set per install. The fallback hotspot and the web server get a password once the node is set up.

**While WiFi is down the node waits, it does not reboot.** ESPHome keeps scanning every channel to reconnect, and after 90 s opens the fallback hotspot, so the grid pauses: beacons go out on whatever channel the radio is on, and nothing reaches Home Assistant anyway, since reports go over WiFi. On reconnecting, the node re-arms CSI and ESP-NOW, steers back to the grid channel and the hive resyncs within seconds; the CSI and beacon watchdogs catch anything a Wi-Fi restart left disarmed. Stretch goal: a neighbour that is still connected forwards the node's data to Home Assistant over ESP-NOW.

**Self-healing, in the firmware:** a beacon watchdog restarts ESP-NOW after 10 s without a beacon sent and re-arms the slot timer if it stopped; a CSI watchdog re-arms CSI and restarts the gateway pings after 30 s without a frame from the access point (waits doubling to 10 min for a gateway that never answers); a node alone on the grid for 5 min in a network with several channels reconnects to re-check the grid channel (waits doubling to 6 h); the saved grid access point survives two scans that miss it; and the core task runs under the task watchdog.

### Web interface (proposed)

Every node runs ESPHome's web server at `http://wisp-xxxxxx.local` and on its fallback hotspot. It shows every entity, the logs and an OTA upload with no extra work. Wisp adds its own entities:

| Group | Entities |
|---|---|
| Status | Firmware, chip, uptime, home AP (BSSID, channel, signal), free memory, last reboot reason |
| Grid | Members and their lifecycle state, slot, clock AP, hive hash, APs seen (grouped BSSIDs), live link scores |
| Node | Name, floor, identify (fast LED blink), forget a node |
| Developer | Raw CSI streaming to a PC on or off |

Later, a small script added to the page (ESPHome `js_include`) draws the hive's layout as a map. Home Assistant stays the main place for floors and positions; the web page is for setup, checks and running without Home Assistant.

### Also in the first firmware (proposed)

- **Status LED.** ESPHome status LED for connecting, fallback hotspot and errors; the grid state on top. On the S3 test board, its RGB LED.
- **Raw CSI streaming.** A developer switch that streams raw CSI over UDP to a PC, plus a small Python script to record and plot it. Phase 1 needs real recordings to tune the disturbance score.
- **Radio settings for clean CSI.** CSI enabled in the ESP-IDF config (`CONFIG_ESP_WIFI_CSI_ENABLED`). WiFi power save off (`power_save_mode: none`), since it breaks CSI timing and ESP-NOW reception. ESP-NOW pings at one fixed 802.11n rate, so every ping carries the same kind of CSI. WiFi country taken from the AP, so channels 12 and 13 work in the UK and Europe.
- **Versioned packets.** Every UDP and ESP-NOW packet starts with a protocol version, node id and sequence number. Unknown versions are ignored safely and reported.
- **One build per chip.** ESPHome's standard OTA partition layout for 4 MB flash, so one build fits every board of that chip; bigger boards simply have room to spare.
- **Discovery.** Home Assistant finds nodes through ESPHome's own discovery. The Wisp integration recognises them by the ESPHome project name `albert-canfield.wisp-node` and attaches its entities to the same device.
- **Build and release.** CI builds the C3 and S3 firmware with ESPHome on every push; a version tag publishes the release, the web flasher and the update manifest on GitHub Pages.

### Shared grid knowledge: the hive (proposed)

Every node holds the same picture of the whole grid, like a honeycomb memory. A new node learns everything from any neighbour within a few rounds, and the grid keeps its layout even while Home Assistant is down.

- **Each node shares its own row.** The ping every node already broadcasts each round (for CSI) also carries what that node hears from each neighbour: a slow median signal strength, plus an FTM range where available. About 4 bytes per neighbour, so 16 neighbours fit easily in one ESP-NOW frame. No extra frames.
- **Rows circulate.** Each row has a version number; nodes keep the newest row from every origin. Every ping also relays one other node's row in rotation, so the matrix reaches nodes that cannot hear each other directly.
- **Agreement without a leader.** Each ping carries a short hash of the matrix the sender holds. Same hash, same knowledge, same layout, because the solver is deterministic (inputs ordered by node id, fixed start, coordinates rounded to 10 cm so chips with and without an FPU agree).
- **Ranks over raw dB.** Cheap boards differ in antenna and power. The solver turns each node's readings into an order ("B and C stronger, D weaker"), which cancels those differences. Combining both ends of every link adds robustness.
- **Ageing.** Rows follow the node lifecycle: stale when their origin goes quiet, position kept while it is missing, dropped once it is forgotten.

Example: four nodes report only stronger or weaker.

| Node | Stronger | Weaker |
|---|---|---|
| A | B, C | D |
| B | A, D | C |
| C | A, D | B |
| D | B, C | A |

Every node computes the same plausible layout (or its mirror image, until anchored):

```
          B

A                 D

          C
```

**Maths, light enough for a node.**

- **Classical MDS** (Torgerson) gives the layout in one step: double-centre the squared distance matrix and take its two leading eigenvectors (power iteration). No iterations that can get stuck in a wrong fold. For 16 nodes this is a few thousand multiply-adds, milliseconds even with software floats on an ESP32-C3.
- **SMACOF**, warm-started from that result, refines it in a few steps and handles noisy and missing links with weights.
- **A new node** is placed against the existing layout by least squares on its own distances (two unknowns), without re-solving the whole grid.
- **Recompute only when the matrix changes** by more than a threshold, and only while the house is quiet, because people moving is exactly the signal the sensing uses.
- **Limits.** Signal strength measures radio distance, not metres: a node behind a thick wall looks further away than it is. FTM ranges and the user's anchor correct that.
- **Built (0.1.5): the exponent is chosen per hive.** Indoors, walls and board-to-board differences make the metric distances break the triangle inequality, and then the least-stress layout is a line (the owner's 4 nodes: 0.8 m, 1.7 m and 4.8 m for one triangle). The solver now tries six path-loss exponents, from 7.8 (distances squeezed together) down to 2.56, each continuing from the last: classical MDS of the most squeezed distances, then 10 SMACOF steps with relative weights (1/d^2) per exponent, each scored by its mean square error in dB over the measured pairs. Of the fits within 0.4 dB^2 of the best, the one nearest the nominal exponent (4.0, typical indoors through walls) wins, gets 15 more steps and is scaled to that exponent's metres. On 1800 simulated homes (3 to 8 nodes, walls, per-board offsets of 4 dB) the median shape error fell from 0.33 to 0.27 and layouts on a line from 14% to 1% (3 nodes: 55% to 6.5%); the owner's rows give a 2D layout. Kruskal's non-metric MDS was tried and did worse (near-line shapes fit the RSSI order perfectly). About 20 ms on an ESP32 at 16 nodes, 1.1 KB more workspace. `firmware/tools/layout_study.py` holds the study. Home Assistant places access points with the same nominal model (exponent 4.0).

### Node placement (proposed)

Nodes can estimate where they are, but not precisely enough to skip the user. Auto-placement gives a first guess that the user confirms on the floor plan. Room presence (phase 2) uses fingerprints and does not need node positions at all; only x/y (phase 3) does. Three nodes plus at least one access point is the minimum for a layout; one or two nodes can only sense motion.

1. **Floors.** Links between floors are much weaker than links within a floor, so clustering the signal strength graph splits nodes into floors.
2. **Distances.** Each pair of nodes measures its range. Wi-Fi FTM (round trip time) where the chip supports it (ESP32-C2, C3, S2, S3, and C6 from chip revision 0.2): about 1 to 3 m indoors after an offset calibration. Signal strength with a fitted path loss model as the fallback (original ESP32), much rougher.
3. **Layout.** Every node solves the layout from the shared matrix (see the hive below): classical MDS, then SMACOF steps over a few path-loss exponents (see Maths above). The result is a relative layout, right up to rotation, mirror and shift.
4. **Anchor.** The user marks the access points on the floor plan. With three APs on the floor nothing else is needed. With one AP, the user also drags one node (two if the AP is on another floor) plus one tap to flip the mirror if needed. The rest snap into place and can be nudged. Positions the user sets always win, and Home Assistant shares the anchor back into the hive so every node knows real coordinates.
   **Built, first version (in Home Assistant):** per floor (Home Assistant floors, plus the hub's own floor) an image URL and its size in metres, and the positions the user dragged nodes (by MAC) and access points (by BSSID) to in the panel, in metres from the image's top left corner with y down; kept in `.storage` per hub (`plans.py`), removed with the hub or the floor. Placed positions win. The hive's layout of the floor's nodes is fitted onto the placed nodes with the least squares similarity (rotation, mirror, uniform scale, shift; Umeyama's closed form, `engine/anchor.py`); unplaced nodes follow it. The mirror goes to the lower error; two placed nodes, or placed nodes on one line, cannot tell, so the layout keeps the handedness the map showed until a third node off the line is placed. With one placed node the layout is only shifted onto it, with none it is centred on the plan, at the hive's own scale. Unplaced access points are placed from how strongly the placed nodes hear them, now in plan metres. Placed access points stay put but do not anchor the fit yet, and the anchor is not shared back into the hive yet.
5. **Watch.** Ranges are re-measured now and then; a large change raises a "node moved?" warning.
6. **Later.** Refine from people walking: the order in which links react as someone crosses them constrains the geometry.

## Firmware build plan

### Three layers

| Layer | What it is | Depends on |
|---|---|---|
| `wisp-core` | The logic: link table, baseline and score, hive rows and merging, node lifecycle, slots, layout maths, AP grouping, packet format. Plain C++17, fixed-size arrays, no allocation, deterministic. | Nothing |
| Platform adapter | Talks to the chip: CSI callback and ring buffer, ESP-NOW, the AP clock, WiFi info, NVS, UDP. | ESP-IDF |
| ESPHome wrapper | The external component: YAML schema, entities, starting the core task. | ESPHome |

The core compiles on a computer too, so it has host unit tests and a simulator that runs many virtual nodes: kill one, add one, reboot one or drop the access point, and watch the grid heal. If ESPHome ever gets in the way, only the wrapper changes.

### Milestones

| Step | What | Hardware |
|---|---|---|
| M0 (done) | Plain ESPHome node: API, OTA, web server, fallback hotspot, Improv, restart, safe mode and factory reset buttons, status LED. Adopted in Home Assistant. | 1 node |
| M1 (done) | CSI from the AP: ping the router about 20 times a second, stream raw CSI to a computer, record and plot it while walking around. | 1 node |
| M2 (done) | First disturbance score on the node, as a slow ESPHome sensor. | 1 node |
| M3 (done) | ESP-NOW beacons, membership, slots, node-to-node CSI. | 3 nodes |
| M4 (done) | Hive rows, node lifecycle and self-healing, proven in the simulator first. | Simulator, then 3 nodes |
| M5 (done) | UDP link stream and the Wisp integration skeleton with one sensor per link. | 3 nodes |
| M6 | Several access points: BSSID grouping, overhearing, CSI from beacons. | 3 nodes |
| M7 | CI builds, web flasher on GitHub Pages, update manifest. | None |

Use the same board model for every node: mixed boards make links harder to compare. Boards with a decent printed or external antenna give steadier CSI than the tiniest ceramic-antenna boards.

**First test node: ESP32-S3 N16R8.** GPIO 33 to 37 belong to its octal PSRAM. The RGB LED is on GPIO 48 (GPIO 38 on some board revisions). Either USB-C port can flash it, but logs and Improv use the native USB port; the COM port (a CH343 serial chip) stays silent after boot. The generic S3 build does not use the PSRAM and uses a 4 MB layout, so it runs on every S3 board.

## Engine (`custom_components/wisp/engine/`)

Pure Python, unit tested, no HA dependency.

- `protocol.py`, `links.py`, `hive.py`: the UDP protocol, the latest state per link, the best recent hive report.
- `imaging.py`: where on a floor. `Locator` finds one moving person by best fit: for every 25 cm pixel it predicts how much each link would be disturbed by someone there (falling off with the distance to the link's line) and keeps the pixel that fits the observed links best, quiet links included. Only pixels within 1.5 link widths (0.6 m) of a link's line count, and the fit's scale leans towards the busiest link's disturbance: with a free scale, a spot far from every link, or at a busy link's fringe, fitted the pattern as well as a spot on the busy line. On synthetic floors it reaches a median error of about 0.3 m, against 1.2 m for classic radio tomographic imaging (`Imager`, kept for heat maps; both run in plain Python by solving only links-by-links systems).
- `tracking.py`: access points placed from how strongly the nodes hear them (kept within twice their distance from the node hearing them best, since readings the geometry cannot meet would push the fit away without end), and a Kalman track with a gate against wild fixes.
- `floor.py`: one floor from hive layout and link scores to a smoothed position with a quality; on a floor plan, in the plan's metres. Someone is placed only while at least one of the floor's links reports motion (the node's detector, so its Motion threshold and hysteresis): replaying 1.5 quiet hours, the sum of quiet links alone put a phantom on the map in 2.2% of seconds, the motion gate in one 2 s stretch.
- `anchor.py`: the similarity (rotation, mirror, scale, shift) that fits the hive's layout onto the nodes placed on a floor plan, see Node placement.
- `rooms.py`: room presence from calibrated motion fingerprints, see below. A floor is classified only while one of its links reports motion, as in `floor.py`.

### Room presence (built: motion fingerprints and still presence)

`engine/rooms.py`. Rooms are Home Assistant areas, floors are Home Assistant floors. A node's floor is the floor of its area (set per node); nodes and areas without a floor share one floor named after the hub. A link belongs to the floor of the node that receives it.

- **Features.** Once a second per floor: the log motion score of every live link on it (reported in the last 3 s), so 1.0, as quiet as usual, is 0, and the link's signal (RSSI, the mean of the last 5 s), since a body near a link's path weakens it. Missing links are left out, never taken as 0.
- **Calibration.** Per room, two classes: moving, recorded while someone walks around it (still moments are skipped), and still, recorded while someone sits still in it (moments with motion are skipped); per floor, an empty class recorded with nobody on it, since a still person now counts. The latest 600 per class are kept in `.storage`. After each calibration Wisp checks how well a floor's classes can be told apart from their own samples (separation, in diagnostics and the panel).
- **Classifier.** Per class and link a mean and a variance (diagonal Gaussian, variance at least 0.01, about 10 %), from links seen in at least 20 samples; a class needs 20 samples. Each class is scored by its mean log-likelihood per shared link, because the links of a floor see the same person and are not independent; a softmax turns the scores into probabilities. A class sharing under half the links of the best covered class (calibrated before a node joined, or for an area now on another floor) sits out. New links count once the rooms are calibrated again.
- **Decision.** If no link reports motion (the node's detector, so its Motion threshold and hysteresis), nobody moves on the floor. Otherwise the most probable class among the rooms' moving classes and the empty class wins; the empty class winning also means nobody moves.
- **Still presence.** While nobody moves, a still classification uses the log scores and the signal of every link, quiet ones included: the floor's empty class, the reference, against each room's still class (before a room has one, its moving class's signal). A room winning it 10 s in a row, with a signal not unlike its class (mean squared z-score at most 4), keeps or turns on its presence, with the attribute still.
- **Presence.** A room is occupied while it won, moving or still, with a probability of 0.6 or more within the hold time (60 s by default): moving for 2 s in a row (one stray second lit a room for a minute), still for 10 s in a row. Another room winning does not end it: two people can be in two rooms.
- **Sitting and working.** Small motion (typing, shifting in a chair) flags links but matches no room's walking. A still calibration keeps those seconds, and when the moving classification picks the empty floor or no room clearly, the still classification decides. On the owner's desk minutes the office won 99% of seconds.
- **While recording.** A calibration's instructions say where everyone is (in its room, moving or still, or off the floor), so presence and the map follow them for its length.
- **The map follows presence.** With rooms calibrated, the map shows someone only in a room with presence (the one someone walks in now, else the latest to win), searches the fit inside that room only, and draws footprints only while presence says someone walks. With 4 corner nodes, one link often crosses three rooms, and the best fit on the whole floor lay next door 2 times in 3 while presence named the right room.

## Home Assistant integration (`custom_components/wisp/`)

Finds nodes among the ESPHome devices by their project name, config flow, UDP listener, runs the engine, and creates the entities: room, x and y per floor, presence per room, link and grid health. Its entities join each node's ESPHome device, so a node is one device in Home Assistant with restart, update and Wisp entities together. Floors and positions are set here, not in the flasher.

## Frontend

- `wisp-map-card`: live footprints on an SVG floor plan. One floor per card (`floor` option); with a plan, its image under the drawing at its size in metres.
- Calibration panel: place nodes on the floor plan, run the calibration walk.

The map feed (`wisp/map/subscribe`) adds `floors` (each floor's nodes, and with a plan its `plan` and `positions` in plan metres) when there are several floors or a plan; the panel feed adds `plan`, `positions`, `access_points` and `fit` to a floor with a plan. Plans are set with the admin commands `wisp/floor/set_plan` (floor, url, width, height), `wisp/floor/place` (floor, nodes and access_points as id to `[x, y]`, or null to let Wisp place one again) and `wisp/floor/clear` (floor).

Both ship inside the integration and register themselves, so one HACS install brings everything.

## Web flasher (`docs/flasher/`)

[ESP Web Tools](https://esphome.github.io/esp-web-tools/), the flasher ESPHome uses, hosted on GitHub Pages at `albert-canfield.github.io/wisp`. It needs Web Serial (Chrome or Edge on desktop).

1. Plug in the board, click Install, pick the port. The right build is chosen from the chip.
2. After flashing, the page asks for WiFi credentials over Improv Serial.
3. The node joins WiFi, announces itself over mDNS and Home Assistant discovers it.

The manifest points at one factory image per chip (bootloader, partition table and app) at offset 0, plus an `ota` block (OTA image, md5, release notes) that ESPHome's update entity reads, so the same file drives first installs and updates. A GitHub Actions workflow, triggered by publishing a GitHub release `vX.Y.Z`, builds the firmware with ESPHome (`esphome/build-action`), writes the version and OTA details into `manifest.json`, publishes `docs/flasher` to GitHub Pages and attaches the images to the release.

## Transport

Nodes send UDP, not MQTT. Each node sends one small packet per round with all its links, node and AP: 4 nodes at 10 Hz is 40 packets and 160 link scores a second, which UDP handles with less overhead and no broker. Management (restart, OTA, health, settings) goes over the ESPHome API. MQTT is only for publishing results, in the optional Docker setup.

## Prior art

- [ESPectre](https://github.com/sunbox/espectre): CSI motion detection as an ESPHome component, one node at a time.
- [TOMMY](https://www.tommysense.com/): Wi-Fi sensing for Home Assistant with CSI on ESP32.
- [Espressif ESP-CSI](https://github.com/espressif/esp-csi): reference examples and the esp-radar component.

What Wisp adds: a self-forming grid of many nodes with shared knowledge, access points as anchors, and positions on a floor plan.

## Naming

| Thing | Name |
|---|---|
| Full title | Wisp: WiFi Spatial Presence |
| Repository | `albert-canfield/wisp` |
| HA domain | `wisp` |
| Entities | `sensor.wisp_floor_2_room`, `binary_sensor.wisp_office_presence`, `sensor.wisp_floor_1_x` |
| Firmware | `wisp-node` |
| Card | `wisp-map-card` |

"WISP" also means wireless ISP, so the full title is used wherever people search.

## Repository layout

```
wisp/
  firmware/                  wisp-node (the core)
    common/base.yaml         ESPHome base shared by every chip
    common/board-*.yaml      board settings per chip
    wisp-node-esp32*.yaml    ESPHome config per chip (S3, C3, original ESP32), built from this repository
    import/                  the same configs for the ESPHome dashboard, with the component from GitHub
    components/wisp/         ESPHome external component, one flat folder (ESPHome copies no subfolders)
      core_*                 wisp-core: portable C++, never includes ESPHome or ESP-IDF
      esp_*                  ESP-IDF adapter: CSI, ESP-NOW, clock, NVS, UDP
      wisp_component.*       ESPHome wrapper
    test/                    host unit tests and the grid simulator
    tools/                   CSI recorder and plotter (Python)
  custom_components/wisp/
    engine/                  pure Python: geometry, layout, imaging, calibration, tracking
    frontend/                wisp-map-card and calibration panel
    brand/                   icons and logos
  docs/
    ARCHITECTURE.md
    flasher/                 web flasher (GitHub Pages)
  .github/workflows/         validation, tests, firmware build and Pages
```

## Phases

1. **Prove the signal** (one floor, 3 or more nodes plus the AP). Firmware (ESPHome plus the `wisp` component): self-forming grid with the node lifecycle and self-healing, slotted pings, CSI from nodes and APs, BSSID lock, per-link scores over UDP, raw CSI streaming. From ESPHome: API with restart and update, OTA, fallback hotspot, web server, Improv Serial, safe mode. Web flasher. Integration skeleton: discover nodes, receive UDP, per-link disturbance sensors. Done when links react as you walk between nodes.
2. **Room presence.** Floor plan and node placement (auto first guess, user confirms), calibration walk per room, fingerprint classifier. Entities: room per floor, presence per room, confidence.
3. **Position and map.** Tomographic imaging, a tracking filter with walls, the map card.
4. **Polish for HACS.** Config flow, OTA, diagnostics, documentation, automatic baseline.

## Open questions

- UDP packet format and versioning.
- ESP-NOW beacon and ping format, round length, how many rounds before a node drops out.
- Which CSI features feed the disturbance score (amplitude variance, subcarrier selection, phase).
- Whether FTM ranging can share the radio with the ping schedule, or runs in a separate setup window.
- Hive row format: node id size, median window, version and hash sizes, relay rotation.
- Slots per round (the maximum number of nodes on one channel).
- Lifecycle timers: how long until quiet, missing and forgotten, and whether the user can change them.
- AP ping rate per node, and whether beacons from common routers carry usable CSI.
- How many home routers answer FTM. Tested: the owner's TP-Link Omada AP on 2.4 GHz does not answer (status "no response"); the optional `ftm_probe` in the firmware checks any AP. Next option: node-to-node FTM, one node as responder.
- How much the AP links improve position in practice: measure in phase 1, with and without them.
- Multi-AP homes on different 2.4 GHz channels: how often to scan the other channels without hurting the grid.
- How reliably BSSIDs group into physical APs across vendors (Omada, UniFi, Deco, Eero, Orbi and others).
- Reading each node's signal as the AP hears it from the controller API (Omada, UniFi), since Home Assistant's Omada integration does not expose client signal.
- Improv over Bluetooth on the C3: RAM cost while WiFi and CSI are running.
- Home AP choice and BSSID lock while ESPHome's WiFi component manages the connection.
- ESP-NOW from the `wisp` component directly, or through ESPHome's own ESP-NOW component.
- RAM headroom on the C3 with the ESPHome API, web server and CSI together.
- ESP-NOW relay for nodes without WiFi: worth it in phase 1 or later.
- Calibration walk flow in detail.
- Multiple people on one floor.
- A key for the UDP streams: today any device on the network can subscribe and read motion per link. A key shared between the integration and each node (set up when the node is added) would sign subscriptions and reports.
