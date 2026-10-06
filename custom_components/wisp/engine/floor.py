"""One floor's position: from the hive's layout and the live link scores to a smoothed spot.

Positions come from the hive (nodes, relative metres) unless the user placed them, and access
points are placed from how strongly the placed nodes hear them. On a floor plan, coordinates are
the plan's (metres from its top left corner, y down): the floor's nodes the user placed stay
put, and the rest of the hive's layout is fitted onto them (anchor.py). The Locator is rebuilt
only when positions (to 10 cm) or the set of usable links change. Each second, while at least
one of the links reports motion (the node's detector: its threshold, with hysteresis), link scores
become disturbances (log of the score, so a quiet link is 0) and the best fit goes through the
Track. The gate keeps the map in step with the motion sensors: quiet links alone add up to a
phantom now and then, more often the more links a floor has.

Few links leave the best fit coarse, and WiFi bounces off walls, so links away from someone react
too. So a fit that explains little of the pattern (min_quality) is dropped, and with rooms drawn
on the plan a fit is kept inside the house, and inside the room that room presence is sure of.

A link reporting motion counts only when another link, often its own reverse direction, also
reports motion within corroborate seconds: on real recordings a long link through walls flagged
motion alone 22 to 29% of the time, against 2 to 6% for the others, and each lone flag became
someone along it on the map.

Room presence comes first when it has calibration (presence.py): it says whether anyone is on
the floor and in which room, and the fit is searched for inside that room only. Few links cross
several rooms each, so the best fit on the whole floor often lay in the room next door while
room presence named the right one. Footprints follow only while room presence says someone walks
(walking=False draws them still), and with no room named the map shows nobody.

Someone sitting and working makes short, scattered disturbances on the same few links, each one
a fit somewhere along them: drawn as they come, that is someone darting about. So fits are kept
for a while and read together. Walking: fits in most of the last 6 s, and the centre of their
newer half a metre or more from the centre of their older half; the Track follows them. Still: a few fits in
the last 20 s without that travel; the spot is their centre, weighted by quality, and stays put.

The mark shown moves no faster than someone walks (1.5 m/s; 0.5 m/s while still): on the owner's
floor the hallway, not drawn, is searched for in all the space between the rooms, and its fits
jumped 4 to 7 m from one second to the next, and a still spot 2 m as fits came and went. The map
card starts a new trail at a 3 m jump, so footprints scattered. Capped, a room change slides
across the nearest wall of the new room.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
import math

from .anchor import Similarity, anchor_layout
from .hive import HiveState
from .imaging import ZONE, Locator
from .tracking import Track, keep_side, place_access_point

Point = tuple[float, float]
Rect = tuple[float, float, float, float]  # x, y, width, height
LinkKey = tuple[str, str]  # (transmitter, receiver)

# The hive's y points up, as the map draws it, and a plan's down: when the placed nodes cannot
# tell the mirror, the layout keeps the look it had on the map.
PREFER_MIRROR = True
INSET = 0.3  # m: a mark kept in a room stays this far in from its walls


def inside(rects: Collection[Rect], x: float, y: float) -> Point:
    """The point, or the nearest point inside the rectangles when it is outside all of them."""
    best: Point | None = None
    for rx, ry, rw, rh in rects:
        px, py = min(max(x, rx), rx + rw), min(max(y, ry), ry + rh)
        if px == x and py == y:
            return x, y
        if best is None or math.dist((px, py), (x, y)) < math.dist(best, (x, y)):
            best = (px, py)
    return best if best is not None else (x, y)


@dataclass(frozen=True, slots=True)
class FloorFix:
    x: float  # smoothed, metres
    y: float
    raw_x: float  # this second's best fit
    raw_y: float
    quality: float  # share of the link pattern the fit explains, 0 to 1
    walking: bool = True  # False: someone present and still, at the centre of recent fits


@dataclass
class FloorModel:
    zone: float = ZONE  # metres of excess path over which a person's disturbance of a link fades, see Locator
    min_disturbance: float = 0.3  # sum of log scores below which nobody is moving
    min_quality: float = 0.35  # a fit explaining less of the link pattern is too unsure to show
    min_streak: int = 2  # seconds in a row with a fit before someone appears walking
    still_window: float = 20.0  # seconds of fits behind a still person's spot
    still_min: int = 3  # fits in that window for someone present and still
    walk_span: float = 6.0  # seconds of fits that tell walking: their older half against the newer
    walk_min: int = 4  # fits in the last walk_span seconds for walking
    walk_travel: float = 1.0  # metres between the centres of the two halves, for walking
    corroborate: float = 2.0  # seconds within which another link must also report motion
    walk_speed: float = 1.5  # m/s the mark shown moves at most while walking
    still_speed: float = 0.5  # m/s while still
    follow_for: float = 10.0  # s after which a mark shown again starts where its fix is
    fits: deque = field(default_factory=deque, init=False)  # (time, x, y, quality)
    _flags: deque = field(default_factory=deque, init=False)  # (time, links reporting motion)
    walking: bool = field(default=False, init=False)
    rooms: dict[str, list[Rect]] = field(default_factory=dict, init=False)  # on a plan, by area
    spots: dict[str, Point] = field(default_factory=dict, init=False)  # by area: where someone was last still
    streak: int = field(default=0, init=False)
    positions: dict[str, Point] = field(default_factory=dict, init=False)
    fit: Similarity | None = field(default=None, init=False)  # hive layout to plan, on a floor plan
    track: Track = field(default_factory=Track, init=False)
    _locator: Locator | None = field(default=None, init=False)
    _key: tuple = field(default=(), init=False)
    _plan: tuple[float, float] | None = field(default=None, init=False)
    _aps: dict[str, Point] = field(default_factory=dict, init=False)  # last placed, to keep their side
    _shown: tuple[float, float, float] | None = field(default=None, init=False)  # (time, x, y) of the last mark

    def set_layout(
        self,
        hive: HiveState | None,
        placed: Mapping[str, Point] | None = None,
        plan: tuple[float, float] | None = None,
        nodes: Collection[str] | None = None,
        rooms: Mapping[str, list[Rect]] | None = None,
    ) -> None:
        """Node positions from the user (placed) or else the hive; access points from the rows.
        With a plan (width and height in metres), only the floor's nodes count, and the hive's
        layout is fitted onto the placed ones."""
        layout: dict[str, Point] = dict(hive.layout) if hive else {}
        if nodes is not None:
            layout = {mac: p for mac, p in layout.items() if mac in nodes}
        if plan is None:
            positions = {**layout, **(placed or {})}
            self.fit = None
        else:
            centre = (plan[0] / 2, plan[1] / 2)
            positions, self.fit = anchor_layout(layout, placed or {}, centre, PREFER_MIRROR)
        self.rooms = dict(rooms or {}) if plan is not None else {}
        if plan != self._plan:  # other coordinates: the track starts over
            self._plan = plan
            self.track = Track()
            self._aps = {}
            self.spots = {}
            self._shown = None
        # What the user did not place stays on the plan (in the house, with rooms drawn): the
        # layout and the access points come from signal strength, which can put them far out.
        positions = {key: p if key in (placed or {}) else self._keep_in(*p) for key, p in positions.items()}
        if hive:
            heard_by: dict[str, dict[Point, float]] = {}
            for origin, row in hive.rows.items():
                if origin not in positions:
                    continue
                for entry in row.entries:
                    if entry.neighbour not in hive.rows:  # no row of its own: an access point
                        heard_by.setdefault(entry.neighbour, {})[positions[origin]] = entry.rssi
            for ap, heard in heard_by.items():
                if ap not in positions and (spot := place_access_point(heard)) is not None:
                    self._aps[ap] = keep_side(spot, list(heard), self._aps.get(ap))
                    positions[ap] = self._keep_in(*self._aps[ap])
        self.positions = positions

    def update(
        self,
        scores: Mapping[LinkKey, float | None],
        now: float,
        moving: Collection[LinkKey] | None = None,
        room: str | None = None,
        walking: bool = True,
    ) -> FloorFix | None:
        """scores: motion score per link (1 = quiet, None = unknown). moving: the links reporting
        motion (None: no such gate). room: the area room presence puts someone in: the fit is
        searched for inside it when it is drawn. walking: whether room presence lets them walk;
        False shows them still. Returns the fix, or None when nobody is moving, the fit is too
        unsure, or the layout cannot place anyone yet."""
        usable = sorted(k for k, s in scores.items() if s is not None and k[0] in self.positions and k[1] in self.positions)
        key = (tuple(usable), tuple(sorted((m, round(p[0], 1), round(p[1], 1)) for m, p in self.positions.items())))
        if key != self._key:
            self._key = key
            self._locator = Locator(self.positions, usable, zone=self.zone) if len(usable) >= 2 else None
        if self._locator is None:
            return self._read(None, now, room, walking)
        if moving is not None:
            moving = self._corroborated(moving, now)
            if not any(k in moving for k in usable):
                return self._read(None, now, room, walking)
        values = {k: math.log(max(scores[k], 1.0)) for k in usable}
        drawn = self.rooms.get(room or "")
        # A room with presence but not drawn (often the hallway: the space between the drawn rooms)
        # is searched for outside every drawn room
        others = [r for rects in self.rooms.values() for r in rects] if room and not drawn else ()
        spot = self._locator.locate(values, self.min_disturbance, drawn or None, others)
        raw: tuple[float, float, float] | None = None
        if spot is not None and spot.contrast >= self.min_quality:
            raw = (*self._keep_in(spot.x, spot.y, room), spot.contrast)
        return self._read(raw, now, room, walking)

    def _corroborated(self, moving: Collection[LinkKey], now: float) -> set[LinkKey]:
        """The links reporting motion that another link backs up within corroborate seconds."""
        self._flags.append((now, frozenset(moving)))
        while self._flags and now - self._flags[0][0] > self.corroborate:
            self._flags.popleft()
        seen = set().union(*(links for _, links in self._flags))
        return {k for k in moving if len(seen - {k}) >= 1}

    def _keep_in(self, x: float, y: float, room: str | None = None) -> Point:
        """On a plan: inside it (the house: rooms not drawn, such as a hallway, are the space
        between the drawn ones), and inside the room room presence names when it is drawn, a
        little in from its walls so the mark shows in it rather than on its border."""
        if self._plan is None:
            return x, y
        x, y = min(max(x, 0.0), self._plan[0]), min(max(y, 0.0), self._plan[1])
        rects = self.rooms.get(room or "")
        return inside([_inset(r) for r in rects], x, y) if rects else (x, y)

    def _read(
        self, raw: tuple[float, float, float] | None, now: float, room: str | None = None, walking: bool = True
    ) -> FloorFix | None:
        fix = self._fix(raw, now, room, walking)
        if fix is None:
            return None
        if self._shown is not None and now - self._shown[0] <= self.follow_for:
            t, x0, y0 = self._shown
            step = (self.walk_speed if fix.walking else self.still_speed) * max(now - t, 1.0)
            if (d := math.dist((x0, y0), (fix.x, fix.y))) > step:
                x, y = self._keep_in(x0 + (fix.x - x0) * step / d, y0 + (fix.y - y0) * step / d, room)
                fix = replace(fix, x=x, y=y)
        self._shown = (now, fix.x, fix.y)
        return fix

    def _fix(
        self, raw: tuple[float, float, float] | None, now: float, room: str | None = None, walking: bool = True
    ) -> FloorFix | None:
        """This second's fit (or none) read with the recent ones: walking (when room presence
        lets them walk), still, or nobody. The position shown stays in the room room presence
        names, as each fit does, and a still spot is the centre of the fits in that room."""
        if raw is not None:
            self.fits.append((now, *raw))
            self.track.update(raw[0], raw[1], now, raw[2])
            self.streak += 1
        else:
            self.streak = 0
        while self.fits and now - self.fits[0][0] >= max(self.still_window, self.walk_span):
            self.fits.popleft()
        recent = [f for f in self.fits if now - f[0] < self.walk_span]
        older = [f for f in recent if now - f[0] >= self.walk_span / 2]
        newer = [f for f in recent if now - f[0] < self.walk_span / 2]
        travel = math.dist(_centre(older), _centre(newer)) if older and newer else 0.0
        if len(recent) >= self.walk_min and travel >= (self.walk_travel / 2 if self.walking else self.walk_travel):
            self.walking = True
        elif len(recent) < self.walk_min - 1 or travel < self.walk_travel / 2:
            self.walking = False
        if walking and self.walking and self.track.state is not None and (raw is None or self.streak >= self.min_streak):
            x, y = self._keep_in(self.track.state[0], self.track.state[1], room)
            last = self.fits[-1]
            return FloorFix(x, y, last[1], last[2], last[3])
        window = [f for f in self.fits if now - f[0] < self.still_window]
        rects = self.rooms.get(room or "")
        if rects:  # fits from before someone was in this room do not count
            window = [f for f in window if inside(rects, f[1], f[2]) == (f[1], f[2])]
        if len(window) < self.still_min:
            if not rects:
                return None
            # Room presence says someone is in the room, keeping still: no link sees them, so they
            # stay where they were last placed in it, else in the middle of its largest rectangle
            spot = self.spots.get(room or "")
            if spot is None:
                rx, ry, rw, rh = max(rects, key=lambda r: r[2] * r[3])
                spot = (rx + rw / 2, ry + rh / 2)
            return FloorFix(spot[0], spot[1], spot[0], spot[1], 0.0, walking=False)
        weight = sum(f[3] for f in window)
        last = window[-1]
        # Offsets from the newest fit: fits on one pixel give exactly that pixel. Plain weighted
        # sums came out a hair off it, and a pixel centre such as 0.375 m then flickered between
        # 0.37 and 0.38 on the map from one second to the next.
        cx = last[1] + sum((f[1] - last[1]) * f[3] for f in window) / weight
        cy = last[2] + sum((f[2] - last[2]) * f[3] for f in window) / weight
        x, y = self._keep_in(cx, cy, room)
        if rects:
            self.spots[room or ""] = (x, y)
        return FloorFix(x, y, last[1], last[2], weight / len(window), walking=False)


def _inset(rect: Rect, by: float = INSET) -> Rect:
    """The rectangle shrunk by by on every side, to no less than its centre line."""
    x, y, w, h = rect
    dx, dy = min(by, w / 2), min(by, h / 2)
    return (x + dx, y + dy, w - 2 * dx, h - 2 * dy)


def _centre(fits: list[tuple[float, float, float, float]]) -> Point:
    return sum(f[1] for f in fits) / len(fits), sum(f[2] for f in fits) / len(fits)
