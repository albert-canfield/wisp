#!/usr/bin/env python3
"""Soak test: watch every node for hours, optionally restart them in turn, and report stability.

    python firmware/tools/soak.py wisp-a8c77c.local wisp-8d5858.local wisp-d71430.local --hours 12
    python firmware/tools/soak.py <nodes...> --hours 6 --restart-every 20 --csv data/soak.csv

Every --interval seconds it reads each node's web page (ESPHome web_server): uptime, free memory,
largest free block, grid nodes, hive in sync, access point CSI rate, dropped CSI, reset reason.
With --restart-every N (minutes) it restarts one node at a time, round robin, through its Restart
button, and times how long until every node sees the whole grid again and the hive is back in
sync. At the end (or on Ctrl-C) it prints per node: resets asked for and unexpected, memory low
and trend, seconds out of the grid or out of sync, and the recovery times.
"""

from __future__ import annotations

import argparse
import csv
import json
import socket
import sys
import time
import urllib.parse
import urllib.request

FIELDS = {
    "uptime": "sensor/Uptime",
    "free": "sensor/Free memory",
    "block": "sensor/Heap max block",
    "grid": "sensor/Grid nodes",
    "sync": "binary_sensor/Hive in sync",
    "csi": "sensor/AP CSI rate",
    "dropped": "sensor/CSI dropped",
    "reset": "text_sensor/Reset reason",
}


def read(ip: str, path: str) -> str | float | None:
    try:
        with urllib.request.urlopen(f"http://{ip}/{urllib.parse.quote(path)}", timeout=4) as r:
            value = json.load(r).get("value")
    except (OSError, ValueError):
        return None
    return value


def press(ip: str, button: str) -> bool:
    req = urllib.request.Request(f"http://{ip}/button/{urllib.parse.quote(button)}/press", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status == 200
    except OSError:
        return False


class Node:
    def __init__(self, name: str) -> None:
        self.name = name if name.replace(".", "").isdigit() else name.split(".")[0]
        self.host = name
        self.ip: str | None = None
        self.last_uptime: float | None = None
        self.resets_asked = 0
        self.resets_unexpected: list[str] = []
        self.expect_reset_until = 0.0
        self.free: list[tuple[float, float]] = []
        self.block_low: float | None = None
        self.out_of_grid = 0.0
        self.out_of_sync = 0.0
        self.unreachable = 0.0
        self.dropped_first: float | None = None
        self.dropped_last: float | None = None

    def resolve(self) -> str | None:
        try:
            self.ip = socket.gethostbyname(self.host)
        except OSError:
            pass
        return self.ip


def fit_slope(points: list[tuple[float, float]]) -> float:
    """Least squares slope, units per hour."""
    if len(points) < 3:
        return 0.0
    n = len(points)
    mx = sum(t for t, _ in points) / n
    my = sum(v for _, v in points) / n
    den = sum((t - mx) ** 2 for t, _ in points)
    return 0.0 if den == 0 else sum((t - mx) * (v - my) for t, v in points) / den * 3600


def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100 * len(s)))] if s else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nodes", nargs="+", help="node host names or IPs")
    ap.add_argument("--hours", type=float, default=12)
    ap.add_argument("--interval", type=float, default=30, help="seconds between readings")
    ap.add_argument("--restart-every", type=float, default=0, help="minutes between restarts (0: never)")
    ap.add_argument("--csv", help="write every reading here")
    args = ap.parse_args()

    nodes = [Node(n) for n in args.nodes]
    for node in nodes:
        if node.resolve() is None:
            print(f"cannot resolve {node.host}")
            return 1
    out = None
    if args.csv:
        out = csv.writer(open(args.csv, "a", newline=""))
        out.writerow(["time", "node", *FIELDS])
    start = time.time()
    end = start + args.hours * 3600
    next_restart = start + args.restart_every * 60 if args.restart_every else float("inf")
    turn = 0
    recovering: tuple[Node, float] | None = None  # node restarted, when
    recoveries: list[tuple[str, float]] = []
    want = len(nodes)
    print(f"soak: {len(nodes)} nodes for {args.hours} h, readings every {args.interval:.0f} s"
          + (f", a restart every {args.restart_every:.0f} min" if args.restart_every else ""), flush=True)
    try:
        while time.time() < end:
            now = time.time()
            whole = True
            for node in nodes:
                values = {k: read(node.ip, path) for k, path in FIELDS.items()}
                if out:
                    out.writerow([f"{now:.0f}", node.name, *values.values()])
                if values["uptime"] is None:
                    node.unreachable += args.interval
                    node.resolve()
                    whole = False
                    continue
                uptime = float(values["uptime"])
                if node.last_uptime is not None and uptime + args.interval < node.last_uptime:
                    if now <= node.expect_reset_until:
                        node.resets_asked += 1
                    else:
                        node.resets_unexpected.append(f"{time.strftime('%H:%M:%S')} {values['reset']}")
                        print(f"{time.strftime('%H:%M:%S')} {node.name}: UNEXPECTED RESET ({values['reset']})", flush=True)
                node.last_uptime = uptime
                if values["free"] is not None and uptime > 120:
                    node.free.append((now, float(values["free"])))
                if values["block"] is not None:
                    b = float(values["block"])
                    node.block_low = b if node.block_low is None else min(node.block_low, b)
                if values["grid"] is not None and float(values["grid"]) < want:
                    node.out_of_grid += args.interval
                    whole = False
                if values["sync"] is not None and not values["sync"]:
                    node.out_of_sync += args.interval
                    whole = False
                if values["dropped"] is not None:
                    d = float(values["dropped"])
                    node.dropped_first = d if node.dropped_first is None else node.dropped_first
                    node.dropped_last = d
            if recovering and whole:
                who, at = recovering
                recoveries.append((who.name, now - at))
                print(f"{time.strftime('%H:%M:%S')} {who.name}: grid whole and hive in sync {now - at:.0f} s after its restart", flush=True)
                recovering = None
            if now >= next_restart and recovering is None:
                node = nodes[turn % len(nodes)]
                turn += 1
                node.expect_reset_until = now + 120
                ok = press(node.ip, "Restart")
                print(f"{time.strftime('%H:%M:%S')} {node.name}: restart {'sent' if ok else 'FAILED'}", flush=True)
                if ok:
                    recovering = (node, now)
                next_restart = now + args.restart_every * 60
            time.sleep(max(0.0, args.interval - (time.time() - now)))
    except KeyboardInterrupt:
        pass

    hours = (time.time() - start) / 3600
    print(f"\nsoak over {hours:.1f} h")
    for node in nodes:
        lows = [v for _, v in node.free]
        dropped = 0 if node.dropped_first is None else node.dropped_last - node.dropped_first
        print(f"{node.name}: resets asked {node.resets_asked}, unexpected {len(node.resets_unexpected)}; "
              f"free memory low {min(lows, default=float('nan')):.0f} B, trend {fit_slope(node.free):+.0f} B/h; "
              f"largest block low {node.block_low if node.block_low is not None else float('nan'):.0f} B; "
              f"out of the grid {node.out_of_grid:.0f} s, out of sync {node.out_of_sync:.0f} s, "
              f"unreachable {node.unreachable:.0f} s; CSI dropped {dropped:.0f}")
        for line in node.resets_unexpected:
            print(f"   unexpected reset {line}")
    if recoveries:
        times = [t for _, t in recoveries]
        print(f"recoveries after a restart: {len(times)}, p50 {percentile(times, 50):.0f} s, max {max(times):.0f} s "
              f"(readings every {args.interval:.0f} s)")
    bad = any(n.resets_unexpected for n in nodes)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
