#!/usr/bin/env python3
"""Where the Locator places someone on labelled recordings, per link model.

    .venv/bin/python firmware/tools/locator_study.py --plan <config>/.storage/wisp.<entry>.plans
    .venv/bin/python firmware/tools/locator_study.py --plan ... --cache seconds.pickle --extra data/*-20261005-2[12].wcsi

Reads the nodes' link reports from the recordings and, for every second of the labelled windows
(WINDOWS, the owner's notes of 2026-10-05) where a link reports motion that another link backs up
within 2 s (the gate floor.py applies), runs the Locator over the whole plan, without the room
room presence names: the raw geometry. A fit counts when it explains at least min_quality of the
pattern, as in floor.py, and is then held on the plan. Per model: office and play room fits over
the seconds with motion, the share in the labelled room and their median distance from it; walk
fits on the path (office, hallway, play room); fits on the empty floor; the mean share of the
pattern explained (quality); fits outside the plan before they are held on it (off: behind a
node); the office share per office window. --extra adds the chosen zone's reach and scale prior.

On 2026-10-05 (exp D0=0.15 chosen, see ZONE in engine/imaging.py): the line model and the
Fresnel zones from 0.05 to 0.15 m put 33 to 36% of the office fits in the office and fit all 19
seconds with motion on the empty floor; most office fits that miss land at the play room's node,
whose links (the wall link to the office's node and the long diagonal) were the busiest in the
first window, whatever the model. The line model put 10% of all fits outside the house (behind a
node), the Fresnel zones none. The play room window (sitting quietly) has 2 seconds with motion.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import glob
import json
import math
from pathlib import Path
import pickle
import statistics
import sys
import time

from csi_logger import read_wcsi

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "custom_components" / "wisp"))
from engine.imaging import ZONE, Locator, excess_path, line_distance  # noqa: E402
from engine.protocol import LinkReport, ProtocolError, parse_packet  # noqa: E402

FLOOR = "ground_floor"  # the plan's floor (--floor)
AGE = 3.0  # s: as ROOM_LINK_AGE
CORROBORATE = 2.0  # s: as FloorModel.corroborate
MIN_DISTURBANCE = 0.3  # as FloorModel
MIN_QUALITY = 0.35  # as FloorModel
OFFICE, PLAY = "office", "kid_s_room"
WALK = "walk"  # office, hallway, play room
EMPTY = "empty"

# Local time, 2026-10-05: where the owner was.
WINDOWS = [
    ("21:23:00", "21:31:00", OFFICE),  # at the desk, working, small motions
    ("21:49:00", "21:58:00", OFFICE),
    ("22:03:41", "22:03:56", WALK),  # office, hallway, play room
    ("22:04:00", "22:05:40", PLAY),  # sitting quietly
    ("22:05:44", "22:06:00", WALK),  # play room, hallway, office
    ("22:06:05", "22:07:45", OFFICE),
    ("22:09:02", "22:12:20", EMPTY),  # nobody on the floor
    ("22:20:40", "22:24:00", OFFICE),
    ("22:29:00", "22:36:50", EMPTY),
]


def at(day: time.struct_time, hms: str) -> float:
    h, m, s = (int(v) for v in hms.split(":"))
    return time.mktime((day.tm_year, day.tm_mon, day.tm_mday, h, m, s, 0, 0, -1))


@dataclass
class Second:
    t: float
    label: str
    scores: dict  # (transmitter, receiver): score
    moving: bool  # a corroborated link reports motion


def seconds(files: list[str], nodes: set[str]) -> list[Second]:
    """Each second of the windows, as presence.py and floor.py see it."""
    reports = []
    for path in files:
        for t, pkt in read_wcsi(path):
            if len(pkt) < 6 or pkt[5] != 2:  # byte 5: packet type, 2 = link report
                continue
            try:
                r = parse_packet(pkt)
            except ProtocolError:
                continue
            if isinstance(r, LinkReport):
                reports.append((t, r))
    reports.sort(key=lambda x: x[0])
    day = time.localtime(reports[-1][0])
    out = []
    for start, end, label in WINDOWS:
        t0, t1 = at(day, start), at(day, end)
        links: dict = {}
        flags: deque = deque()
        i = 0
        now = t0 - 6  # warm up the corroboration
        while now < t1:
            while i < len(reports) and reports[i][0] <= now:
                t, r = reports[i]
                if r.node in nodes:
                    for link in r.links:
                        links[(link.transmitter, r.node)] = (t, link.score, link.motion)
                i += 1
            live = {k: v for k, v in links.items() if v[1] is not None and now - v[0] <= AGE}
            moving = frozenset(k for k, v in live.items() if v[2])
            flags.append((now, moving))
            while flags and now - flags[0][0] > CORROBORATE:
                flags.popleft()
            seen = set().union(*(m for _, m in flags))
            backed = any(len(seen - {k}) >= 1 for k in moving)
            if now >= t0:
                out.append(Second(now, label, {k: v[1] for k, v in live.items()}, backed))
            now += 1.0
    return out


# Link models: a link's predicted disturbance by someone at a pixel p. d: distance from p to the
# link's segment; D: excess path, |p-A| + |p-B| - |A-B| (bistatic radar: constant on the link's
# Fresnel ellipses). The engine's Locator is the Fresnel model exp(-D / zone).


@dataclass
class Line(Locator):
    """Before: a Gaussian across the link's segment, width w (round around its ends)."""

    width: float = 0.4

    def predict(self, p, a, b) -> float:
        d = line_distance(p, a, b)
        return math.exp(-d * d / (2 * self.width * self.width))


class FresnelGauss(Locator):
    """exp(-(D / zone)^2): flat across a link's middle, then steep."""

    def predict(self, p, a, b) -> float:
        return math.exp(-((excess_path(p, a, b) / self.zone) ** 2))


@dataclass
class Mix(Locator):
    """The mean of the line model and the Fresnel model."""

    width: float = 0.4

    def predict(self, p, a, b) -> float:
        d = line_distance(p, a, b)
        return 0.5 * math.exp(-d * d / (2 * self.width * self.width)) + 0.5 * Locator.predict(self, p, a, b)


LINE_REACH = 1.5 * 1.5 / 2  # before: 1.5 widths from a segment (a predicted 0.32)


def variants(extra: bool) -> list[tuple[str, type, dict]]:
    """(name, class, keywords): the comparison; with extra, the zone's reach and scale prior too."""
    out = [(f"line w={w}{' (before)' if w == 0.4 else ''}", Line, {"width": w, "reach": LINE_REACH}) for w in (0.3, 0.4, 0.6)]
    out += [(f"fresnel exp D0={d0}{' (now)' if d0 == ZONE else ''}", Locator, {"zone": d0}) for d0 in (0.05, 0.07, 0.1, 0.15, 0.2, 0.3, 0.5)]
    out += [(f"fresnel gauss D0={d0}", FresnelGauss, {"zone": d0, "reach": LINE_REACH}) for d0 in (0.07, 0.1, 0.15, 0.2, 0.3)]
    out += [(f"mix line 0.4 + exp D0={d0}", Mix, {"zone": d0, "reach": LINE_REACH}) for d0 in (0.1, 0.2)]
    if extra:
        out += [(f"fresnel exp D0={ZONE} reach={r}", Locator, {"reach": r}) for r in (0.5, 2.0, 3.0)]
        out += [(f"fresnel exp D0={ZONE} prior={m}", Locator, {"scale_prior": m}) for m in (0.0, 0.25, 1.0, 2.0)]
    return out


def rect_distance(rects, x: float, y: float) -> float:
    return min(math.hypot(max(rx - x, 0, x - rx - rw), max(ry - y, 0, y - ry - rh)) for rx, ry, rw, rh in rects)


def room_of(rooms: dict, x: float, y: float) -> str:
    for area, rects in rooms.items():
        if rect_distance(rects, x, y) == 0:
            return area
    return "hallway"


def evaluate(make, plan: dict, positions: dict, data: list[Second]) -> dict:
    """Per label, and per office window (keyed by its start): seconds gated, fits, fits in the
    labelled room (on the path, for walks), distances from it, qualities."""
    rooms = plan["rooms"]
    locators: dict = {}
    starts = {at(time.localtime(data[0].t), w[0]): w[0] for w in WINDOWS}
    res: dict = {}
    window = None
    for s in data:
        window = starts.get(s.t, window)
        usable = tuple(sorted(k for k in s.scores if k[0] in positions and k[1] in positions))
        if not s.moving or len(usable) < 2:
            continue
        if usable not in locators:
            locators[usable] = make(positions, list(usable))
        spot = locators[usable].locate({k: math.log(max(s.scores[k], 1.0)) for k in usable}, MIN_DISTURBANCE)
        for key in (s.label, window) if s.label == OFFICE else (s.label,):
            r = res.setdefault(key, {"gated": 0, "fits": 0, "in": 0, "dist": [], "quality": [], "off": 0})
            r["gated"] += 1
            if spot is None or spot.contrast < MIN_QUALITY:
                continue
            x, y = min(max(spot.x, 0.0), plan["width"]), min(max(spot.y, 0.0), plan["height"])
            r["fits"] += 1
            r["off"] += (x, y) != (spot.x, spot.y)
            r["quality"].append(spot.contrast)
            if s.label in (OFFICE, PLAY):
                d = rect_distance(rooms[s.label], x, y)
                r["dist"].append(d)
                r["in"] += d == 0
            elif s.label == WALK:
                r["in"] += room_of(rooms, x, y) in (OFFICE, PLAY, "hallway")
    return res


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:3.0f}%" if b else "  - "


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help=".wcsi recordings (default data/wisp-*-20261005-2[12].wcsi)")
    ap.add_argument("--plan", required=True, help="the integration's plans store (wisp.<entry>.plans)")
    ap.add_argument("--cache", help="pickle of the labelled seconds: written once, read after")
    ap.add_argument("--floor", default=FLOOR, help=f"the plan's floor (default {FLOOR})")
    ap.add_argument("--extra", action="store_true", help="also the chosen zone's reach and scale prior")
    args = ap.parse_args()
    plan = json.loads(Path(args.plan).read_text())["data"]["floors"][args.floor]
    positions = {m: tuple(p) for m, p in {**plan["nodes"], **plan["access_points"]}.items()}
    if args.cache and Path(args.cache).exists():
        data = [Second(*row) for row in pickle.loads(Path(args.cache).read_bytes())]
    else:
        files = args.files or sorted(glob.glob(str(ROOT / "data" / "wisp-*-20261005-2[12].wcsi")))
        data = seconds(files, set(plan["nodes"]))
        if args.cache:
            Path(args.cache).write_bytes(pickle.dumps([(s.t, s.label, s.scores, s.moving) for s in data]))
    total = {lab: sum(s.label == lab for s in data) for lab in (OFFICE, PLAY, WALK, EMPTY)}
    print("labelled seconds:", total, " with a link in motion:", {lab: sum(s.label == lab and s.moving for s in data) for lab in total})
    office = [w[0] for w in WINDOWS if w[2] == OFFICE]
    print(f"{'model':30s} | {'office fits':>11s} {'in':>4s} {'med m':>5s} | {'play':>7s} {'in':>4s} | {'walk':>3s} {'path':>4s} | {'empty':>7s} | {'quality':>7s} | {'off':>4s} | office in, per window from " + " ".join(office))
    for name, cls, kw in variants(args.extra):
        r = evaluate(lambda p, links, cls=cls, kw=kw: cls(p, links, **kw), plan, positions, data)
        none = {"gated": 0, "fits": 0, "in": 0, "dist": [], "quality": [], "off": 0}
        o, p, w, e = (r.get(k, none) for k in (OFFICE, PLAY, WALK, EMPTY))
        q = o["quality"] + p["quality"]
        off = sum(r[k]["off"] for k in (OFFICE, PLAY, WALK, EMPTY) if k in r)
        fits = sum(r[k]["fits"] for k in (OFFICE, PLAY, WALK, EMPTY) if k in r)
        per = " ".join(f"{pct(r[k]['in'], r[k]['fits']):>8s}" for k in office if k in r)
        print(
            f"{name:30s} | {o['fits']:4d}/{o['gated']:<4d}  {pct(o['in'], o['fits'])} {statistics.median(o['dist']) if o['dist'] else math.nan:5.2f} | "
            f"{p['fits']:3d}/{p['gated']:<3d} {pct(p['in'], p['fits'])} | {w['fits']:3d} {pct(w['in'], w['fits'])} | {e['fits']:3d}/{e['gated']:<3d} | "
            f"{statistics.mean(q) if q else math.nan:7.2f} | {pct(off, fits)} | {per}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
