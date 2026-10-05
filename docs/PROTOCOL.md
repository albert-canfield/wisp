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
| 19 | 1 | flags: bit 0 hive confirmation (node firmware 0.1.6 and later): links carry bit 1 of their flags, and the confirmed pairs follow the links; bit 1 more pairs were confirmed than the report lists |
| 20 | 4 | node uptime, seconds |
| 24 | 14 × n | links |

Each link:

| Offset | Size | Field |
|---|---|---|
| 0 | 6 | transmitter MAC: an access point BSSID or another node's MAC |
| 6 | 1 | kind: 0 access point, 1 node |
| 7 | 1 | mean RSSI over the interval, dBm (int8); -128 if no frames |
| 8 | 2 | motion score × 100 (uint16); 65535 = unknown. 100 means as quiet as usual. The raw score: since firmware 0.1.7 each link flags motion at its own threshold (see below), which the entry has no spare byte to carry |
| 10 | 2 | spread × 100 (uint16), percent: how much the signal shape moves right now |
| 12 | 1 | frames received in the interval (uint8, capped at 255) |
| 13 | 1 | flags: bit 0 motion detected on this link, bit 1 the hive confirms that motion (see below; only bit 0 before firmware 0.1.6), bit 2 someone breathing on this link (firmware 0.1.7, while its Breathing detection switch is on; see below) |

A link is identified by (transmitter, receiver). The same pair seen from the other end is a separate link: node A hearing B and node B hearing A are both reported.

With header flag bit 0, the node pairs the hive confirms follow the links:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | confirmed pairs p (at most 16) |
| 1 | 12 × p | pairs: node MAC a, node MAC b, a below b in MAC order |

Older readers stop after the links and never see them.

### Per-link thresholds

From firmware 0.1.7 a link flags motion (bit 0) at its own threshold: the node's Motion threshold, or higher for a noisy link, a margin of 1.1 above the score that 0.5% of its quiet seconds exceed (seconds with no motion confirmed anywhere in the hive for 10 s, and the link not flagged; decayed over about 20 minutes; the node's threshold until about 3 minutes of them are counted). It turns off halfway between that threshold and 1.0, as before. See "Per-link thresholds" in ARCHITECTURE.md.

### Breathing

Bit 2 of a link's flags: someone sitting still is breathing near it (firmware 0.1.7, experimental, off unless the node's Breathing detection switch is on). Set once two 32 s windows in a row, with no motion on the link in them, show a clear peak at 0.16 to 0.59 Hz (about 9 to 36 breaths a minute); cleared after two windows without, at once by motion on the link, and when the link goes silent. It never comes with bit 0 set. See "Breathing" in ARCHITECTURE.md.

### Hive-confirmed motion

A body changes a link both ways and the links around it; one node's own noise shows only on what it sends or receives. Every node works out, each second, which pairs of nodes (A, B) are confirmed:

1. Both ways: one direction (A hearing B, or B hearing A) reports motion now, the other did within 2 s.
2. A node nearby agrees: of the K = min(6, live nodes - 2) other nodes nearest the pair, at least 1 (K up to 4) or 2 (K from 5) saw motion on a link to A or to B, either direction, within the same 2 s. Nearest by the hive's layout (distance to the segment A-B); a node without a layout position goes by its signal strength to the nearer of A and B. Ties go by MAC.

What each node hears comes from the scores in the beacons it hears directly (below), at most 1.5 s old, judged at 2.0 (on at it, off halfway back to 1); for its own links a node uses its own flags. From firmware 0.1.7 a beacon carries each score normalised to its link's own threshold, 1 + (score - 1) × (2.0 - 1) / (threshold - 1), so judging it at 2.0 follows the receiver's own flag, on and off; at a threshold of 2.0 that is the score itself. Nodes before 0.1.7 sent raw scores and judged them at their own Motion threshold, the same with the default everywhere. So every node that hears the same beacons holds the same pairs, with no extra traffic. A node's link from another node carries bit 1 while it reports motion and that pair is confirmed; its link from the access point (no reverse direction) while it reports motion and a pair with this node was confirmed within 2 s. Two nodes alone never confirm: there is no third. See "Shared grid knowledge: the hive" in ARCHITECTURE.md for the numbers behind the rule.

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

Not for computers, listed for completeness. Defined in `core_grid.h` (beacon, type 1) and `core_hive.h` (hive row, type 2), magic `WG`, grid protocol version 3. Every node broadcasts one beacon per 100 ms round in its slot, carrying its own hive row with its live motion score (× 10, normalised to the link's own threshold from firmware 0.1.7) for each neighbour, then one relayed row from another node (no scores), with how long ago that row's origin was last heard directly. The scores are what hive-confirmed motion is worked out from.

