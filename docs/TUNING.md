# Tuning notes

Measurements behind the defaults: the motion threshold, its hysteresis, and when the map and rooms
believe someone moves. Each section says what was measured and on what, so it can be repeated on
other homes and hardware.

## Setup

- Two ESP32-S3 N16R8 boards side by side on a desk (0.4 m apart in the hive's layout), firmware
  0.1.0 on ESPHome 2026.9.1, USB powered.
- One TP-Link Omada access point on channel 1, heard at -48 and -61 dBm. Its ping replies come at
  MCS7 (98.5% from 02:11 to 03:20), the rest at MCS6.
- Four links: each board hears the access point and the other board.
- A quiet house at night, 02:11 to 05:30 on 5 October 2026 (3.3 h). Nobody moved near the
  boards as far as known; the Mac next to them stayed on.

## How

```bash
python firmware/tools/csi_logger.py <node> <node> --dir data          # overnight: raw CSI, link and hive reports
python firmware/tools/replay.py data/*.wcsi --since 02:11              # per link: score distribution, motion stretches
python firmware/tools/replay.py data/*.wcsi --since 02:11 --sweep      # motion seconds per threshold and persistence
python firmware/tools/replay_floor.py data/*.wcsi --hive <node> --since 02:11   # when and where the map places someone
```

`replay.py` is a line-by-line port of the firmware's motion score (`core_link_motion.h`). From
03:56 to 05:30 the recordings also hold the nodes' own reports: second by second, the port and the
firmware differ by 0.014 on average, with no bias and no lag (correlation 0.69 to 0.77 on scores
that barely move). The port starts a node's links over when the node reboots, as the firmware does.

## Quiet scores per link

Motion score: 1.0 is as quiet as usual. 3.3 h, about 11,900 scored seconds per link.

| Link (receiver from transmitter) | p50 | p95 | p99 | max |
|---|---|---|---|---|
| Node 58:58 from the access point | 1.03 | 1.08 | 1.12 | 1.73 |
| Node 58:58 from node c7:7c | 1.03 | 1.07 | 1.23 | 1.91 |
| Node c7:7c from node 58:58 | 1.03 | 1.09 | 1.18 | 1.40 |
| Node c7:7c from the access point | 1.03 | 1.09 | 1.18 | 2.20 |

The firmware itself reported the same shape (p50 1.03, p99 1.11 to 1.24 per link).

## Threshold and persistence

Seconds of motion per link over the 3.3 h, with the hysteresis the firmware uses now (on at the
threshold, off halfway back to 1.0), for thresholds and for how many seconds in a row a score must
stay at or above it.

| Link | 1.5 / 1 s | 1.5 / 2 s | 2.0 / 1 s | 2.0 / 2 s | 2.5 / 1 s | 3.0 / 1 s |
|---|---|---|---|---|---|---|
| Node 58:58 from the access point | 15 | 0 | 0 | 0 | 0 | 0 |
| Node 58:58 from node c7:7c | 42 | 0 | 0 | 0 | 0 | 0 |
| Node c7:7c from node 58:58 | 0 | 0 | 0 | 0 | 0 | 0 |
| Node c7:7c from the access point | 4 | 1 | 2 | 0 | 0 | 0 |

At the default threshold of 2.0, motion came on twice in 3.3 h, 2 s each time:

- 03:05:22 on node c7:7c from the access point (peak 2.20), access point rate and signal steady.
- 04:22:13 on node 58:58 from node c7:7c (firmware 2.06; the port saw the same burst peak at 1.77,
  split over two seconds). Seen in the firmware's own reports.

Both may well be real (the house, the Mac's fan); either way they are rare and short.

Choices:

- **Threshold 2.0** stays the default. The node's Motion threshold setting (1.2 to 6) changes it
  live; 1.5 is still quiet with 2 s of persistence, but noisier without.
- **Hysteresis**: off halfway back to 1.0 (1.5 for the default). It used to be off below 75% of
  the threshold, the same at 2.0, but a low threshold then never turned off: at 1.2 (off below
  0.9), a link stayed in motion for 65 to 83% of a quiet hour.
- **Persistence**: 1 s. Two seconds would remove both events above, but delays every detection;
  it can come later as an option if homes need it.

## The map and rooms

Both now act only while at least one link reports motion (the node's flag, so its threshold and
hysteresis). Before, the map used the sum of every link's log score (at least 0.3) and rooms any
score of 1.5 or more.

| 3.3 quiet hours (replay) | Before | Now |
|---|---|---|
| Seconds the map placed someone | 294 (2.45%), in 219 stretches | 2, in one stretch |
| Times rooms were classified | 29 | 1 |

The firmware's own flags add one more 2 s stretch (04:22, above) to the "Now" column.

A quiet link adds about 0.03 to the sum, so the old map gate would have fired more often the more
links a floor has. A room that wins holds its presence for 60 s, so each of those 29 was a chance
of a minute of false presence.

## Other findings

- **The access point's data rate does not move the shape.** MCS6 replies sit as close to their
  MCS7 neighbours (5.65% mean shape difference) as MCS7 replies sit to each other (5.91%).
- **After a node reboots,** the access point ramps its rate back up for about 20 s (MCS4 and MCS6,
  fewer replies). The firmware's 20 s settle after boot covers it.
- **Access point placement:** two boards side by side heard the access point 13 dB apart, which no
  position can meet; the fit ran off to 18 km. It now stays within twice the distance of the node
  that hears it best (1.5 m here).
- **Stability:** the boards ran through the night with no unexpected reset (every reset in the
  health log was an update or a restart asked for), free memory between 205 and 214 KB, the hive in
  sync and about 20.6 access point frames a second per board.

## Not measured yet

- Someone walking: `walk_test.py` is ready, per link and phase.
- Daytime: people, pets, appliances and heavy WiFi traffic.
- Three or more boards, boards spread through a room, other boards and other access points.
