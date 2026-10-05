#!/usr/bin/env python3
"""Replay recorded CSI through the node's motion score, to tune it offline.

    python firmware/tools/replay.py data/wisp-a8c77c-20261005-*.wcsi
    python firmware/tools/replay.py data/*.wcsi --threshold 2.5 --csv scores.csv

A line-by-line port of core_link_motion.h (shape, running statistics, settling baseline, score,
detector with hysteresis), driven by the host time of each recorded packet. Prints, per link,
the score distribution and every stretch the detector would have reported motion.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import math
import struct
import sys
import time

from csi_logger import read_wcsi

HEADER = struct.Struct("<4sBBHI6s6sIbbBBBBBBH")
SHAPE_IDX = list(range(2, 27)) + list(range(38, 64))  # same subcarriers as lltf_shape()


class LinkMotion:
    WARMUP_FRAMES = 40
    SETTLE_TICKS = 20
    BASELINE_SETTLE = 0.2
    BASELINE_DOWN = 0.05
    BASELINE_UP = 0.001

    def __init__(self, alpha: float = 0.05) -> None:
        self.alpha = alpha
        self.mean = [0.0] * len(SHAPE_IDX)
        self.var = [0.0] * len(SHAPE_IDX)
        self.frames = 0
        self.fresh = 0
        self.baseline = 0.0
        self.settle = 0

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
        if self.baseline <= 0:
            self.baseline = sp
        elif self.settle < self.SETTLE_TICKS:
            self.baseline += self.BASELINE_SETTLE * (sp - self.baseline)
        elif sp < self.baseline:
            self.baseline += self.BASELINE_DOWN * (sp - self.baseline)
        else:
            self.baseline += self.BASELINE_UP * (sp - self.baseline)
        if self.settle < self.SETTLE_TICKS:
            self.settle += 1
            return math.nan
        return sp / self.baseline if self.baseline > 0 else math.nan


class Detector:
    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self.active = False

    def update(self, score: float) -> bool:
        if math.isnan(score):
            return self.active
        if not self.active and score >= self.threshold:
            self.active = True
        elif self.active and score < 0.75 * self.threshold:
            self.active = False
        return self.active


def percentile(values: list[float], p: float) -> float:
    if not values:
        return math.nan
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help=".wcsi files from csi_logger.py, any order")
    ap.add_argument("--threshold", type=float, default=2.0)
    ap.add_argument("--csv", help="write per-second scores here")
    args = ap.parse_args()

    packets = []
    for path in args.files:
        packets.extend(read_wcsi(path))
    packets.sort(key=lambda p: p[0])
    if not packets:
        print("no packets")
        return 1

    links: dict[tuple, LinkMotion] = defaultdict(LinkMotion)
    detectors: dict[tuple, Detector] = defaultdict(lambda: Detector(args.threshold))
    scores: dict[tuple, list[tuple[float, float, bool]]] = defaultdict(list)
    next_tick = packets[0][0] + 1.0
    for t, pkt in packets:
        while t >= next_tick:
            for key, link in links.items():
                score = link.tick()
                scores[key].append((next_tick, score, detectors[key].update(score)))
            next_tick += 1.0
        if len(pkt) < HEADER.size:
            continue
        f = HEADER.unpack_from(pkt)
        if f[0] != b"WISP" or f[2] != 1:
            continue
        node, src, header_len, n = f[5].hex(":"), f[6].hex(":"), f[3], f[16]
        links[(node, src)].add_frame(pkt[header_len:header_len + n])

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
        for t0, t1, peak in events[:15]:
            print(f"   {time.strftime('%H:%M:%S', time.localtime(t0))} for {t1 - t0 + 1:4.0f} s, peak {peak:.2f}")
        if len(events) > 15:
            print(f"   ... {len(events) - 15} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
