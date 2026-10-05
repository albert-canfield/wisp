"""Anchoring the hive's layout on a floor plan (no Home Assistant needed)."""

from __future__ import annotations

import math
import random

import pytest

from custom_components.wisp.engine.anchor import Similarity, anchor_layout, fit_similarity, shift
from custom_components.wisp.engine.floor import FloorModel

from .test_floor import AP, NODES, hive_state

# A house's nodes on its plan (metres from the top left corner, y down)
PLAN = {"a": (1.0, 1.0), "b": (9.0, 1.5), "c": (8.5, 7.0), "d": (1.5, 6.5), "e": (5.0, 4.0), "f": (3.0, 2.5)}


def hive_of(plan: dict, turn: float, scale: float, mirror: bool, shift_by=(0.0, 0.0), noise=0.0, seed=1) -> dict:
    """What the hive could make of the plan: turned, scaled, maybe mirrored, shifted, with noise."""
    rng = random.Random(seed)
    out = {}
    for key, (x, y) in plan.items():
        if mirror:
            y = -y
        c, s = math.cos(math.radians(turn)), math.sin(math.radians(turn))
        out[key] = (
            scale * (c * x - s * y) + shift_by[0] + rng.gauss(0, noise),
            scale * (s * x + c * y) + shift_by[1] + rng.gauss(0, noise),
        )
    return out


@pytest.mark.parametrize(("turn", "scale", "mirror"), [(0, 1, False), (37, 0.8, False), (-120, 1.3, True), (180, 1, True)])
def test_exact_layouts_fit_exactly(turn: float, scale: float, mirror: bool) -> None:
    hive = hive_of(PLAN, turn, scale, mirror, (4.0, -2.0))
    fit = fit_similarity([(hive[k], PLAN[k]) for k in "abc"])
    assert fit is not None and fit.mirror is mirror and not fit.guessed
    assert fit.error < 1e-9 and fit.pairs == 3
    assert fit.scale == pytest.approx(1 / scale)
    for key in PLAN:  # the unplaced ones land where they belong
        assert math.dist(fit.apply(hive[key]), PLAN[key]) < 1e-9


@pytest.mark.parametrize("mirror", [False, True])
def test_noisy_layouts_fit_closely(mirror: bool) -> None:
    errors = []
    for seed in range(20):
        hive = hive_of(PLAN, seed * 17.0, 0.9 + 0.02 * seed, mirror, (seed, -seed), noise=0.25, seed=seed)
        placed = {k: PLAN[k] for k in "abcd"}
        positions, fit = anchor_layout(hive, placed, (5.0, 4.0))
        assert fit.mirror is mirror, seed  # the error decides: four spread points tell it clearly
        assert 0.0 < fit.error < 0.5
        assert all(positions[k] == PLAN[k] for k in placed)  # placed points stay put
        errors += [math.dist(positions[k], PLAN[k]) for k in "ef"]
    assert sum(errors) / len(errors) < 0.45, errors


def test_rotation_and_scale_are_reported() -> None:
    hive = hive_of(PLAN, 30, 2.0, False)
    fit = fit_similarity([(hive[k], PLAN[k]) for k in PLAN])
    assert fit.rotation == pytest.approx(-30) and fit.scale == pytest.approx(0.5)


def test_two_points_cannot_tell_the_mirror() -> None:
    hive = hive_of(PLAN, 50, 1.1, True)
    pairs = [(hive[k], PLAN[k]) for k in "ab"]
    for prefer in (False, True):
        fit = fit_similarity(pairs, prefer_mirror=prefer)
        assert fit.mirror is prefer and fit.guessed and fit.error < 1e-9
        assert all(math.dist(fit.apply(p), q) < 1e-9 for p, q in pairs)  # both ways fit the two exactly
    # Three on one line cannot either, a third off the line can
    line = {"a": (0.0, 0.0), "b": (2.0, 1.0), "c": (6.0, 3.0), "d": (1.0, 4.0)}
    hive = hive_of(line, 10, 1.0, True)
    assert fit_similarity([(hive[k], line[k]) for k in "abc"]).guessed
    fit = fit_similarity([(hive[k], line[k]) for k in "abd"])
    assert fit.mirror is True and not fit.guessed
    assert fit_similarity([(hive[k], line[k]) for k in "abd"], mirror=False).error > 0.5  # forced the wrong way


def test_too_few_points() -> None:
    assert fit_similarity([]) is None
    assert fit_similarity([((1.0, 1.0), (2.0, 2.0))]) is None
    assert fit_similarity([((1.0, 1.0), (2.0, 2.0)), ((1.0, 1.0), (5.0, 2.0))]) is None  # no spread on the layout


def test_shifts_without_two_placed_nodes() -> None:
    hive = {"a": (0.0, 0.0), "b": (4.0, 0.0), "c": (4.0, 3.0)}
    # None placed: the layout's middle on the plan's centre, the same size, y turned down
    positions, fit = anchor_layout(hive, {}, (5.0, 4.0), prefer_mirror=True)
    assert fit.pairs == 0 and fit.scale == 1 and fit.mirror and fit.guessed
    assert positions["a"] == pytest.approx((3.0, 5.5)) and positions["c"] == pytest.approx((7.0, 2.5))
    # One placed: the layout moves with it
    positions, fit = anchor_layout(hive, {"b": (8.0, 1.0)}, (5.0, 4.0))
    assert fit.pairs == 1 and positions["b"] == (8.0, 1.0)
    assert positions["a"] == pytest.approx((4.0, 1.0)) and positions["c"] == pytest.approx((8.0, 4.0))
    # Placed points off the layout are kept, and an empty layout leaves just them
    positions, _ = anchor_layout({}, {"ap": (2.0, 2.0)}, (5.0, 4.0))
    assert positions == {"ap": (2.0, 2.0)}


def test_shift_and_identity() -> None:
    assert Similarity().apply((1.5, -2.0)) == (1.5, -2.0)
    move = shift((1.0, 2.0), (3.0, 3.0), mirror=True)
    assert move.apply((1.0, 2.0)) == pytest.approx((3.0, 3.0))
    assert move.apply((1.0, 3.0)) == pytest.approx((3.0, 2.0))  # one up on the layout is one down on the plan


def test_floor_model_on_a_plan() -> None:
    """The floor's nodes fitted onto a plan; access points placed from the rows in plan metres."""
    hive = hive_state()  # NODES of tests/test_floor.py, the access point at AP

    def on_plan(p):  # the layout turned a quarter, moved 1 m in: no mirror
        return (1.0 + p[1], 7.0 - p[0])

    plan = {mac: on_plan(p) for mac, p in NODES.items()}
    floor = FloorModel()
    floor.set_layout(hive, {mac: plan[mac] for mac in ("aa:00", "cc:00")}, plan=(7.0, 8.0), nodes=set(NODES))
    assert floor.fit.pairs == 2 and floor.fit.scale == pytest.approx(1.0)
    # Two placed nodes leave the mirror to the preference, which keeps the map's look: wrong here
    assert floor.fit.guessed and floor.fit.mirror
    assert math.dist(floor.positions["bb:00"], plan["bb:00"]) > 3
    # A third one settles it
    floor.set_layout(hive, {mac: plan[mac] for mac in ("aa:00", "bb:00", "cc:00")}, plan=(7.0, 8.0), nodes=set(NODES))
    assert not floor.fit.guessed and not floor.fit.mirror and floor.fit.error < 1e-9
    assert math.dist(floor.positions["dd:00"], plan["dd:00"]) < 1e-9
    assert math.dist(floor.positions[AP[0]], on_plan(AP[1])) < 1.0
    # A placed access point stays put; nodes of other floors are left out
    floor.set_layout(hive, {**plan, AP[0]: (2.0, 2.0)}, plan=(7.0, 8.0), nodes={"aa:00", "bb:00", "cc:00"})
    assert floor.positions[AP[0]] == (2.0, 2.0) and "dd:00" in floor.positions  # placed by the user
    floor.set_layout(hive, {}, plan=(7.0, 8.0), nodes={"aa:00", "bb:00"})
    assert set(floor.positions) == {"aa:00", "bb:00", AP[0]}
    # Without the plan the hive's layout is back, and the track starts over
    track = floor.track
    floor.set_layout(hive)
    assert floor.positions["aa:00"] == NODES["aa:00"] and floor.fit is None and floor.track is not track
