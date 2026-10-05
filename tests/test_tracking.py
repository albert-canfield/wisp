"""Access point placement and track smoothing (no Home Assistant needed)."""

from __future__ import annotations

import math
import random

from custom_components.wisp.engine.tracking import (
    PATH_LOSS_EXPONENT,
    RSSI_AT_1M,
    Track,
    place_access_point,
    rssi_to_metres,
)


def rssi_at(d: float) -> float:
    return RSSI_AT_1M - 10 * PATH_LOSS_EXPONENT * math.log10(max(d, 0.3))


def test_rssi_round_trip() -> None:
    for d in (0.5, 1.0, 3.0, 8.0):
        assert abs(rssi_to_metres(rssi_at(d)) - d) < 1e-6


def test_places_an_access_point_from_three_nodes() -> None:
    nodes = [(0.0, 0.0), (6.0, 0.0), (6.0, 4.5), (0.0, 4.5)]
    rng = random.Random(3)
    for ap in [(3.0, 5.0), (1.0, 2.0), (5.5, 1.0)]:
        heard = {p: rssi_at(math.dist(p, ap)) + rng.gauss(0, 1.0) for p in nodes}
        got = place_access_point(heard)
        assert got is not None and math.dist(got, ap) < 1.5, (ap, got)
    assert place_access_point({(0.0, 0.0): -50}) is None


def test_readings_the_geometry_cannot_meet_stay_near() -> None:
    """Two boards side by side, one hearing the AP far weaker: the fit used to run off for km."""
    a, b = (0.2, 0.0), (-0.2, 0.0)
    for strong, weak in [(-48, -61), (-40, -75), (-61, -48)]:
        got = place_access_point({a: strong, b: weak})
        near, d = (a, rssi_to_metres(strong)) if strong > weak else (b, rssi_to_metres(weak))
        assert got is not None and all(map(math.isfinite, got))
        assert math.dist(got, near) <= 2 * d + 1e-9, (strong, weak, got)
        assert math.dist(got, near) < math.dist(got, b if near == a else a)  # beyond the node hearing it best


def test_track_smooths_and_follows() -> None:
    rng = random.Random(5)
    track = Track()
    raw, smooth = [], []
    for i in range(60):  # walking at 0.8 m/s along x, one fix a second
        truth = (0.8 * i * 0.5, 2.0)
        fix = (truth[0] + rng.gauss(0, 0.5), truth[1] + rng.gauss(0, 0.5))
        est = track.update(*fix, t=i * 0.5)
        if i > 10:
            raw.append(math.dist(fix, truth))
            smooth.append(math.dist(est, truth))
    assert sum(smooth) / len(smooth) < 0.8 * sum(raw) / len(raw)


def test_track_ignores_one_wild_fix_but_follows_a_real_jump() -> None:
    track = Track()
    for i in range(10):
        track.update(2.0, 2.0, t=float(i))
    held = track.update(9.0, 9.0, t=10.0)  # one bad fit across the house
    assert math.dist(held, (2.0, 2.0)) < 0.5
    track.update(9.0, 9.0, t=11.0)
    jumped = track.update(9.0, 9.0, t=12.0)  # it keeps coming: someone else, or a real move
    assert math.dist(jumped, (9.0, 9.0)) < 0.5
    restarted = track.update(1.0, 1.0, t=40.0)  # a long gap starts afresh
    assert restarted == (1.0, 1.0)
