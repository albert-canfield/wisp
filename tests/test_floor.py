"""A floor end to end: hive layout and rows, link scores, smoothed position (no Home Assistant)."""

from __future__ import annotations

import math
import random

from custom_components.wisp.engine.floor import FloorModel
from custom_components.wisp.engine.hive import HiveState
from custom_components.wisp.engine.protocol import HiveEntry, HiveRow
from custom_components.wisp.engine.tracking import PATH_LOSS_EXPONENT, RSSI_AT_1M

NODES = {"aa:00": (0.0, 0.0), "bb:00": (6.0, 0.0), "cc:00": (6.0, 4.5), "dd:00": (0.0, 4.5)}
AP = ("ff:00", (3.0, 5.2))


def rssi(a, b) -> int:
    return round(RSSI_AT_1M - 10 * PATH_LOSS_EXPONENT * math.log10(max(math.dist(a, b), 0.3)))


def hive_state() -> HiveState:
    rows = {}
    for origin, p in NODES.items():
        entries = [HiveEntry(other, rssi(p, q)) for other, q in NODES.items() if other != origin]
        entries.append(HiveEntry(AP[0], rssi(p, AP[1])))
        rows[origin] = HiveRow(origin, 1, tuple(entries))
    return HiveState(reporter="aa:00", seq=1, hash=1, in_sync=True, truncated=False, layout=dict(NODES), rows=rows, updated=0.0)


def score(person, tx, rx, positions, sigma=0.45) -> float:
    a, b = positions[tx], positions[rx]
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((person[0] - a[0]) * dx + (person[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    d = math.hypot(person[0] - (a[0] + t * dx), person[1] - (a[1] + t * dy))
    return math.exp(1.2 * math.exp(-(d * d) / (2 * sigma * sigma)))  # log(score) is the disturbance


def test_access_point_is_placed_from_the_rows() -> None:
    floor = FloorModel()
    floor.set_layout(hive_state())
    assert set(floor.positions) == {*NODES, AP[0]}
    assert math.dist(floor.positions[AP[0]], AP[1]) < 1.0


def test_user_placed_positions_win() -> None:
    floor = FloorModel()
    floor.set_layout(hive_state(), placed={"aa:00": (0.5, 0.5)})
    assert floor.positions["aa:00"] == (0.5, 0.5)


def test_follows_a_walk() -> None:
    floor = FloorModel()
    floor.set_layout(hive_state())
    truth_positions = {**NODES, AP[0]: AP[1]}
    links = [(t, r) for t in truth_positions for r in NODES if t != r]
    rng = random.Random(8)
    errors = []
    for i in range(40):  # walking diagonally across the room, one reading a second
        person = (0.8 + 0.11 * i, 0.8 + 0.07 * i)
        scores = {k: score(person, *k, truth_positions) * rng.uniform(0.9, 1.1) for k in links}
        fix = floor.update(scores, now=float(i))
        assert fix is not None
        if i >= 5:
            errors.append(math.dist((fix.x, fix.y), person))
    assert sum(errors) / len(errors) < 0.8, errors


def test_quiet_floor_and_unknown_links() -> None:
    floor = FloorModel()
    floor.set_layout(hive_state())
    links = [(t, r) for t in NODES for r in NODES if t != r]
    assert floor.update({k: 1.0 for k in links}, now=0.0) is None  # all quiet
    assert floor.update({k: None for k in links}, now=1.0) is None  # nothing scored yet
    assert floor.update({("xx:00", "aa:00"): 3.0}, now=2.0) is None  # transmitter without a position


def test_no_one_without_a_link_in_motion() -> None:
    """Quiet links add up to a phantom now and then: a fix needs a link that reports motion."""
    floor = FloorModel()
    floor.set_layout(hive_state())
    positions = {**NODES, AP[0]: AP[1]}
    links = [(t, r) for t in positions for r in NODES if t != r]
    noisy = {k: 1.0 + 0.12 * (i % 3) for i, k in enumerate(links)}  # every link a bit restless
    assert floor.update(noisy, now=0.0) is not None  # enough for the sum alone
    assert floor.update(noisy, now=1.0, moving=set()) is None
    person = (2.0, 2.0)
    busy = {k: score(person, *k, positions) for k in links}
    moving = {k for k, v in busy.items() if v >= 2.0}
    fix = floor.update(busy, now=2.0, moving=moving)
    assert moving and fix is not None and math.dist((fix.raw_x, fix.raw_y), person) < 1.0


def test_positions_stay_on_the_plan() -> None:
    """On a floor plan, someone is placed inside it, even when the links point past its edge."""
    floor = FloorModel()
    placed = {mac: (p[0] + 0.5, p[1] + 0.5) for mac, p in NODES.items()}
    floor.set_layout(hive_state(), placed, plan=(6.5, 5.0), nodes=set(NODES))
    positions = {**floor.positions}
    links = [(t, r) for t in positions for r in NODES if t != r]
    person = (7.0, 2.7)  # just past the right wall, by the nodes standing on it
    fixes = [floor.update({k: score(person, *k, positions) for k in links}, now=float(i), moving=set(links)) for i in range(5)]
    fixes = [f for f in fixes if f is not None]
    assert fixes and all(0 <= f.x <= 6.5 and 0 <= f.y <= 5.0 and 0 <= f.raw_x <= 6.5 for f in fixes)
    assert max(f.raw_x for f in fixes) == 6.5  # held at the wall
