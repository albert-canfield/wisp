#!/usr/bin/env python3
"""Guided walk test: how well does each link see you?

    python firmware/tools/walk_test.py wisp-a8c77c.local wisp-8d5858.local

It walks you through short phases (stay still, walk across a link, walk elsewhere, leave the
room), records every node's link reports labelled with the phase, and prints for each link the
motion score and the share of seconds it reported motion in each phase. Everything is saved as
CSV for later tuning (replay.py, or a spreadsheet). Press Enter to start each phase; Ctrl+C stops.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import os
import socket
import statistics
import struct
import sys
import threading
import time

PORT = 47010
SUBSCRIBE_LINKS = b"WSUB\x01\x02"
PHASES = [
    ("still", 30, "Stand or sit still, away from the nodes and the access point."),
    ("across", 30, "Walk back and forth between a node and the access point it is connected to."),
    ("between_nodes", 30, "Walk back and forth between two nodes."),
    ("elsewhere", 30, "Walk around the room, away from the straight lines between nodes."),
    ("away", 30, "Leave the room (or stand still far away)."),
]


class Collector(threading.Thread):
    """Subscribes to link reports and keeps per-second rows, labelled with the current phase."""

    def __init__(self, hosts: list[str]) -> None:
        super().__init__(daemon=True)
        self.hosts = hosts
        self.phase = "setup"
        self.rows: list[tuple] = []
        self.stop = False

    def run(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", 0))
        s.settimeout(0.3)
        ips: dict[str, str] = {}
        last_sub = 0.0
        while not self.stop:
            now = time.time()
            if now - last_sub > 2:
                for h in self.hosts:
                    try:
                        ips[h] = socket.gethostbyname(h)
                        s.sendto(SUBSCRIBE_LINKS, (ips[h], PORT))
                    except OSError:
                        pass
                last_sub = now
            try:
                data, (ip, _) = s.recvfrom(2048)
            except socket.timeout:
                continue
            if len(data) < 24 or data[:4] != b"WISP" or data[5] != 2:
                continue
            node = data[12:18].hex(":")
            for i in range(data[18]):
                tx, kind, rssi, score, spread, frames, flags = struct.unpack_from("<6sBbHHBB", data, 24 + 14 * i)
                self.rows.append((now, self.phase, node, tx.hex(":"), "ap" if kind == 0 else "node",
                                  None if score == 65535 else score / 100, spread / 100, rssi, frames, flags & 1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nodes", nargs="+", help="node hostnames or IPs")
    ap.add_argument("--out", default="data/walk-%Y%m%d-%H%M.csv")
    ap.add_argument("--seconds", type=int, default=0, help="override every phase length")
    args = ap.parse_args()

    collector = Collector(args.nodes)
    collector.start()
    print("Listening to the nodes. Wait for the scores to settle (20 s after a node starts).")
    try:
        for name, seconds, text in PHASES:
            seconds = args.seconds or seconds
            input(f"\nNext: {name} ({seconds} s). {text}\nPress Enter to start...")
            collector.phase = name
            end = time.time() + seconds
            while time.time() < end:
                print(f"\r  {name}: {end - time.time():4.0f} s left ", end="", flush=True)
                time.sleep(0.5)
            collector.phase = "between"
            print(f"\r  {name}: done            ")
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
    collector.stop = True
    time.sleep(0.5)

    path = time.strftime(args.out)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "phase", "receiver", "transmitter", "kind", "score", "spread", "rssi", "frames", "motion"])
        w.writerows(collector.rows)
    print(f"\nSaved {len(collector.rows)} link readings to {path}\n")

    # Per link and phase: median and high score, and the share of readings flagged as motion.
    stats: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for t, phase, rx, tx, kind, score, spread, rssi, frames, motion in collector.rows:
        if phase in ("setup", "between") or score is None:
            continue
        stats[(rx, tx, kind)][phase].append((score, motion))
    names = [p[0] for p in PHASES]
    print(f"{'link':44s}" + "".join(f"{n:>18s}" for n in names))
    for (rx, tx, kind), phases in sorted(stats.items()):
        cells = []
        for n in names:
            vals = phases.get(n, [])
            if not vals:
                cells.append(f"{'-':>18s}")
                continue
            scores = [s for s, _ in vals]
            share = 100 * sum(m for _, m in vals) / len(vals)
            cells.append(f"{statistics.median(scores):5.2f}/{max(scores):5.2f} {share:3.0f}%".rjust(18))
        print(f"{rx[-8:]} <- {kind:4s} {tx:17s}    " + "".join(cells))
    print("\nEach cell: median score / highest score, then the share of readings with motion on.")
    print("Good: 'still' and 'away' near 1 with 0%, 'across' high with a large share.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
