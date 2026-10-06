# Room presence study, recording of 2026-10-05

An offline study of room presence on the owner's ground floor (4 nodes, 1 AP, 16 links): an
evaluation harness, today's engine as the baseline, per-second classifiers, rhythm features, and a
hidden Markov room tracker meant to replace the presence-first logic. Everything replays the
recordings as the integration sees them (its own `LinkTable`, one tick per second).

## Summary

- **Today's engine** gets 84% of present seconds right; the other 16% show nobody, because someone
  reading at a desk loses their room after 180 s without activity (`ACTIVE_HOLD`). The empty floor
  is clean once the 60 s hold after leaving has passed. Error over all labelled stay seconds:
  **12.5%**.
- **The room tracker** (`tracker.py`, an HMM over empty / walking / busy / resting per room; moves
  follow the plan; the floor is entered and left through the hallway) gets **100%** of present
  seconds, no room switches while sitting, and 84% walking recall (57% today). Error **1.4%** with
  the defaults, **3.4%** leave one window out (the honest number). CPU about the same as today:
  73 us per second per floor.
- **What it costs:** a walk out the tracker misses leaves someone shown for **341 s** (178 s
  today). The one exit in the labels is such a case: the stairs look like the WC to every
  classifier, so the tracker shows the WC for 72 s after the owner went upstairs (today's engine
  shows the hallway for 75 s). With the WC marked as next to the stairs (`exits`, a plan setting),
  every labelled second is right and leaving takes 27 s. That hint comes from looking at this
  exit, so it needs the owner to confirm where the stairs are.
- **Robust to calibration:** with each of the four calibration snapshots HA saved that evening, the
  tracker beats today's engine (1.4 / 0.0 / 0.0 / 13.4% against 12.5 / 49.2 / 18.8 / 18.8%).
- **Did not help:** RSSI features (walking-room accuracy fell from 81% to 42%), a magnitude-free
  pattern classifier, the 0.5 to 3 Hz gait band of the raw CSI (AUC 0.44 walking against desk
  work), online max-product decoding, cleaning the empty class.
- **Recommendation:** move `tracker.py` into the engine and let it drive presence and the map; add
  an "exits" (stairs) plan setting; make the empty-floor calibration skip seconds with confirmed
  activity; feed the firmware's breathing flags in as evidence for the resting states.

The data is thin: 23 min of labelled presence (22 of them in one office), 12 min of labelled empty
floor, 4 labelled walks, one evening, one house. Treat every number as a direction, not a rate.

## Data and harness

- **Recordings:** link reports from `data/wisp-*-20261005-{21,22,23}.wcsi`. `common.py` caches them
  as per-second arrays; the labels are in `common.STAYS`, `WALKS` and `ROUTE`.
- **Label fix:** the notes put the desk at 21:49 to 21:58. From 21:55:48 every link shows someone
  walking through the hallway, the utility room and the play room (10+ links flagged, scores up to
  7 on the play room's links). Scored to 21:55:45.
- **Calibration runs found in the recording** (`calib_times.py` matches each stored sample to its
  second). These seconds are left out of the test (one run overlapped the first desk window by
  10 s), and they give each sample a session for the leave-one-session-out tests.
  - Walking, two runs per room: hallway 20:55 and 21:39, office 20:57 and 21:22, WC 20:59 and
    21:41, utility 21:00 and 21:43, play room 21:18 and 21:45.
  - Still: office 21:02 and 21:37, WC 21:04 and 21:42, play room 21:19 to 21:21 and 21:46,
    utility 21:44.
  - Empty: 19:19, 20:07, 20:24, 20:49, 20:52.
- **Two of the five empty runs were not empty.** At 19:19 every second had confirmed activity and
  4+ links were flagged throughout; at 20:24, 54% of seconds did. That is 227 of the 479 empty
  samples.
- **Scored:** 1298 s at the office desk, 100 s sitting in the play room, 695 s of empty floor; 4
  walks, scored as transitions; the 23:30 room-by-room walk, scored as a sequence; and 22:41 to
  23:28, reported as "eve fp/h": not labelled, 47 min with no activity on any link (including node
  reboots for a firmware update), most likely nobody downstairs.
- **Baseline:** `baseline.py` drives the engine as `presence.py` does (presence.py's own activity
  check for older firmware, the nodes' confirmations from 23:18, presence first). The recordings
  carry no breathing flags, so the breathing hold is not exercised. A frozen copy of the engine
  gives identical numbers with the repo's current engine.

**Metrics** (`evaluate.py`):

- **err:** share of labelled stay seconds wrong (wrong room, nobody while present, someone on the
  empty floor).
- **acc / miss:** share of present seconds right / showing nobody.
- **fp/h:** empty seconds showing someone, per hour; **fp60/h** counts only from 60 s into each
  empty window.
- **sw/min:** room changes per minute within a stay.
- **walk rec / walk fa:** mid-walk seconds flagged walking / stay seconds flagged walking.
- **walks@10:** destination shown 10 s after the walk ends.
- **route edit:** edit distance between the rooms held 3 s or more and hall, office, hall, utility,
  play room, hall.
- **missed walk out shown:** seconds someone stays shown when a walk out is missed.

## Results, latest calibration (21:50)

| method | err | acc | miss | fp/h | fp60/h | sw/min | walk rec | walk fa | walks@10 | route edit | eve fp/h | missed walk out shown |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| today's engine | 12.5% | 84% | 16% | 166 | 0 | 0.13 | 57% | 1% | 3/4 | 3 | 0 | 178 s |
| tracker (defaults) | 1.4% | 100% | 0% | 150 | 0 | 0.00 | 84% | 3% | 3/4 | 6 | 0 | 341 s |
| tracker, WC as exit | 0.0% | 100% | 0% | 0 | 0 | 0.00 | 84% | 3% | 4/4 | 5 | 0 | 340 s |
| Viterbi (offline bound) | 0.0% | 100% | 0% | 0 | 0 | 0.00 | 84% | 4% | 4/4 | 2 | 0 | - |

**Share right per window, today / tracker:**

| window | today | tracker |
|---|---|---|
| desk 21:23 | 100% | 100% |
| desk 21:49 | 81% | 100% |
| desk 22:01 | 65% | 100% |
| play room | 100% | 100% |
| desk 22:06 | 100% | 100% |
| empty 22:08 | 86% | 87% |
| desk 22:20 | 46% | 100% |
| empty 22:29 | 100% | 100% |

**Seconds to reach the destination of each walk, today / tracker:**

| walk | today | tracker |
|---|---|---|
| office to play room | 13 | 14 |
| play room to office | 15 | 17 |
| out (upstairs) | 75 | 72 (27 with the exit) |
| in (from upstairs) | 8 | 9 |

**Where each goes wrong:**

- **Today:** the desk at 21:49 (368 s with no confirmed activity) and at 22:20 (440 s) drops to
  nobody after `ACTIVE_HOLD`; leaving shows the hallway for the 60 s `HOLD`.
- **Tracker:** the walk out at 22:08 reads as walking into the WC. The stair climb disturbs the
  same links as the WC's walking class (n1-n3 and AP>n3), so the WC stays shown for 72 s. In the
  room-by-room walk, the extra rooms are the stairs read as the WC at both ends, plus 5 s in the
  play room at 23:31:14.

### Robustness: the same test with each calibration snapshot

| saved at | training data | today | tracker | tracker, WC as exit | Viterbi |
|---|---|---|---|---|---|
| 21:50 | walking 20:55 to 21:46 (2 runs per room), still 21:02 to 21:47 | 12.5% | 1.4% | 0.0% | 0.0% |
| 21:28 | walking 20:55 to 21:23, office still class 17 samples (under 20: unused) | 49.2% | 0.0% | 0.0% | 0.0% |
| 20:29 | an earlier session 19:41 to 20:06 (1 walking, 1 still run per room), empty 19:19, 20:07, 20:24 | 18.8% | 0.0% | 0.0% | 0.0% |
| 20:21 | same session, empty 19:19 and 20:07 only (half of it with someone moving) | 18.8% | 13.4% | 13.4% | 21.9% |

- Today's engine holds a sitting person only in rooms with a still class; the tracker lets anyone
  stay in any drawn room (not the undrawn hallway). That explains most of the 21:28 difference.
- With the 20:21 snapshot, even the offline bound reads the busy desk at 21:23 as another room:
  there the classes are the limit, not the decoding.

### Not fitting the few labelled minutes

`tune.py` searches a grid of 864 parameter sets (adjacency, exits, fit weight, P(active | busy),
three transition rates, the leaving rate), each with all four calibrations, leave one window out:
for each of the 8 stay windows, take the set best on the other 7 and score it on the one left out.

| grid | held out, 21:50 | 21:28 | 20:29 | 20:21 | all |
|---|---|---|---|---|---|
| exits fixed to none (house-agnostic) | 3.4% | 1.0% | 5.5% | 18.8% | 7.2% |
| exits chosen per fold | 3.8% | 15.2% | 6.3% | 25.3% | 12.7% |
| WC as exit fixed | 2.4% | 15.2% | 6.3% | 25.3% | 12.3% |
| today's engine (no tuning) | 12.5% | 49.2% | 18.8% | 18.8% | 24.8% |

- Every fold chose doors only to the hallway and fit weight 1.0: the softmax temperature
  `Rooms.decide` uses, also the log-loss optimum on the calibration data alone.
- The variance comes from P(active | busy). Only the busy desk at 21:23 (85% of its seconds active)
  pins it down; without that window the folds pick 0.5, which then misreads that desk with the
  older calibrations.

**Sensitivity** (`tune.py --sensitivity`): each default halved and doubled alone, error over all
four calibrations (defaults: 3.7%).

| change | error, 4 calibrations | missed walk out shown |
|---|---|---|
| fit weight x0.5 | 12.0% | 154 s |
| fit weight x2 | 6.5% | |
| P(active given busy) 0.5 | 10.5% | |
| P(active given busy) 0.9 | 4.8% | |
| start_walk x2 | 6.9% | |
| calm x2 | 7.8% | |
| touching rooms adjacent | 9.7% | |
| stir x2 | 4.1% | 223 s |
| everything else | 3.2 to 4.7% | |

A shorter hold trades accuracy for a shorter phantom: stir x2 cuts it to 223 s for 0.4 points of
error; fit weight x0.5 to 154 s for 8 points.

## Per-second classifiers (calibration only)

`classifiers.py`: which room someone walks in, one second at a time; train on one calibration
session (the first run of each room), test on the other, both ways.

| classifier | accuracy | log-loss | hallway | WC |
|---|---|---|---|---|
| engine (`ClassModel.fit`, log scores) | 81% | 0.60 | 57% | 67% |
| engine + signal (RSSI) | 42% | 4.32 | 6% | 0% |
| kNN, k=5 | 87% | 0.59 | 68% | 78% |
| kNN, k=15 | 86% | 0.47 | 66% | 80% |
| logistic regression | 84% | 0.46 | 55% | 77% |
| LDA (shared covariance, shrunk) | 90% | 0.62 | 82% | 80% |
| cosine of the link pattern | 86% | 0.85 | 84% | 57% |

- **RSSI** drifts too much between sessions 45 minutes apart: keep it out of walking decisions.
- **LDA** is the best single-second classifier (a 16x16 covariance, cheap in pure Python). Inside
  the tracker the engine's Gaussian already gets every labelled second right, so it is not needed
  now; worth retrying if a house shows room confusion.
- **The empty class:** trained on its quiet runs only, it stops winning walking seconds (8% down to
  0%), but in the tracker cleaning it made things worse (13.5% up to 17% at that stage): its
  "activity elsewhere" samples look like the stairs and upstairs.
- **The WC class** wins most moderate disturbances (desk fidgets at 21:23, the stairs): its walking
  class is the weakest. Magnitude-free patterns (cosine) avoid that but confuse walking in the WC
  itself.

## Rhythm features

`rhythm.py`, on the raw CSI: per link, the shape projected on its first principal direction over
4 s at 10 Hz; each second, the 3 most disturbed links.

| feature | AUC walking / desk at work | AUC walking / empty |
|---|---|---|
| CSI energy | 0.90 | 0.95 |
| CSI share of 0.5 to 3 Hz (gait band) | 0.44 | 0.60 |
| mean positive log score (what HA has) | 0.88 | 0.94 |
| its rise over 3 s | 0.56 | 0.55 |

- The gait band does not separate walking from desk work: both put about 0.6 of their power there.
- Energy does, but the per-second scores HA already has carry the same information (balanced
  accuracy 87% against 91%, optimistic folds). Nothing here for the integration.
- Breathing (0.15 to 0.6 Hz) is covered by `firmware/tools/breathing_study.py` and firmware
  `core_breathing.h`; its flags are the evidence the tracker lacks.

## How the tracker got here (present-seconds accuracy at each stage)

| stage | result |
|---|---|
| intensity + room-softmax emissions | 10%: a busy desk is as intense as walking, and the WC class wins weak patterns |
| each state on its own class (walking, still, empty) | 54% |
| still = better of the still class and the empty class | 67% |
| still split into busy and rest | 90% (err 13.5%): activity independent per second meant 30 quiet seconds said "gone" |
| doors only to the hallway | 92% |
| still classes only on active seconds, any drawn room can hold someone | err 7.0% |
| fit weight 1, P(active given busy) 0.7, slower stir and start_walk | err 1.4% |

The active-seconds change fixed a phantom (with the 20:21 calibration the hallway stayed lit for 20
min on the empty evening); the drawn-rooms change fixed the 21:28 snapshot (49% down to 7%); the
final parameters came from the leave-one-window-out runs over four calibrations. Tried and dropped:
max-product decoding (78% against 90% at that stage), touching rooms as adjacent (worse in every
fold), a stricter empty class.

## Recommendation

1. **Replace presence first with the tracker** (`tracker.py`, about 300 lines, no new dependency).
   In `presence.py`, per floor: build `Classes` from `Rooms` after each calibration change
   (`from_rooms`); every tick call `step(scores, now, bool(active))` with the inputs `presence.py`
   already computes; use the estimate for the map (`room`, `walking`) and the presence sensors
   (room probability over 0.5, or the room shown). Keep `Rooms` for recording, classes and the
   separation check; its walk counting, holds and `ACTIVE_HOLD` go. Reset the tracker on restart.
   Expected from these data: labelled-second error from 12.5% to about 3% (held out), no flicker
   while sitting, walking recall from 57% to 84%, leaving through a recognised way out in about
   30 s instead of 75 s.
2. **Exits on the plan:** let the user mark the stairs (or the rooms beside them). Without it, any
   way off the floor that looks like a room leaves a phantom for up to about 6 minutes. Also
   suggest calibrating the hallway by walking it end to end, including the foot of the stairs.
3. **Empty calibration:** skip seconds with confirmed activity while recording the empty floor (as
   the walking classes skip quiet seconds), or warn when more than a few percent were active: 47%
   of the owner's empty samples were recorded with someone moving.
4. **Breathing as evidence** (firmware 0.1.7): add P(breathing | rest, busy) against
   P(breathing | empty) to the emissions. That separates "sitting quietly" from "left unseen", the
   trade-off the hold rates now set by hand (a 341 s phantom against quiet desk periods of 368 to
   440 s).
5. **Cost:** CPU 73 us per floor per second on the owner's Mac (today's `Rooms.step` 113 us), 52 us
   of it the class fits both share; memory 3R+1 floats of state (R = rooms) and a cached transition
   table (about 80 entries for 5 rooms); no numpy.

**Limits:** one person per floor (two people show as one, in the room with more evidence; today's
per-room holds can show two rooms); one evening, one house; the stairs-next-to-WC hypothesis;
parameters chosen on 35 labelled minutes.

**Next data worth collecting:** an hour of normal use with notes; someone leaving and entering by
each way; two people on the floor; a quiet sitter with breathing detection on.

## Files and commands

All scripts are in `firmware/tools/study/` and run with `.venv-ha/bin/python`; numpy is needed for
the study scripts only, `tracker.py` is pure Python. The cache and copies of the owner's
calibration snapshots and plan are in `data/study/` (git-ignored). `WISP_ENGINE_ROOT` can point at
a frozen engine copy.

- `common.py`: paths, labels, recordings to per-second link state (cached).
- `calib_times.py`: finds each calibration sample's second in the recording.
- `evaluate.py baseline hmm hmm:Viterbi [--detail] [--seq 22:03:30-22:06:30] [--calibration file] [--param k=v] [--phantom]`
- `baseline.py`: today's engine as `presence.py` drives it.
- `tracker.py`: the room tracker (the deliverable); `hmm.py`: its adapter for the harness.
- `tune.py [--fix '{"exits": []}'] [--sensitivity]`: grid search, leave one window out, sensitivity.
- `classifiers.py`, `rhythm.py`: the per-second classifier and rhythm studies.
