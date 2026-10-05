#!/usr/bin/env python3
"""Replay recorded CSI through the integration's position engine: when the map would show
someone moving, and where. Over quiet hours, every fix is a phantom.

    python firmware/tools/replay_floor.py data/*-20261005-0[2-5].wcsi --hive wisp-a8c77c.local --since 02:11

Scores per second come from replay.py (the firmware's motion score). Positions come from one
live hive report (--hive, any node), as Home Assistant draws them without a floor plan. Each
second the scores go through FloorModel as presence.py does, once per --min-disturbance value:
with the gate presence.py uses (a link reporting motion, the firmware's detector at --threshold)
and with the sum of the scores alone, as before that gate.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import socket
import sys
import time

from replay import Detector, load, seconds

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "wisp"))
from engine.floor import FloorModel  # noqa: E402
from engine.hive import HiveState, HiveTracker  # noqa: E402
from engine.protocol import STREAM_HIVE_REPORTS, HiveReport, build_subscribe, parse_packet  # noqa: E402


def fetch_hive(host: str, port: int, wait: float = 15.0) -> HiveState | None:
    """The first in-sync hive report from a node (they come every 5 s)."""
    ip = socket.gethostbyname(host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    sock.settimeout(1.0)
    tracker = HiveTracker(timeout=30)
    end = time.time() + wait
    try:
        while time.time() < end:
            sock.sendto(build_subscribe(STREAM_HIVE_REPORTS), (ip, port))
            try:
                data, _ = sock.recvfrom(8192)
            except socket.timeout:
                continue
            report = parse_packet(data)
            if isinstance(report, HiveReport) and report.in_sync:
                tracker.apply(report, time.time())
                return tracker.current(time.time())
    finally:
        sock.close()
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help=".wcsi files from csi_logger.py, any order")
    ap.add_argument("--hive", required=True, help="a node to take the hive's layout from")
    ap.add_argument("--port", type=int, default=47010)
    ap.add_argument("--since", help="only packets from this local time on, HH:MM")
    ap.add_argument("--threshold", type=float, default=2.0, help="the nodes' motion threshold")
    ap.add_argument("--nodes", help="only these nodes (comma separated MACs), as one floor of a house")
    ap.add_argument("--min-disturbance", type=float, nargs="+", default=[0.3, 0.5, 0.8, 1.2],
                    help="sum of log scores below which nobody is moving (FloorModel's default: 0.3)")
    args = ap.parse_args()

    hive = fetch_hive(args.hive, args.port)
    if hive is None:
        print(f"no in-sync hive report from {args.hive}")
        return 1
    packets = load(args.files, args.since)
    if not packets:
        print("no packets")
        return 1
    models = {(m, gated): FloorModel(min_disturbance=m) for m in args.min_disturbance for gated in (True, False)}
    detectors: dict[tuple, Detector] = {}
    floor_nodes = set(args.nodes.lower().split(",")) if args.nodes else None
    for model in models.values():
        model.set_layout(hive, nodes=floor_nodes)
    print("positions (m): " + ", ".join(f"{mac[-8:]} ({x:.1f}, {y:.1f})" for mac, (x, y) in
                                       sorted(next(iter(models.values())).positions.items())))

    fixes: dict[tuple, list[tuple[float, float, float, float]]] = {key: [] for key in models}
    raw: dict[tuple, list[tuple[float, float, float]]] = {key: [] for key in models}
    total = 0
    disturbance = []
    for t, tick in seconds(packets):
        total += 1
        scores = {(tx, rx): s for (rx, tx), s in tick.items() if not math.isnan(s)}
        moving = {(tx, rx) for (rx, tx), s in tick.items()
                  if detectors.setdefault((rx, tx), Detector(args.threshold)).update(s)}
        disturbance.append(sum(math.log(max(s, 1.0)) for s in scores.values()))
        for (m, gated), model in models.items():
            if (fix := model.update(scores, t, moving if gated else None)) is not None:
                fixes[(m, gated)].append((t, fix.x, fix.y, fix.quality))
                raw[(m, gated)].append((t, fix.raw_x, fix.raw_y))

    if not total:
        print("less than a second of packets")
        return 1
    start, end = packets[0][0], packets[-1][0]
    print(f"{time.strftime('%H:%M', time.localtime(start))} to {time.strftime('%H:%M', time.localtime(end))}, "
          f"{total} s; sum of log scores p50 {sorted(disturbance)[len(disturbance) // 2]:.3f}, "
          f"p99 {sorted(disturbance)[int(0.99 * len(disturbance))]:.3f}, max {max(disturbance):.3f}")
    print(f"{'min_disturbance':>16s} {'gate':>12s} {'fix s':>7s} {'share':>7s} {'stretches':>10s} {'longest s':>10s}")
    for (m, gated), rows in fixes.items():
        stretches, longest, run, last = 0, 0, 0, None
        for t, *_ in rows:
            run = run + 1 if last is not None and t - last <= 1.01 else 1
            stretches += run == 1
            longest = max(longest, run)
            last = t
        print(f"{m:16.2f} {'link motion' if gated else 'sum only':>12s} {len(rows):7d} {len(rows) / max(total, 1):7.2%} "
              f"{stretches:10d} {longest:10d}")
    default = (min(args.min_disturbance), True)
    # How far the position moves from one second to the next, while someone moves: a person walks
    # about 1 m a second, so longer steps are noise the map shows as jumps.
    for label, rows in (("smoothed", [(t, x, y) for t, x, y, _ in fixes[default]]), ("raw fit", raw[default])):
        steps = [math.dist(a[1:], b[1:]) for a, b in zip(rows, rows[1:]) if b[0] - a[0] <= 1.01]
        if steps:
            st = sorted(steps)
            print(f"{label:9s} steps a second: p50 {st[len(st) // 2]:.2f} m, p90 {st[int(0.9 * len(st))]:.2f} m, "
                  f"max {st[-1]:.2f} m, over 1.5 m {sum(x > 1.5 for x in st) / len(st):.0%} of {len(st)}")
    for t, x, y, q in fixes[default][:20]:
        print(f"   {time.strftime('%H:%M:%S', time.localtime(t))} at ({x:5.1f}, {y:5.1f}), quality {q:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
