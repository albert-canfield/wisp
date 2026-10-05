"""One floor's position: from the hive's layout and the live link scores to a smoothed spot.

Positions come from the hive (nodes, relative metres) unless the user placed them, and access
points are placed from how strongly the placed nodes hear them. The Locator is rebuilt only when
positions (to 10 cm) or the set of usable links change. Each second, link scores become
disturbances (log of the score, so a quiet link is 0) and the best fit goes through the Track.
Coordinates are the layout's until floor plans add anchors (rotation, mirror and scale).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math

from .hive import HiveState
from .imaging import Locator
from .tracking import Track, place_access_point

Point = tuple[float, float]
LinkKey = tuple[str, str]  # (transmitter, receiver)


@dataclass(frozen=True, slots=True)
class FloorFix:
    x: float  # smoothed, metres
    y: float
    raw_x: float  # this second's best fit
    raw_y: float
    quality: float  # share of the link pattern the fit explains, 0 to 1


@dataclass
class FloorModel:
    width: float = 0.4  # metres a person reaches from a link's line, see Locator
    min_disturbance: float = 0.3  # sum of log scores below which nobody is moving
    positions: dict[str, Point] = field(default_factory=dict, init=False)
    track: Track = field(default_factory=Track, init=False)
    _locator: Locator | None = field(default=None, init=False)
    _key: tuple = field(default=(), init=False)

    def set_layout(self, hive: HiveState | None, placed: Mapping[str, Point] | None = None) -> None:
        """Node positions from the user (placed) or else the hive; access points from the rows."""
        positions: dict[str, Point] = dict(hive.layout) if hive else {}
        positions.update(placed or {})
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
                    positions[ap] = spot
        self.positions = positions

    def update(self, scores: Mapping[LinkKey, float | None], now: float) -> FloorFix | None:
        """scores: motion score per link (1 = quiet, None = unknown). Returns the fix, or None when
        nobody is moving or the layout cannot place anyone yet."""
        usable = sorted(k for k, s in scores.items() if s is not None and k[0] in self.positions and k[1] in self.positions)
        key = (tuple(usable), tuple(sorted((m, round(p[0], 1), round(p[1], 1)) for m, p in self.positions.items())))
        if key != self._key:
            self._key = key
            self._locator = Locator(self.positions, usable, width=self.width) if len(usable) >= 2 else None
        if self._locator is None:
            return None
        values = {k: math.log(max(scores[k], 1.0)) for k in usable}
        spot = self._locator.locate(values, self.min_disturbance)
        if spot is None:
            return None
        x, y = self.track.update(spot.x, spot.y, now, spot.contrast)
        return FloorFix(x, y, spot.x, spot.y, spot.contrast)
