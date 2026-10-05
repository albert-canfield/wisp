#!/usr/bin/env python3
"""Live view of a Wisp grid: who hears whom, the hive and the layout, from the nodes themselves.

    python firmware/tools/monitor.py 192.168.10.76 192.168.10.75 192.168.10.77
    python firmware/tools/monitor.py <nodes...> --heal 192.168.10.77    # self-healing test

Every --interval seconds it prints, per node, its ESPHome readings (Grid nodes, Hive in sync,
grid channel, access point CSI rate) and, from its UDP reports, every link it receives (CSI
frames a second, RSSI, motion score), then each node's layout and whether they all agree.
Events print as they happen: a link lost or found, the grid count, hive sync or hash changing,
a node restarting or out of reach.

--heal NODE (repeatable) restarts each given node in turn through its Restart button, once the
grid is whole, and times: when the others stop hearing it, when they hear it again, when every
node counts the whole grid again, and when every hive is in sync with the same hash.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "custom_components" / "wisp"))
from engine.protocol import (  # noqa: E402
    STREAM_HIVE_REPORTS,
    STREAM_LINK_REPORTS,
    HiveReport,
    LinkReport,
    ProtocolError,
    build_subscribe,
    parse_packet,
)

PORT = 47010
SENSORS = {
    "grid": "sensor/Grid nodes",
    "sync": "binary_sensor/Hive in sync",
    "channel": "sensor/Grid channel",
    "csi": "sensor/AP CSI rate",
    "uptime": "sensor/Uptime",
}
LINK_GONE_S = 3.0  # a link with no frames this long counts as lost


def web(ip: str, path: str, timeout: float = 3):
    try:
        with urllib.request.urlopen(f"http://{ip}/{urllib.parse.quote(path)}", timeout=timeout) as r:
            return json.load(r).get("value")
    except (OSError, ValueError):
        return None


def press(ip: str, button: str) -> bool:
    req = urllib.request.Request(f"http://{ip}/button/{urllib.parse.quote(button)}/press", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


class Grid:
    def __init__(self, ips: list[str], poll: float = 0.3) -> None:
        self.poll = poll
        self.ips = ips
        self.mac: dict[str, str] = {}  # ip -> MAC
        self.name: dict[str, str] = {}  # MAC -> short name
        for ip in ips:
            mac = (web(ip, "text_sensor/MAC address") or "").lower()
            if mac:
                self.mac[ip] = mac
                self.name[mac] = "wisp-" + mac.replace(":", "")[-6:]
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("", 0))
        self.sock.settimeout(0.2)
        self.last_sub = 0.0
        self.links: dict[tuple[str, str], dict] = {}  # (rx, tx) -> latest
        self.hives: dict[str, HiveReport] = {}
        self.readings: dict[str, dict] = {ip: {} for ip in ips}
        self.start = time.time()
        self.lock = threading.Lock()
        # One poller per node, so a node that is down never holds up the others or the reports.
        for ip in ips:
            threading.Thread(target=self._poll, args=(ip,), daemon=True).start()

    def label(self, mac: str) -> str:
        return self.name.get(mac, f"AP {mac[-5:]}")

    def stamp(self) -> str:
        return f"{time.strftime('%H:%M:%S')} +{time.time() - self.start:6.1f}s"

    def event(self, text: str) -> None:
        with self.lock:
            print(f"{self.stamp()}  {text}", flush=True)

    def pump(self, seconds: float) -> None:
        """Receive reports for a while, renewing subscriptions."""
        end = time.time() + seconds
        while time.time() < end:
            now = time.time()
            if now - self.last_sub >= 3:
                for ip in self.ips:
                    try:
                        self.sock.sendto(build_subscribe(STREAM_LINK_REPORTS | STREAM_HIVE_REPORTS), (ip, PORT))
                    except OSError:  # this computer's network is down for a moment: try again later
                        pass
                self.last_sub = now
            try:
                data, _ = self.sock.recvfrom(8192)
            except (socket.timeout, OSError):
                continue
            try:
                p = parse_packet(data)
            except ProtocolError:
                continue
            if isinstance(p, LinkReport):
                for link in p.links:
                    key = (p.node, link.transmitter)
                    entry = self.links.setdefault(key, {"last_frames": 0.0, "frames": [], "up": False})
                    entry.update(rssi=link.rssi, score=link.score, motion=link.motion, seen=now)
                    entry["frames"] = [(t, f) for t, f in entry["frames"] if now - t <= 5] + [(now, link.frames)]
                    if link.frames > 0:
                        entry["last_frames"] = now
            elif isinstance(p, HiveReport):
                old = self.hives.get(p.node)
                if old is not None and old.hash != p.hash:
                    self.event(f"{self.label(p.node)}: hive hash {old.hash:08x} -> {p.hash:08x}")
                self.hives[p.node] = p
            self.check_links(now)

    def check_links(self, now: float) -> None:
        for (rx, tx), e in self.links.items():
            up = now - e["last_frames"] <= LINK_GONE_S and now - e.get("seen", 0) <= LINK_GONE_S
            if up != e["up"]:
                e["up"] = up
                self.event(f"link {self.label(tx)} -> {self.label(rx)} {'FOUND' if up else 'LOST'}")

    def read_esphome(self) -> None:
        """Kept for callers: the pollers keep the readings current on their own."""

    def _poll(self, ip: str) -> None:
        last_slow = 0.0
        while True:
            old = dict(self.readings[ip])
            new = dict(old)
            for key in ("grid", "sync", "uptime"):
                new[key] = web(ip, SENSORS[key], timeout=1.5)
            if time.time() - last_slow >= 10:
                last_slow = time.time()
                for key in ("channel", "csi"):
                    new[key] = web(ip, SENSORS[key], timeout=1.5)
            self._compare(ip, old, new)
            self.readings[ip] = new
            time.sleep(self.poll)

    def _compare(self, ip: str, old: dict, new: dict) -> None:
        name = self.name.get(self.mac.get(ip, ""), ip)
        if new["uptime"] is None and old.get("uptime") is not None:
            self.event(f"{name}: OUT OF REACH")
        elif new["uptime"] is not None and old.get("uptime") is None and old:
            self.event(f"{name}: reachable again")
        if new["uptime"] is not None and old.get("uptime") is not None and new["uptime"] + 5 < old["uptime"]:
            self.event(f"{name}: RESTARTED")
        for key, what in (("grid", "Grid nodes"), ("sync", "Hive in sync"), ("channel", "Grid channel")):
            if new.get(key) is not None and old.get(key) is not None and new[key] != old[key]:
                self.event(f"{name}: {what} {old[key]} -> {new[key]}")

    def frames_per_s(self, e: dict) -> float:
        frames = e["frames"]
        if len(frames) < 2:
            return 0.0
        span = frames[-1][0] - frames[0][0]
        return sum(f for _, f in frames[1:]) / span if span > 0 else 0.0

    def whole(self) -> bool:
        n = len(self.ips)
        grid_ok = all(r.get("grid") == n for r in self.readings.values())
        sync_ok = all(r.get("sync") is True for r in self.readings.values())
        hashes = {h.hash for mac, h in self.hives.items() if mac in self.mac.values()}
        nodes = set(self.mac.values())
        links_ok = all(self.links.get((rx, tx), {}).get("up") for rx in nodes for tx in nodes if rx != tx)
        return grid_ok and sync_ok and len(hashes) == 1 and links_ok

    def table(self) -> None:
        print(f"\n{self.stamp()}  grid {'WHOLE' if self.whole() else 'not whole'}")
        for ip in self.ips:
            mac = self.mac.get(ip, "")
            r = self.readings[ip]
            print(f"  {self.name.get(mac, ip):13s} {ip:15s} grid {r.get('grid')} sync {r.get('sync')} "
                  f"ch {r.get('channel')} AP CSI {r.get('csi')} Hz, up {r.get('uptime')} s")
            for (rx, tx), e in sorted(self.links.items()):
                if rx != mac:
                    continue
                score = "-" if e.get("score") is None else f"{e['score']:.2f}"
                print(f"      hears {self.label(tx):13s} {self.frames_per_s(e):5.1f} CSI/s  {e.get('rssi')} dBm  "
                      f"score {score}{'  MOTION' if e.get('motion') else ''}{'' if e['up'] else '  (lost)'}")
        layouts = {}
        for mac, h in sorted(self.hives.items()):
            lay = tuple((self.label(p.node), round(p.x, 1), round(p.y, 1)) for p in h.layout)
            layouts.setdefault((h.hash, lay), []).append(self.label(mac))
        for (hsh, lay), who in layouts.items():
            pts = ", ".join(f"{n} ({x:+.1f}, {y:+.1f})" for n, x, y in lay) or "none yet"
            print(f"  layout by {', '.join(who)} (hash {hsh:08x}): {pts}")
        if len(layouts) == 1 and self.hives:
            print("  every node solved the same layout")


def heal(grid: Grid, ip: str, timeout: float = 120.0) -> None:
    mac = grid.mac.get(ip)
    name = grid.name.get(mac, ip)
    others = [o for o in grid.ips if o != ip]
    other_macs = [grid.mac[o] for o in others]
    n = len(grid.ips)
    print(f"\n=== self-healing test: restarting {name} ===", flush=True)
    wait_end = time.time() + 180
    while not grid.whole() and time.time() < wait_end:
        grid.pump(1)
    if not grid.whole():
        print("grid not whole before the test, skipping")
        return
    t0 = time.time()
    if not press(ip, "Restart"):
        print("restart failed")
        return
    steps = ["others mark it gone", "its CSI heard again", "every node counts the grid whole", "hives in sync"]
    marks: dict[str, float] = {}
    while time.time() - t0 < timeout and len(marks) < len(steps):
        grid.pump(0.2)
        now = time.time() - t0
        r = grid.readings
        heard = [grid.links.get((rx, mac), {}).get("up", False) for rx in other_macs]
        if steps[0] not in marks and all(r[o].get("grid") == n - 1 for o in others):
            marks[steps[0]] = now
        if steps[0] in marks and steps[1] not in marks and all(heard):
            marks[steps[1]] = now
        if steps[1] in marks and steps[2] not in marks and all(r[x].get("grid") == n for x in grid.ips):
            marks[steps[2]] = now
        if steps[2] in marks and steps[3] not in marks and grid.whole():
            marks[steps[3]] = now
    print(f"=== {name} restarted: " + ", ".join(f"{k} after {v:.1f} s" for k, v in marks.items())
          + ("" if len(marks) == len(steps) else f"; not all within {timeout:.0f} s") + " ===", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nodes", nargs="+", help="node IPs")
    ap.add_argument("--interval", type=float, default=10, help="seconds between tables")
    ap.add_argument("--minutes", type=float, default=0, help="stop after this long (0: never)")
    ap.add_argument("--heal", action="append", default=[], help="node IP to restart for the self-healing test")
    ap.add_argument("--poll", type=float, default=0.3, help="seconds between readings of each node's web page")
    args = ap.parse_args()
    grid = Grid(args.nodes, args.poll)
    grid.pump(4)
    grid.table()
    for ip in args.heal:
        heal(grid, ip)
        grid.table()
    end = time.time() + args.minutes * 60 if args.minutes else float("inf")
    try:
        while time.time() < end:
            grid.pump(args.interval)
            grid.table()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
