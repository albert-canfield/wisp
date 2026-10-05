#!/usr/bin/env python3
"""Breathing on the links, offline: is the chest rising (0.15 to 0.6 Hz) visible on the CSI of
someone sitting still, against an empty floor?

    .venv/bin/python firmware/tools/breathing_study.py data/wisp-*-20261005-0[234].wcsi data/wisp-*-20261005-2[12].wcsi

Takes the raw CSI and the link reports (motion flags) that csi_logger.py recorded, for the
labelled stretches in WINDOWS (owner's recording of 2026-10-05, local time; edit for others).
Per link, a scalar series at 8 Hz in 32 s windows, linearly detrended (quadratically for the
first table), Hann-windowed; its power in the breathing band (0.15 to 0.6 Hz) against the
median of 0.7 to 2.0 Hz. Prints:
  1. the band ratio (peak over that median) per link and stretch, for four scalar series: the
     mean amplitude, the first principal component of the window's gain-normalised shape, the
     shape's distance from its window mean, and the firmware's projection (below);
  2. the firmware's detector (core_breathing.h, mirrored here): share of motion-free
     evaluations flagged as breathing per stretch, for several ratio thresholds, and per link;
  3. its evaluations while sitting, second by second, with the rate per link.
The firmware's projection: the shape in 17 groups of 3 subcarriers, averaged per 125 ms bin, its
deviation from a slow mean, projected on its principal direction followed by Oja's rule.
Needs numpy.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import math
from pathlib import Path
import struct
import sys
import time

import numpy as np

from csi_logger import read_wcsi

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "wisp"))
from engine.protocol import LinkReport, ProtocolError, parse_packet  # noqa: E402

DAY = (2026, 10, 5)
WINDOWS = {
    "empty": [("22:09:02", "22:12:20"), ("22:29:00", "22:36:50")],  # owner upstairs
    "night": [("02:00:00", "05:00:00")],  # asleep upstairs; two nodes then
    "sitting": [("22:04:00", "22:05:40")],  # sitting quietly in the play room
    "desk": [("21:23:00", "21:31:00"), ("21:49:00", "21:58:00"), ("22:06:05", "22:07:45"), ("22:20:40", "22:24:00")],
}
LEAD = 120  # s of CSI before each stretch, so the firmware's state has settled
HEADER = struct.Struct("<4sBBHI6s6sIbbBBBBBBH")
SHAPE_IDX = list(range(2, 27)) + list(range(38, 64))  # as lltf_shape()

# Mirror of core_breathing.h
FS = 8.0  # bins a second (125 ms)
N = 256  # 32 s ring
EVAL_S = 4.0
GROUP = 3  # adjacent subcarriers averaged: 17 values
SLOW = 0.02  # slow mean, per bin: about 6 s
ETA = 0.05  # Oja step, per bin
VAR_RATE = 0.05
GAP_BINS = 40  # 5 s without frames starts the ring over
BAND = range(5, 20)  # DFT bins of 1/32 Hz: 0.16 to 0.59 Hz
REF = range(23, 65)  # 0.72 to 2.0 Hz
RATIO = 24.0
PEAK_STEP = 2  # consecutive peaks within 2 bins (0.0625 Hz)


def at(hms: str) -> float:
    h, m, s = (int(x) for x in hms.split(":"))
    return time.mktime((*DAY, h, m, s, 0, 0, -1))


def spans() -> list[tuple[float, float]]:
    return [(at(a) - LEAD, at(b)) for w in WINDOWS.values() for a, b in w]


def load(files: list[str]):
    """Raw CSI per link inside the stretches: times and gain-normalised shapes; motion flags per second."""
    keep = spans()
    frames: dict[tuple[str, str], list] = defaultdict(list)
    flags: dict[tuple[str, str], dict[int, bool]] = defaultdict(dict)
    for path in files:
        for t, pkt in read_wcsi(path):
            if len(pkt) < 24 or not any(a <= t < b for a, b in keep):
                continue
            if pkt[5] == 2:
                try:
                    report = parse_packet(pkt)
                except ProtocolError:
                    continue
                if isinstance(report, LinkReport):
                    for link in report.links:
                        flags[(report.node, link.transmitter)][int(t)] = link.motion
                continue
            if pkt[5] != 1 or len(pkt) < HEADER.size:
                continue
            f = HEADER.unpack_from(pkt)
            if f[16] < 128:
                continue
            raw = np.frombuffer(pkt, np.int8, 128, f[3]).astype(np.float32)
            amp = np.hypot(raw[1::2], raw[0::2])[SHAPE_IDX]
            if amp.sum() > 0:
                frames[(f[5].hex(":"), f[6].hex(":"))].append((t, amp))
    out = {}
    for key, rows in frames.items():
        rows.sort(key=lambda r: r[0])
        amp = np.stack([r[1] for r in rows])
        out[key] = (np.array([r[0] for r in rows]), amp, amp / amp.mean(1, keepdims=True))
    return out, flags


class Breathing:
    """One link of the firmware's detector."""

    def __init__(self) -> None:
        self.acc = np.zeros(17)
        self.count = 0
        self.bin = None
        self.mean = self.w = None
        self.var = 0.0
        self.last = 0.0
        self.ring: list[float] = []

    def frame(self, t: float, shape: np.ndarray) -> None:
        b = int(t * FS)
        if self.bin is None:
            self.bin = b
        if b != self.bin:
            self._close(b)
        self.acc = self.acc + shape[:51].reshape(17, GROUP).mean(1)
        self.count += 1

    def _close(self, b: int) -> None:
        if self.count:
            x0 = self.acc / self.count
            if self.mean is None:
                self.mean, self.w = x0.copy(), np.full(17, 1 / math.sqrt(17))
            x = x0 - self.mean
            self.mean += SLOW * x
            y = float(self.w @ x)
            v = float(x @ x)
            self.var = v if self.var == 0 else self.var + VAR_RATE * (v - self.var)
            if self.var > 0:
                self.w = self.w + ETA * y * (x - y * self.w) / self.var
                self.w /= np.linalg.norm(self.w)
            self.last = float(self.w @ x)
        gap = b - self.bin
        self.ring = [] if gap > GAP_BINS else (self.ring + [self.last] * min(gap, N))[-N:]
        self.bin, self.acc, self.count = b, np.zeros(17), 0

    def evaluate(self) -> tuple[float, int, bool] | None:
        """Band peak over the reference median, its bin, and whether it is a local maximum."""
        if len(self.ring) < N:
            return None
        return spectrum_ratio(np.array(self.ring), linear=True)


def spectrum_ratio(y: np.ndarray, linear: bool = False) -> tuple[float, int, bool]:
    x = np.arange(len(y))
    y = (y - np.polyval(np.polyfit(x, y, 1 if linear else 2), x)) * np.hanning(len(y))
    p = np.abs(np.fft.rfft(y)) ** 2
    scale = len(y) / N  # bins of 1/32 Hz whatever the length
    band = [round(k * scale) for k in BAND]
    pk = max(band, key=lambda k: p[k])
    ref = float(np.median([p[round(k * scale)] for k in REF]))
    return (p[pk] / ref if ref > 0 else 0.0), round(pk / scale), bool(p[pk] >= p[pk - 1] and p[pk] >= p[pk + 1])


def detector(evaluations, ratio: float = RATIO):
    """On after two positive evaluations in a row with peaks within PEAK_STEP bins, off after two
    negative ones; an evaluation with motion in its window turns it off and starts over."""
    on, pos, neg, prev, out = False, 0, 0, None, []
    for t, r, peak, local, moved in evaluations:
        if moved:
            on, pos, neg, prev = False, 0, 0, None
            out.append((t, False, True, peak))
            continue
        hit = r >= ratio and local
        if hit:
            pos = pos + 1 if pos and abs(peak - prev) <= PEAK_STEP else 1
            neg = 0
        else:
            neg, pos = neg + 1, 0
        prev = peak if hit else None
        on = (on or pos >= 2) and not (on and neg >= 2)
        out.append((t, on, False, peak))
    return out


def run(times, shapes, flags, a, b):
    """Firmware evaluations over one stretch: (time, ratio, peak bin, local max, motion in window)."""
    br = Breathing()
    out, next_eval = [], a - LEAD + EVAL_S
    for t, shape in zip(times, shapes):
        while t >= next_eval:
            ev = br.evaluate()
            if ev is not None and next_eval >= a:
                moved = any(flags.get(s, False) for s in range(int(next_eval - N / FS), int(next_eval) + 1))
                out.append((next_eval, *ev, moved))
            next_eval += EVAL_S
        br.frame(t, shape)
    return out


def label(mac: str, nodes: set[str]) -> str:
    return mac.replace(":", "")[6:10] if mac in nodes else "AP" + mac[-2:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help=".wcsi files from csi_logger.py, any order")
    ap.add_argument("--ratios", type=float, nargs="+", default=[12, 16, 20, 24, 32])
    args = ap.parse_args()
    links, flags = load(args.files)
    nodes = {rx for rx, _ in links}

    def name(key):
        return f"{label(key[0], nodes)}<-{label(key[1], nodes):6s}"

    print("1. Band ratio, median and 90th percentile per 32 s window (step 8 s)")
    for kind in ("mean", "pc1", "dist", "firmware"):
        print(f"   {kind}")
        for key in sorted(links):
            times, amp, shape = links[key]
            row = f"   {name(key)}"
            for w in ("empty", "sitting", "desk"):
                ratios = []
                for a0, b0 in ((at(a), at(b)) for a, b in WINDOWS[w]):
                    if kind == "firmware":
                        m = (times >= a0 - LEAD) & (times < b0)
                        evs = run(times[m], shape[m], flags.get(key, {}), a0, b0)
                        ratios += [e[1] for e in evs if e[0] - N / FS >= a0][1::2]  # windows inside the stretch
                        continue
                    for s in np.arange(a0, b0 - 32 + 1e-6, 8.0):
                        m = (times >= s) & (times < s + 32)
                        if m.sum() < 32 * FS * 0.5:
                            continue
                        bins = np.clip(((times[m] - s) * FS).astype(int), 0, N - 1)
                        cnt = np.bincount(bins, minlength=N)
                        if kind == "mean":
                            y = np.bincount(bins, amp[m].mean(1), N)
                        else:
                            sh = np.stack([np.bincount(bins, shape[m][:, j], N) for j in range(51)], 1)
                            y = sh / np.maximum(cnt, 1)[:, None]
                        good = cnt > 0
                        idx = np.arange(N)
                        if y.ndim == 1:
                            y = np.interp(idx, idx[good], y[good] / cnt[good])
                        else:
                            y = np.stack([np.interp(idx, idx[good], y[good, j]) for j in range(51)], 1)
                            y = y - y.mean(0)
                            y = y @ np.linalg.svd(y, full_matrices=False)[2][0] if kind == "pc1" else np.sqrt((y ** 2).sum(1))
                        ratios.append(spectrum_ratio(y)[0])
                if ratios:
                    row += f" | {w} {np.median(ratios):6.1f} {np.quantile(ratios, 0.9):6.1f}"
            if "|" in row:
                print(row)

    evaluations = {}
    for w, stretches in WINDOWS.items():
        for key, (times, _, shape) in links.items():
            for a, b in ((at(a), at(b)) for a, b in stretches):
                m = (times >= a - LEAD) & (times < b)
                if m.sum() > N:
                    evaluations.setdefault((w, key), []).extend(run(times[m], shape[m], flags.get(key, {}), a, b))

    print("\n2. Firmware detector: share of motion-free evaluations flagged as breathing (per link), and of")
    print("   evaluation times with any link breathing")
    for ratio in args.ratios:
        row = f"   ratio {ratio:4.0f}:"
        for w in WINDOWS:
            res = [x for (ww, _), ev in evaluations.items() if ww == w for x in detector(ev, ratio) if not x[2]]
            at_time = defaultdict(bool)
            for (ww, _), ev in evaluations.items():
                if ww == w:
                    for t, on, _, _ in detector(ev, ratio):
                        at_time[t] |= on
            row += f" {w} {np.mean([x[1] for x in res]):6.1%} of {len(res):5d}, any {np.mean(list(at_time.values())):4.0%} |"
        print(row)
    print(f"   per link at ratio {RATIO:.0f}:")
    for key in sorted(links):
        row = f"   {name(key)}"
        for w in WINDOWS:
            res = [x for x in detector(evaluations.get((w, key), [])) if not x[2]]
            row += f" | {w} {np.mean([x[1] for x in res]):4.0%} of {len(res):5d}" if res else f" | {w}   -"
        print(row)

    print("\n3. While sitting: links breathing at each evaluation, with their rate per minute (M: motion in the window)")
    at_time = defaultdict(list)
    for (w, key), ev in evaluations.items():
        if w == "sitting":
            for t, on, moved, peak in detector(ev):
                at_time[t].append((name(key).strip(), on, moved, peak))
    for t in sorted(at_time):
        free = sum(not m for _, _, m, _ in at_time[t])
        on = [f"{n}@{p * 60 / 32:.0f}" for n, o, _, p in at_time[t] if o]
        print(f"   {time.strftime('%H:%M:%S', time.localtime(t))} motion-free links {free:2d}, breathing {len(on)}: {' '.join(on)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
