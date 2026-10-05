"""Placing access points from the nodes that hear them, and smoothing a person's track.

place_access_point: an access point has no row of its own in the hive, but every node reports
how strongly it hears it. RSSI becomes a rough distance (the same path loss model as the nodes'
layout solver) and a least squares fit (Gauss-Newton) puts the AP where those distances agree
best. Needs three nodes; with two the answer is one of two mirror images. Readings the geometry
cannot meet would push the fit away without end, so it stays within twice the distance of the
node that hears the AP best.

Track: a constant velocity Kalman filter in 2D. Measurements that land implausibly far from the
prediction are ignored for a moment (a gate), so one bad fit does not throw the dot across the
house; the filter is reset if the person keeps showing up elsewhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math

Point = tuple[float, float]

RSSI_AT_1M = -45.0
PATH_LOSS_EXPONENT = 2.7


def rssi_to_metres(rssi: float) -> float:
    d = 10 ** ((RSSI_AT_1M - rssi) / (10 * PATH_LOSS_EXPONENT))
    return min(40.0, max(0.3, d))


def place_access_point(heard: Mapping[Point, float], iterations: int = 30) -> Point | None:
    """heard: node position -> RSSI of the access point there. Returns the best fit position."""
    if len(heard) < 2:
        return None
    pts = list(heard)
    dists = [rssi_to_metres(heard[p]) for p in pts]
    weights = [1.0 / d for d in dists]  # near readings are more trustworthy
    # Start: weighted centroid, nudged off the nodes' line so two nodes still converge.
    sw = sum(weights)
    x = sum(w * p[0] for w, p in zip(weights, pts)) / sw + 0.1
    y = sum(w * p[1] for w, p in zip(weights, pts)) / sw + 0.1
    for _ in range(iterations):
        jtj = [[0.0, 0.0], [0.0, 0.0]]
        jtr = [0.0, 0.0]
        for (px, py), d, w in zip(pts, dists, weights):
            dx, dy = x - px, y - py
            r = math.hypot(dx, dy) or 1e-6
            jx, jy = dx / r, dy / r
            res = r - d
            jtj[0][0] += w * jx * jx
            jtj[0][1] += w * jx * jy
            jtj[1][1] += w * jy * jy
            jtr[0] += w * jx * res
            jtr[1] += w * jy * res
        jtj[1][0] = jtj[0][1]
        det = jtj[0][0] * jtj[1][1] - jtj[0][1] * jtj[1][0] + 1e-9
        step_x = (jtj[1][1] * jtr[0] - jtj[0][1] * jtr[1]) / det
        step_y = (jtj[0][0] * jtr[1] - jtj[1][0] * jtr[0]) / det
        x, y = x - step_x, y - step_y
        if abs(step_x) + abs(step_y) < 1e-4:
            break
    # Readings that the geometry cannot meet (nodes close together, one hearing much less) push
    # the fit away without end: no further than twice its distance from the node hearing it best.
    near = min(range(len(pts)), key=dists.__getitem__)
    (nx, ny), d = pts[near], dists[near]
    dx, dy = x - nx, y - ny
    r = math.hypot(dx, dy) if math.isfinite(dx) and math.isfinite(dy) else math.inf
    if r > 2.0 * d:  # back to its own distance from that node, in the direction of the fit
        if not math.isfinite(r):
            dx, dy, r = 1.0, 0.0, 1.0
        x, y = nx + dx / r * d, ny + dy / r * d
    return (x, y)


@dataclass
class Track:
    """Smoothed position and velocity of one person, from noisy fixes once a second or so."""

    accel: float = 0.8  # how fast a person may change speed (m/s^2), the process noise
    noise: float = 0.5  # typical fix error (m)
    gate: float = 2.5  # fixes further than this many sigmas from the prediction are doubted
    reset_after: int = 3  # consecutive doubted fixes that restart the track there
    state: list[float] | None = field(default=None, init=False)  # x, y, vx, vy
    cov: list[list[float]] = field(default_factory=list, init=False)
    t: float = field(default=0.0, init=False)
    doubted: int = field(default=0, init=False)

    def _start(self, x: float, y: float, t: float) -> None:
        self.state = [x, y, 0.0, 0.0]
        n2 = self.noise * self.noise
        self.cov = [[n2, 0, 0, 0], [0, n2, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]
        self.t = t
        self.doubted = 0

    def update(self, x: float, y: float, t: float, quality: float = 1.0) -> Point:
        """Feed a fix at time t (seconds); quality 0 to 1 widens or narrows its error."""
        if self.state is None or t - self.t > 10:
            self._start(x, y, t)
            return (x, y)
        dt = max(1e-3, t - self.t)
        self.t = t
        s = self.state
        # Predict: position moves with velocity; uncertainty grows with possible acceleration.
        s[0] += s[2] * dt
        s[1] += s[3] * dt
        p = self.cov
        q = self.accel * self.accel
        for a in (0, 1):
            v = a + 2
            p[a][a] += dt * (p[v][a] + p[a][v]) + dt * dt * p[v][v] + q * dt**4 / 4
            p[a][v] += dt * p[v][v] + q * dt**3 / 2
            p[v][a] = p[a][v]
            p[v][v] += q * dt * dt
        # Update each axis (independent axes keep this small and exact enough).
        r = (self.noise / max(0.2, min(1.0, quality))) ** 2
        far = [abs((x, y)[a] - s[a]) / math.sqrt(p[a][a] + r) for a in (0, 1)]
        if max(far) > self.gate:
            self.doubted += 1
            if self.doubted >= self.reset_after:
                self._start(x, y, t)
            return (s[0], s[1])
        self.doubted = 0
        for a, z in ((0, x), (1, y)):
            v = a + 2
            innov = z - s[a]
            denom = p[a][a] + r
            k_pos, k_vel = p[a][a] / denom, p[v][a] / denom
            s[a] += k_pos * innov
            s[v] += k_vel * innov
            paa, pva, pvv = p[a][a], p[v][a], p[v][v]
            p[a][a] = (1 - k_pos) * paa
            p[a][v] = p[v][a] = (1 - k_pos) * pva
            p[v][v] = pvv - k_vel * pva
        return (s[0], s[1])
