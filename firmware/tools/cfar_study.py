#!/usr/bin/env python3
"""Per-link motion thresholds from each link's own quiet scores (CFAR, as a radar sets each
cell's threshold from its own noise), offline.

    .venv/bin/python firmware/tools/cfar_study.py data/wisp-*-20261005-1[7-9].wcsi data/wisp-*-20261005-2?.wcsi

Reads the nodes' own link reports (csi_logger.py records them): the firmware's score and motion
flag of every link, once a second. Works out the hive's confirmation from the flags at 1 s
resolution (core_confirm.h; within 5 link-seconds of what firmware 0.1.6 reported), then prints:
  1. per link, on the EMPTY windows: score percentiles, how often it flags at 2.0, and the
     threshold that would give each target false alarm rate on those same seconds;
  2. the firmware's adaptive threshold (QuietThreshold in core_link_motion.h, mirrored below),
     replayed second by second over the whole recording for several false alarm rates and
     margins: flags with nobody there, and what is left while sitting, at the desk and walking;
  3. the chosen setting per link.
WINDOWS holds the labelled stretches of the owner's recording of 2026-10-05 (local time); edit
it for other recordings. Needs numpy.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import math
from pathlib import Path
import sys
import time

import numpy as np

from csi_logger import read_wcsi

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "wisp"))
from engine.protocol import LinkReport, ProtocolError, parse_packet  # noqa: E402

DAY = (2026, 10, 5)
WINDOWS = {
    "empty": [("22:09:02", "22:12:20"), ("22:29:00", "22:36:50")],  # owner upstairs
    "sitting": [("22:04:00", "22:05:40")],  # sitting quietly in the play room
    "desk": [("21:23:00", "21:31:00"), ("21:49:00", "21:58:00"), ("22:06:05", "22:07:45"), ("22:20:40", "22:24:00")],
    "walking": [("22:03:41", "22:03:56"), ("22:05:44", "22:06:00")],
}
USER = 2.0  # the nodes' Motion threshold that day

# Mirror of QuietThreshold (core_link_motion.h)
QUIET_BINS = 32
QUIET_LO = 1.0
QUIET_HI = 8.0
QUIET_SECONDS = 1200.0
QUIET_WARMUP = 180.0
QUIET_HOLD_S = 10


class QuietThreshold:
    """Decaying log-spaced histogram of a link's quiet scores; its threshold sits a margin above
    the score exceeded by a share pfa of them, never below the user's."""

    def __init__(self, pfa: float, margin: float) -> None:
        self.pfa, self.margin = pfa, margin
        self.log_ratio = math.log(QUIET_HI / QUIET_LO) / QUIET_BINS
        self.bins = [0.0] * QUIET_BINS
        self.total = 0.0

    def learn(self, score: float) -> None:
        keep = 1.0 - 1.0 / QUIET_SECONDS
        self.bins = [c * keep for c in self.bins]
        self.total = self.total * keep + 1.0
        b = 0 if not score > QUIET_LO else min(QUIET_BINS - 1, int(math.log(score / QUIET_LO) / self.log_ratio))
        self.bins[b] += 1.0

    def quantile(self) -> float:
        want = self.pfa * self.total
        above = 0.0
        for b in range(QUIET_BINS - 1, -1, -1):
            if self.bins[b] > 0 and above + self.bins[b] >= want:
                return QUIET_LO * math.exp((b + 1 - (want - above) / self.bins[b]) * self.log_ratio)
            above += self.bins[b]
        return QUIET_LO

    def threshold(self, user: float) -> float:
        if self.total < QUIET_WARMUP:
            return user
        return max(user, self.quantile() * self.margin)


def at(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return int(time.mktime((*DAY, h, m, s, 0, 0, -1)))


def load(files: list[str]) -> dict[tuple[str, str], dict[int, tuple[float, bool]]]:
    """(receiver, transmitter): {second: (score, motion flag)} from the link reports."""
    out: dict[tuple[str, str], dict[int, tuple[float, bool]]] = defaultdict(dict)
    for path in files:
        for t, pkt in read_wcsi(path):
            if len(pkt) < 24 or pkt[5] != 2:
                continue
            try:
                report = parse_packet(pkt)
            except ProtocolError:
                continue
            if isinstance(report, LinkReport):
                for link in report.links:
                    if link.score is not None:
                        out[(report.node, link.transmitter)][int(t)] = (link.score, link.motion)
    return out


class Floor:
    """Per-second scores of every link, and the 1 s port of the hive's confirmation."""

    def __init__(self, reports: dict) -> None:
        busy = [k for k, rows in reports.items() if len(rows) > 0.5 * max(len(r) for r in reports.values())]
        self.links = sorted(busy)
        self.nodes = sorted({rx for rx, _ in self.links})
        self.t0 = min(min(reports[k]) for k in self.links)
        n = max(max(reports[k]) for k in self.links) - self.t0 + 1
        self.scores = np.full((len(self.links), n), np.nan)
        for i, k in enumerate(self.links):
            for s, (score, _) in reports[k].items():
                self.scores[i, s - self.t0] = score
        self.index = {k: i for i, k in enumerate(self.links)}
        self.pairs = [(a, b) for i, a in enumerate(self.nodes) for b in self.nodes[i + 1:]
                      if (a, b) in self.index and (b, a) in self.index]

    def mask(self, name: str) -> np.ndarray:
        m = np.zeros(self.scores.shape[1], bool)
        for a, b in WINDOWS[name]:
            m[max(0, at(a) - self.t0):max(0, at(b) - self.t0)] = True
        return m

    def run(self, pfa: float | None, margin: float = 1.0):
        """Flags, thresholds and confirmed seconds, second by second; pfa None: the user's threshold."""
        n_links, n = self.scores.shape
        quiet = [QuietThreshold(pfa or 0.0, margin) for _ in self.links]
        flags = np.zeros((n_links, n), bool)
        thresholds = np.full((n_links, n), USER)
        confirmed = np.zeros(n, bool)
        active = [False] * n_links
        last_confirmed = -10**9
        for t in range(n):
            for i in range(n_links):
                th = thresholds[i, t] = quiet[i].threshold(USER) if pfa else USER
                s = self.scores[i, t]
                if math.isnan(s):
                    active[i] = False
                elif not active[i] and s >= th:
                    active[i] = True
                elif active[i] and s < 1 + 0.5 * (th - 1):
                    active[i] = False
                flags[i, t] = active[i]
            recent = flags[:, max(0, t - 2):t + 1].any(1)
            for a, b in self.pairs:
                ab, ba = self.index[(a, b)], self.index[(b, a)]
                if not ((active[ab] and recent[ba]) or (active[ba] and recent[ab])):
                    continue
                if any(recent[self.index[k]] for c in self.nodes if c not in (a, b)
                       for k in ((c, a), (a, c), (c, b), (b, c)) if k in self.index):
                    confirmed[t] = True
            if confirmed[t]:
                last_confirmed = t
            if pfa is None or t - last_confirmed <= QUIET_HOLD_S:
                continue
            for i in range(n_links):
                s = self.scores[i, t]
                if not math.isnan(s) and not active[i]:  # the link's own flagged seconds never count
                    quiet[i].learn(s)
        return flags, thresholds, confirmed


def name(mac: str, nodes: list[str]) -> str:
    return mac.replace(":", "")[6:10] if mac in nodes else "AP"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help=".wcsi files from csi_logger.py, any order")
    ap.add_argument("--pfa", type=float, nargs="+", default=[0.002, 0.005, 0.01])
    ap.add_argument("--margin", type=float, nargs="+", default=[1.0, 1.1, 1.2])
    ap.add_argument("--chosen", type=float, nargs=2, default=[0.005, 1.1], metavar=("PFA", "MARGIN"))
    args = ap.parse_args()

    floor = Floor(load(args.files))
    nodes = floor.nodes
    empty = floor.mask("empty")
    print(f"{len(floor.links)} links, {floor.scores.shape[1]} s; EMPTY {empty.sum()} s\n")
    flags, _, confirmed = floor.run(None)
    print("1. EMPTY windows per link: score percentiles, share of seconds flagged at 2.0, and the")
    print("   threshold giving each false alarm rate there (at least 2.0)")
    print(f"{'link':12s} {'p50':>5s} {'p99':>5s} {'max':>5s} {'flag 2.0':>8s}" + "".join(f"  thr {p:.1%}" for p in args.pfa))
    for i, (rx, tx) in enumerate(floor.links):
        v = floor.scores[i, empty]
        v = v[~np.isnan(v)]
        q = [max(USER, float(np.quantile(v, 1 - p))) for p in args.pfa]
        print(f"{name(rx, nodes)}<-{name(tx, nodes):6s} {np.median(v):5.2f} {np.quantile(v, .99):5.2f} {v.max():5.2f} "
              f"{flags[i, empty].mean():8.2%}" + "".join(f"  {x:8.2f}" for x in q))

    def line(tag: str, flags: np.ndarray, confirmed: np.ndarray) -> str:
        out = f"{tag:22s} {flags[:, empty].mean():6.2%} {flags[:, empty].any(0).mean():6.1%} {confirmed[empty].mean():5.1%}"
        for w in ("sitting", "desk", "walking"):
            m = floor.mask(w)
            out += f" | {flags[:, m].any(0).mean():4.0%} {confirmed[m].mean():4.0%}"
        return out

    print("\n2. Replayed over the recording. EMPTY: share of link-seconds flagged, seconds with any link")
    print("   flagged, seconds with a confirmed pair; then any link flagged / confirmed while sitting, desk, walking")
    print(f"{'':22s} {'link':>6s} {'any':>6s} {'conf':>5s} | sitting   | desk      | walking")
    print(line("fixed 2.0", flags, confirmed))
    chosen = None
    for pfa in args.pfa:
        for margin in args.margin:
            result = floor.run(pfa, margin)
            print(line(f"pfa {pfa:.1%} margin {margin}", result[0], result[2]), flush=True)
            if [pfa, margin] == args.chosen:
                chosen = result
    if chosen is None:
        chosen = floor.run(*args.chosen)
    flags_c, thresholds, _ = chosen
    print(f"\n3. pfa {args.chosen[0]:.1%}, margin {args.chosen[1]}: per link, threshold in the EMPTY windows and flagged share")
    for i, (rx, tx) in enumerate(floor.links):
        th = thresholds[i, empty]
        print(f"{name(rx, nodes)}<-{name(tx, nodes):6s} threshold {th.min():.2f} to {th.max():.2f}, "
              f"flagged {flags[i, empty].mean():6.2%} at 2.0, {flags_c[i, empty].mean():6.2%} now")
    return 0


if __name__ == "__main__":
    sys.exit(main())
