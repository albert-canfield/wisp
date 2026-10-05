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

## Per-link thresholds (firmware 0.1.7)

Four nodes on the ground floor, an evening of the firmware's own reports (2026-10-05, 17:15 to
23:45), with labelled stretches: the floor empty (11 minutes, the owner upstairs), the owner
sitting quietly (100 s), at the desk (22 minutes) and walking (31 s). Hive confirmation worked out
from the flags at 1 s, as the firmware does (after 0.1.6 went on at 23:30 it matched 1,235 of the
1,240 link-seconds the nodes confirmed). `firmware/tools/cfar_study.py` prints it all.

On the empty floor at the threshold 2.0, per link (receiver from transmitter), and the threshold
that would have given each false alarm rate there:

| Link | p50 | p99 | Flagged at 2.0 | 0.2% | 0.5% | 1% |
|---|---|---|---|---|---|---|
| a8c7 from d714 | 1.48 | 2.21 | 20.4% | 2.49 | 2.31 | 2.21 |
| 8d58 from d714 | 1.09 | 2.23 | 2.7% | 3.07 | 2.61 | 2.23 |
| ee90 from a8c7 | 1.21 | 2.01 | 2.7% | 2.13 | 2.03 | 2.01 |
| d714 from 8d58 | 1.06 | 1.97 | 2.0% | 2.36 | 2.22 | 2.00 |
| a8c7 from 8d58 | 1.06 | 2.00 | 1.8% | 2.81 | 2.23 | 2.00 |
| d714 from a8c7 | 1.46 | 1.96 | 1.7% | 2.21 | 2.11 | 2.00 |
| ee90 from 8d58 | 1.14 | 1.84 | 1.4% | 2.24 | 2.15 | 2.00 |
| four links | | | 0.15 to 0.9% | up to 2.51 | 2.00 | 2.00 |
| five links | | | 0 | 2.00 | 2.00 | 2.00 |

The firmware's adaptive threshold, replayed second by second over the whole evening (learning
only quiet seconds, so the labelled stretches are judged with what it had learned by then): link-
seconds and seconds with any link flagged on the empty floor, then seconds with any link flagged
and with a confirmed pair while sitting, at the desk and walking.

| Setting | Empty: link | Empty: any | Sitting | Desk | Walking |
|---|---|---|---|---|---|
| Fixed 2.0 (before) | 2.14% | 31.1% | 11% / 1% | 67% / 49% | 100% / 100% |
| 0.2%, margin 1.1 | 0.13% | 2.1% | 0% / 0% | 53% / 12% | 100% / 94% |
| **0.5%, margin 1.1** | **0.36%** | **5.4%** | **5% / 0%** | **63% / 38%** | **100% / 100%** |
| 0.5%, margin 1.2 | 0.12% | 1.9% | 0% / 0% | 27% / 8% | 100% / 81% |
| 1%, margin 1.1 | 0.61% | 9.0% | 7% / 0% | 65% / 41% | 100% / 100% |
| 1%, margin 1.2 | 0.30% | 4.5% | 3% / 0% | 57% / 14% | 100% / 100% |

No setting confirmed a pair on the empty floor, nor did the fixed 2.0: the hive's confirmation
already removes those. What the per-link threshold buys is fewer single flags (the map and rooms
act on them, and each one is a chance of a stray supporter), at the cost of a quarter of the
confirmed desk seconds. With margin 1.0 the thresholds barely move: the link's flagged seconds
never count, so its learned scores lie under its threshold. Learning from every second without
confirmed motion (flagged ones too, 30 s after) put several thresholds at 5 to 8 after the busy
evening and left a third of the walking seconds confirmed. A two-node night (01:00 to 07:00) flagged
0.02% of seconds at 2.0, and every link kept 2.0.

## Breathing (experimental, firmware 0.1.7)

Raw CSI of the same stretches, plus three hours of night (02:00 to 05:00, two nodes);
`firmware/tools/breathing_study.py`. Band ratio: the breathing band's peak (0.16 to 0.59 Hz)
over the median of 0.72 to 2.0 Hz, per 32 s window at 8 Hz, medians per link:

| Scalar per link | Empty | Sitting quietly | Links that tell (sitting at least 15) |
|---|---|---|---|
| Mean amplitude | 3.4 to 5.4 | 2.6 to 10.3 | 0 of 16 |
| Shape's distance from its mean | 3.0 to 5.7 | 2.6 to 26 | 1 |
| First principal component of the shape (per window) | 3.2 to 6.2 | 3.3 to 119 | 7 |
| The firmware's online projection (windows inside each stretch) | 3.6 to 6.1 | 2.9 to 67 | 9 |

The firmware's detector (two positive windows in a row, peaks within 2 bins, off after two
negative), share of motion-free link evaluations (every 4 s) flagged, and of evaluation times with
any link breathing:

| Ratio | Empty (2,286) | Night (10,628) | Sitting (264) | Desk (3,421) |
|---|---|---|---|---|
| 12 | 1.8%, any 23% | 0.9% | 44%, any 68% | 9.3%, any 58% |
| 16 | 0.4%, any 6% | 0.1% | 38%, any 68% | 4.9%, any 36% |
| **24** | **0, any 0** | **0** | **28%, any 68%** | **2.4%, any 19%** |
| 32 | 0, any 0 | 0 | 20%, any 64% | 1.5%, any 12% |

Sitting "any" stops at 68% because the first 32 s after sitting down still hold the walk: from
the first window clear of it every evaluation had at least one link breathing (1 to 7 of 16),
rates a median 13 a minute. Evaluating windows that still held the walk read them as breathing
(96% instead of 68%, the desk 38% instead of 19%): the firmware waits for a whole window without
motion on the link. Only one 100 s sitting was recorded: the detector stays off unless switched
on.

## Not measured yet

- Someone walking: `walk_test.py` is ready, per link and phase.
- Daytime: people, pets, appliances and heavy WiFi traffic.
- Three or more boards, boards spread through a room, other boards and other access points.
