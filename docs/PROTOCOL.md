# Wisp protocol

How nodes talk to computers and to Home Assistant over UDP. Little-endian throughout. The C++ side lives in `firmware/components/wisp/core_*.h`; the Python side in `custom_components/wisp/engine/protocol.py` and `firmware/tools/csi_recorder.py`.

## Ports

| Port | Where | What |
|---|---|---|
| UDP 47010 | every node | subscription requests in, streams out to subscribers |

Nothing is configured on the node: whoever wants data subscribes, and the node sends to that address and port until the lease runs out.

## Subscribe

Sent by a computer or Home Assistant to a node's port 47010, at least every 5 seconds.

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `WSUB` |
| 4 | 1 | protocol version (1) |
| 5 | 1 | streams wanted, bit mask: bit 0 raw CSI, bit 1 link reports, bit 2 hive reports. Optional: a 5 byte request means raw CSI only. |

- A lease lasts 10 seconds after the last request.
- A node serves up to 4 subscribers at once; a new address replaces the one closest to expiry.
- Raw CSI only flows while the node's "Raw CSI stream" switch is on. Link and hive reports always flow.

## Common header

Every packet a node sends starts with:

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `WISP` |
| 4 | 1 | protocol version (1) |
| 5 | 1 | packet type: 1 raw CSI, 2 link report, 3 hive report |
| 6 | 2 | header length: offset of the payload, so readers skip fields they do not know |

Readers ignore packets with an unknown version or type.

## Type 1: raw CSI

One captured frame. Header length 38.

| Offset | Size | Field |
|---|---|---|
| 8 | 4 | sequence number |
| 12 | 6 | node MAC (receiver) |
| 18 | 6 | source MAC (transmitter: an access point BSSID or another node) |
| 24 | 4 | radio timestamp, microseconds, node clock |
| 28 | 1 | RSSI, dBm (int8) |
| 29 | 1 | noise floor, dBm (int8) |
| 30 | 1 | channel |
| 31 | 1 | secondary channel |
| 32 | 1 | signal mode: 0 legacy, 1 HT, 3 VHT |
| 33 | 1 | MCS |
| 34 | 1 | bandwidth: 0 20 MHz, 1 40 MHz |
| 35 | 1 | flags: bit 0 STBC, bit 1 first word invalid, bit 2 short guard interval |
| 36 | 2 | CSI length in bytes |
| 38 | n | CSI: per subcarrier, imaginary then real (int8). LLTF first (64 subcarriers ordered 0..31, -32..-1), then HT-LTF when present. |

## Type 2: link report

Everything one node measured in the last report interval (default 200 ms, so 5 a second). Header length 24.

| Offset | Size | Field |
|---|---|---|
| 8 | 4 | sequence number |
| 12 | 6 | node MAC (receiver of every link below) |
| 18 | 1 | number of links |
| 19 | 1 | flags (reserved, 0) |
| 20 | 4 | node uptime, seconds |
| 24 | 14 × n | links |

Each link:

| Offset | Size | Field |
|---|---|---|
| 0 | 6 | transmitter MAC: an access point BSSID or another node's MAC |
| 6 | 1 | kind: 0 access point, 1 node |
| 7 | 1 | mean RSSI over the interval, dBm (int8); -128 if no frames |
| 8 | 2 | motion score × 100 (uint16); 65535 = unknown. 100 means as quiet as usual |
| 10 | 2 | spread × 100 (uint16), percent: how much the signal shape moves right now |
| 12 | 1 | frames received in the interval (uint8, capped at 255) |
| 13 | 1 | flags: bit 0 motion detected on this link |

A link is identified by (transmitter, receiver). The same pair seen from the other end is a separate link: node A hearing B and node B hearing A are both reported.

## Type 3: hive report

What one node knows about the whole grid (see "Shared grid knowledge: the hive" in ARCHITECTURE.md), every 5 seconds. Header length 26. At most 1400 bytes: rows that do not fit are left out and flagged.

| Offset | Size | Field |
|---|---|---|
| 8 | 4 | sequence number |
| 12 | 6 | node MAC (sender of this report) |
| 18 | 4 | hive hash: equal on every node that holds the same rows |
| 22 | 1 | flags: bit 0 in sync (every active member advertises the same hash), bit 1 rows truncated |
| 23 | 1 | layout points p |
| 24 | 1 | rows r |
| 25 | 1 | reserved (0) |
| 26 | 10 × p | layout: node MAC (6), x and y in centimetres (int16 each). Relative: rotation, mirror and scale come from anchors on the floor plan. |
| ... | | r rows, each: origin MAC (6), row version (uint16), entries k (1), then k × (neighbour MAC (6), RSSI int8) |

Rows include access points as neighbours; layout points are nodes only.

## ESP-NOW frames between nodes

Not for computers, listed for completeness. Defined in `core_grid.h` (beacon, type 1) and `core_hive.h` (hive row, type 2), magic `WG`, grid protocol version 2. Every node broadcasts one beacon per 100 ms round in its slot, carrying its own hive row, then one relayed row from another node.

