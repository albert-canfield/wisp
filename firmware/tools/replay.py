#!/usr/bin/env python3
"""Replay recorded CSI through the node's motion score, to tune it offline.

    python firmware/tools/replay.py data/wisp-a8c77c-20261005-*.wcsi
    python firmware/tools/replay.py data/*.wcsi --threshold 2.5 --csv scores.csv

A line-by-line port of core_link_motion.h (shape, running statistics, settling baseline, score,
detector with hysteresis), driven by the host time of each recorded packet. Prints, per link,
the score distribution and every stretch the detector would have reported motion. Recordings
that hold the nodes' link reports (csi_logger.py since it asks for them) also show what the
firmware itself reported, to check the port against it.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import math
from pathlib import Path
import struct
import sys
import time

from csi_logger import read_wcsi

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "wisp"))
from engine.protocol import LinkReport, ProtocolError, parse_packet  # noqa: E402

HEADER = struct.Struct("<4sBBHI6s6sIbbBBBBBBH")
SHAPE_IDX = list(range(2, 27)) + list(range(38, 64))  # same subcarriers as lltf_shape()


class LinkMotion:
    WARMUP_FRAMES = 40
    SETTLE_TICKS = 20
    BASELINE_SETTLE = 0.2
    BASELINE_DOWN = 0.05
    BASELINE_UP = 0.001
    QUIET_WINDOW_MINUTES = 10

    def __init__(self, alpha: float = 0.05) -> None:
        self.alpha = alpha
        self.mean = [0.0] * len(SHAPE_IDX)
        self.var = [0.0] * len(SHAPE_IDX)
        self.frames = 0
        self.fresh = 0
        self.baseline = 0.0
        self.settle = 0
        self.quiet = [0.0] * self.QUIET_WINDOW_MINUTES
        self.minute = 0
        self.minute_ticks = 0
        self.minutes = 0

    def add_frame(self, csi: bytes) -> None:
        if len(csi) < 128:
            return
        raw = struct.unpack_from("<128b", csi)
        amp = [math.hypot(raw[2 * i + 1], raw[2 * i]) for i in SHAPE_IDX]
        total = sum(amp)
        if total <= 0:
            return
        k = len(amp) / total
        shape = [a * k for a in amp]
        if self.frames == 0:
            self.mean = shape[:]
            self.var = [0.0] * len(shape)
        else:
            a = self.alpha
            for j, s in enumerate(shape):
                d = s - self.mean[j]
                self.mean[j] += a * d
                self.var[j] = (1 - a) * (self.var[j] + a * d * d)
        self.frames += 1
        self.fresh += 1

    def spread(self) -> float:
        return 100.0 * sum(math.sqrt(v) for v in self.var) / len(self.var)

    def tick(self) -> float:
        fresh, self.fresh = self.fresh, 0
        if fresh == 0 or self.frames < self.WARMUP_FRAMES:
            return math.nan
        sp = self.spread()
        if self.minute_ticks == 0 or sp < self.quiet[self.minute]:
            self.quiet[self.minute] = sp
        self.minute_ticks += 1
        if self.minute_ticks >= 60:
            self.minute_ticks = 0
            self.minute = (self.minute + 1) % self.QUIET_WINDOW_MINUTES
            self.minutes = min(self.minutes + 1, self.QUIET_WINDOW_MINUTES)
        quietest = min([self.quiet[self.minute], *self.quiet[: self.minutes]])
        if self.baseline <= 0:
            self.baseline = sp
        elif self.settle < self.SETTLE_TICKS:
            self.baseline += self.BASELINE_SETTLE * (sp - self.baseline)
        elif sp < self.baseline:
            self.baseline += self.BASELINE_DOWN * (sp - self.baseline)
        elif quietest > self.baseline:
            self.baseline += self.BASELINE_UP * (quietest - self.baseline)
        if self.settle < self.SETTLE_TICKS:
            self.settle += 1
            return math.nan
        return sp / self.baseline if self.baseline > 0 else math.nan


class Detector:
    """The firmware's hysteresis, plus an optional persistence: motion only after `persist`
    seconds in a row at or above the threshold (1 = the firmware today)."""

    def __init__(self, threshold: float, persist: int = 1) -> None:
        self.threshold = threshold
        self.persist = persist
        self.above = 0
        self.active = False

    def update(self, score: float) -> bool:
        if math.isnan(score):
            self.above = 0
            self.active = False  # a silent link reports no motion (firmware does the same)
            return False
        self.above = self.above + 1 if score >= self.threshold else 0
        if not self.active and self.above >= self.persist:
            self.active = True
        elif self.active and score < 1 + 0.5 * (self.threshold - 1):
            self.active = False
        return self.active


def percentile(values: list[float], p: float) -> float:
    if not values:
        return math.nan
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


def load(files: list[str], since: str | None = None) -> list[tuple[float, bytes]]:
    """Every recorded packet in time order, optionally from a local time (HH:MM) of the last day on."""
    packets = []
    for path in files:
        packets.extend(read_wcsi(path))
    packets.sort(key=lambda p: p[0])
    if since and packets:
        day = time.localtime(packets[-1][0])
        hh, mm = (int(v) for v in since.split(":"))
        start = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, hh, mm, 0, 0, 0, -1))
        packets = [p for p in packets if p[0] >= start]
    return packets


def seconds(packets: list[tuple[float, bytes]]):
    """Each second, every link's score as the firmware computes it (NaN while it warms up or is
    silent): yields (time, {(receiver, transmitter): score}). A node that reboots (its packet
    sequence starts over) starts its links over, as the firmware does; a transmitter that reboots
    does not touch them."""
    links: dict[tuple, LinkMotion] = defaultdict(LinkMotion)
    last_seq: dict[str, int] = {}
    next_tick = packets[0][0] + 1.0
    for t, pkt in packets:
        while t >= next_tick:
            yield next_tick, {key: link.tick() for key, link in links.items()}
            next_tick += 1.0
        if len(pkt) < HEADER.size:
            continue
        f = HEADER.unpack_from(pkt)
        if f[0] != b"WISP" or f[2] != 1:
            continue
        node, src, header_len, n = f[5].hex(":"), f[6].hex(":"), f[3], f[16]
        if f[4] < last_seq.get(node, 0):  # rebooted
            for key in [key for key in links if key[0] == node]:
                del links[key]
        last_seq[node] = f[4]
        links[(node, src)].add_frame(pkt[header_len:header_len + n])


def reported(packets: list[tuple[float, bytes]]) -> dict[tuple, list[tuple[float, float, bool]]]:
    """The firmware's own score and motion flag per link, from its link reports: the last report
    of each host second (the score changes once a second), as (time, score, motion)."""
    out: dict[tuple, dict[int, tuple[float, float, bool]]] = defaultdict(dict)
    for t, pkt in packets:
        if len(pkt) < 6 or pkt[5] != 2:  # byte 5: packet type, 2 = link report
            continue
        try:
            report = parse_packet(pkt)
        except ProtocolError:
            continue
        if isinstance(report, LinkReport):
            for link in report.links:
                if link.score is not None:
                    out[(report.node, link.transmitter)][int(t)] = (t, link.score, link.motion)
    return {key: sorted(rows.values()) for key, rows in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help=".wcsi files from csi_logger.py, any order")
    ap.add_argument("--threshold", type=float, default=2.0)
    ap.add_argument("--csv", help="write per-second scores here")
    ap.add_argument("--persist", type=int, default=1, help="seconds above the threshold before motion")
    ap.add_argument("--sweep", action="store_true", help="motion seconds per link for several thresholds and persistences")
    ap.add_argument("--since", help="only packets from this local time on, HH:MM")
    args = ap.parse_args()

    packets = load(args.files, args.since)
    if not packets:
        print("no packets")
        return 1

    detectors: dict[tuple, Detector] = defaultdict(lambda: Detector(args.threshold, args.persist))
    scores: dict[tuple, list[tuple[float, float, bool]]] = defaultdict(list)
    for t, tick in seconds(packets):
        for key, score in tick.items():
            scores[key].append((t, score, detectors[key].update(score)))

    if args.sweep:
        grid = [(th, pe) for th in (1.5, 2.0, 2.5, 3.0) for pe in (1, 2, 3)]
        print(f"motion seconds per link ({(packets[-1][0] - packets[0][0]) / 3600:.1f} h); columns: threshold/persist")
        print(f"{'link':42s}" + "".join(f"{th:>5.1f}/{pe}" for th, pe in grid))
        for key, rows in sorted(scores.items()):
            cells = []
            for th, pe in grid:
                d = Detector(th, pe)
                cells.append(f"{sum(d.update(s) for _, s, _ in rows):7d}")
            print(f"{key[0][-8:]} <- {key[1]:17s}{'':15s}" + "".join(cells))
        return 0

    firmware = reported(packets)
    out = csv.writer(open(args.csv, "w", newline="")) if args.csv else None
    if out:
        out.writerow(["time", "receiver", "transmitter", "score", "motion"])
    start, end = packets[0][0], packets[-1][0]
    print(f"{len(packets)} packets, {time.strftime('%H:%M', time.localtime(start))} to "
          f"{time.strftime('%H:%M', time.localtime(end))} ({(end - start) / 3600:.1f} h), threshold {args.threshold}")
    for (node, src), rows in sorted(scores.items()):
        valid = [s for _, s, _ in rows if not math.isnan(s)]
        events, current = [], None
        for t, s, active in rows:
            if out and not math.isnan(s):
                out.writerow([f"{t:.0f}", node, src, f"{s:.3f}", int(active)])
            if active and current is None:
                current = [t, t, s]
            elif active:
                current[1], current[2] = t, max(current[2], s)
            elif current is not None:
                events.append(current)
                current = None
        if current is not None:
            events.append(current)
        motion_s = sum(1 for _, _, a in rows if a)
        print(f"\n{node} <- {src}: {len(valid)} s scored, p50 {percentile(valid, 50):.2f}, "
              f"p95 {percentile(valid, 95):.2f}, p99 {percentile(valid, 99):.2f}, max {max(valid, default=math.nan):.2f}, "
              f"motion {motion_s} s in {len(events)} stretches")
        if rows_fw := firmware.get((node, src)):
            fw = [s for _, s, _ in rows_fw]
            moving = [m for _, _, m in rows_fw]
            starts = sum(1 for i, m in enumerate(moving) if m and (i == 0 or not moving[i - 1]))
            print(f"   firmware: {len(fw)} s reported, p50 {percentile(fw, 50):.2f}, p95 {percentile(fw, 95):.2f}, "
                  f"p99 {percentile(fw, 99):.2f}, max {max(fw):.2f}, motion {sum(moving)} s in {starts} stretches")
        for t0, t1, peak in events[:15]:
            print(f"   {time.strftime('%H:%M:%S', time.localtime(t0))} for {t1 - t0 + 1:4.0f} s, peak {peak:.2f}")
        if len(events) > 15:
            print(f"   ... {len(events) - 15} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
