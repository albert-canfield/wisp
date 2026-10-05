"""Engine: room presence on synthetic floors. Plain pytest, no Home Assistant needed."""
import json
import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "wisp"))

from engine import ClassModel, Rooms, decide, features  # noqa: E402
from engine.rooms import CONFIDENCE, HOLD, QUIET, SCORE_FLOOR, VAR_FLOOR  # noqa: E402

AP = "a8:29:48:db:b6:70"
N1, N2, N3, N4 = (f"02:57:49:53:50:0{i}" for i in range(1, 5))
FLOOR = "ground"


def links_of(*nodes: str) -> list[tuple[str, str]]:
    """Every link between the nodes, both ways, plus the access point to each."""
    return [(tx, rx) for rx in nodes for tx in (AP, *nodes) if tx != rx]


LINKS = links_of(N1, N2, N3)  # 9 links
# Each room disturbs the links that cross it: strongly, and a little on a neighbouring link
ROOMS = {
    "kitchen": {(AP, N1): 1.1, (N2, N1): 1.0, (N1, N2): 0.9, (N3, N1): 0.3},
    "office": {(AP, N2): 1.1, (N3, N2): 1.0, (N2, N3): 0.9, (N1, N2): 0.3},
    "hall": {(AP, N3): 1.1, (N1, N3): 1.0, (N3, N1): 0.9, (N2, N3): 0.3},
}


class Floor:
    """Motion scores of a floor: quiet links near 1, links across the room someone moves in higher."""

    def __init__(self, links: list[tuple[str, str]] = LINKS, seed: int = 1) -> None:
        self.links = links
        self.rng = random.Random(seed)

    def scores(self, room: str | None = None, strength: float = 1.0, drop=(), noise=None) -> dict:
        out = {}
        for key in self.links:
            if key in drop or (key[0] in drop or key[1] in drop):
                continue
            mean = ROOMS.get(room, {}).get(key, 0.0) * strength
            if noise and key in noise:
                mean = noise[key]
            sd = 0.25 if mean > 0.2 else 0.06
            out[key] = round(math.exp(self.rng.gauss(mean, sd)), 2)  # reports carry 2 decimals
        return out


def calibrated(floor: Floor | None = None, seconds: int = 60, empty: bool = True, rooms=ROOMS) -> Rooms:
    """Rooms calibrated through runs, one second per step, as the hub drives it."""
    floor = floor or Floor()
    engine = Rooms()
    now = 0.0
    for area in [*rooms, None] if empty else list(rooms):
        engine.start(FLOOR, area, now, seconds)
        while FLOOR in engine.runs:
            now += 1
            engine.step(FLOOR, list(rooms), floor.scores(area), now)
    return engine


@pytest.fixture(scope="module")
def engine() -> Rooms:
    return calibrated()


def classify(engine: Rooms, scores: dict, areas=ROOMS):
    return decide(scores, engine.models(FLOOR, areas))


def accuracy(engine: Rooms, floor: Floor, room: str, n: int = 100, **kwargs) -> tuple[float, float]:
    """Share of seconds the room wins, and the median confidence of those wins."""
    wins = []
    for _ in range(n):
        d = classify(engine, floor.scores(room, **kwargs))
        if d and d.room == room:
            wins.append(d.confidence)
    wins.sort()
    return len(wins) / n, wins[len(wins) // 2] if wins else 0.0


def test_features_are_log_scores():
    assert features({("a", "x"): 1.0, ("b", "x"): math.e, ("c", "x"): None, ("d", "x"): 0.0}) == pytest.approx(
        {("a", "x"): 0.0, ("b", "x"): 1.0, ("d", "x"): math.log(SCORE_FLOOR)}
    )


def test_quiet_floor_is_empty_without_calibration():
    assert decide({}, {}) is None  # no live links: cannot tell
    assert decide({("a", "x"): None}, {}) is None
    d = decide({("a", "x"): 1.2, ("b", "x"): QUIET - 0.01}, {})
    assert (d.room, d.confidence, d.probabilities, d.links) == (None, None, {}, 2)
    assert decide({("a", "x"): 1.2, ("b", "x"): QUIET}, {}) is None  # moving, nothing calibrated
    # With the nodes' motion flags, they decide: a restless link alone is nobody
    d = decide({("a", "x"): 1.2, ("b", "x"): QUIET + 0.3}, {}, moving=False)
    assert (d.room, d.confidence, d.links) == (None, None, 2)
    assert decide({("a", "x"): 1.2, ("b", "x"): QUIET - 0.1}, {}, moving=True) is None  # moving, nothing calibrated


def test_each_room_wins_where_it_disturbs_its_links(engine: Rooms):
    floor = Floor(seed=7)
    for room in ROOMS:
        share, confidence = accuracy(engine, floor, room)
        assert share >= 0.95 and confidence >= 0.9, room
    # Weaker movement (sitting, turning) still lands in the right room, less sure of it
    for room in ROOMS:
        share, confidence = accuracy(engine, floor, room, strength=0.5)
        assert share >= 0.8 and CONFIDENCE <= confidence < 0.999, room


def test_probabilities_cover_every_class(engine: Rooms):
    d = classify(engine, Floor(seed=3).scores("office"))
    assert set(d.probabilities) == {"kitchen", "office", "hall", None}
    assert sum(d.probabilities.values()) == pytest.approx(1.0)
    assert d.room == "office" and d.confidence == max(d.probabilities.values())


def test_empty_class_takes_noise_nobody_makes(engine: Rooms):
    """A link that is busy with nobody there (a fan, an AP changing power) does not make a room."""
    floor = Floor(seed=5)
    busy = {(AP, N2): 0.6}
    engine = Rooms()
    now = 0.0
    for area in [*ROOMS, None]:
        engine.start(FLOOR, area, now, 60)
        while FLOOR in engine.runs:
            now += 1
            engine.step(FLOOR, list(ROOMS), floor.scores(area, noise=busy if area is None else None), now)
    decisions = [classify(engine, floor.scores(None, noise={(AP, N2): 0.7})) for _ in range(50)]
    assert sum(d.room is None for d in decisions) >= 45
    assert accuracy(engine, floor, "office")[0] >= 0.9


def test_links_missing_at_inference_are_skipped(engine: Rooms):
    floor = Floor(seed=11)
    # Node 3 is off: its 5 links are gone, kitchen and office keep enough of theirs
    for room in ("kitchen", "office"):
        share, _ = accuracy(engine, floor, room, drop=(N3,))
        assert share >= 0.9, room
    d = classify(engine, floor.scores("kitchen", drop=(N3,)))
    assert d.links == 4


def test_links_not_calibrated_are_ignored(engine: Rooms):
    """A new node joins after calibration: its links do not count until the rooms are calibrated again."""
    floor = Floor(links=links_of(N1, N2, N3, N4), seed=13)
    for room in ROOMS:
        share, _ = accuracy(engine, floor, room, noise={(N4, N1): 1.5, (N1, N4): 1.5, (AP, N4): 1.5})
        assert share >= 0.95, room
    d = classify(engine, floor.scores("hall"))
    assert d.links == 16 and d.room == "hall"


def test_class_calibrated_before_a_node_joined_still_takes_part():
    floor = Floor(links=links_of(N1, N2, N3, N4), seed=17)
    engine = Rooms()
    now = 0.0
    for area, drop in (("kitchen", (N4,)), ("office", ()), ("hall", ()), (None, ())):
        engine.start(FLOOR, area, now, 60)
        while FLOOR in engine.runs:
            now += 1
            engine.step(FLOOR, list(ROOMS), floor.scores(area, drop=drop), now)
    models = engine.models(FLOOR, ROOMS)
    assert len(models["kitchen"].links) == 9 and len(models["office"].links) == 16
    assert accuracy(engine, floor, "kitchen")[0] >= 0.95
    assert accuracy(engine, floor, "office")[0] >= 0.95


def test_class_without_shared_links_sits_out(engine: Rooms):
    """An area moved to another floor keeps samples of links this floor does not have."""
    upstairs = Floor(links=links_of(N4, "02:57:49:53:50:05"), seed=19)
    other = ClassModel([upstairs.scores() for _ in range(30)])
    models = {**engine.models(FLOOR, ROOMS), "attic": other}
    d = decide(Floor(seed=23).scores("kitchen"), models)
    assert d.room == "kitchen" and "attic" not in d.probabilities
    # Sharing under half the links of the best covered class: sits out too
    few = ClassModel([{key: v for key, v in Floor(seed=29).scores("hall").items() if key[1] == N3} for _ in range(30)])
    d = decide(Floor(seed=31).scores("hall"), {**engine.models(FLOOR, ROOMS), "closet": few})
    assert "closet" not in d.probabilities and d.room == "hall"


def test_only_an_empty_class_cannot_tell_a_room():
    engine = calibrated(rooms={})
    assert engine.models(FLOOR, []).keys() == {None}
    assert classify(engine, Floor().scores("kitchen"), areas=()) is None


def test_one_room_wins_all_motion():
    engine = calibrated(rooms={"kitchen": ROOMS["kitchen"]}, empty=False)
    d = classify(engine, Floor(seed=37).scores("hall"), areas=["kitchen"])
    assert (d.room, d.confidence) == ("kitchen", 1.0)


def test_room_needs_enough_samples():
    floor = Floor()
    engine = calibrated(floor, seconds=19)
    assert engine.models(FLOOR, ROOMS) == {}  # 19 samples each
    assert classify(engine, floor.scores("kitchen")) is None
    engine.start(FLOOR, "kitchen", 1000, 1)
    engine.step(FLOOR, ROOMS, floor.scores("kitchen"), 1001)
    assert list(engine.models(FLOOR, ROOMS)) == ["kitchen"]
    # A link seen in fewer samples than that does not count in the class
    model = ClassModel([{("a", "x"): 0.5, **({("b", "x"): 0.1} if i < 5 else {})} for i in range(30)])
    assert list(model.links) == [("a", "x")]


def test_variance_has_a_floor():
    model = ClassModel([{("a", "x"): 0.4}] * 25)
    assert model.links[("a", "x")] == (pytest.approx(0.4), VAR_FLOOR)
    ll, shared = model.score({("a", "x"): 0.4, ("b", "x"): 1.0})
    assert shared == 1 and math.isfinite(ll)
    assert model.score({("b", "x"): 1.0}) == (-math.inf, 0)


def test_room_runs_record_motion_only():
    floor = Floor()
    engine = Rooms()
    assert engine.start(FLOOR, "kitchen", 0.0, 10) is None
    for t in range(1, 11):
        engine.step(FLOOR, [], floor.scores("kitchen" if t % 2 else None), float(t))
    assert FLOOR not in engine.runs  # ended at its duration
    assert len(engine.areas["kitchen"]) == 5  # still moments say nothing about the room
    # The empty class takes every second, quiet or not; a floor without live links records nothing
    assert engine.start(FLOOR, None, 20.0, 3) is None
    engine.step(FLOOR, [], floor.scores(None), 21.0)
    engine.step(FLOOR, [], {}, 22.0)
    run = engine.runs[FLOOR]
    assert (run.recorded, run.skipped, run.area) == (1, 1, None)
    ended = engine.step(FLOOR, [], floor.scores("hall"), 23.0)
    assert (ended.recorded, ended.skipped) == (2, 1) and len(engine.empty[FLOOR]) == 2


def test_a_delayed_run_records_after_its_delay():
    floor = Floor()
    engine = Rooms()
    engine.start(FLOOR, None, 0.0, 3, delay=2)
    assert (engine.runs[FLOOR].starts, engine.runs[FLOOR].ends) == (2.0, 5.0)
    for t in range(1, 6):  # someone walks out for 2 s, then the floor is empty
        ended = engine.step(FLOOR, [], floor.scores("kitchen" if t <= 2 else None), float(t))
    assert (ended.recorded, ended.skipped) == (3, 0) and len(engine.empty[FLOOR]) == 3


def test_new_run_replaces_the_floors_run_and_clear_stops_it():
    floor = Floor()
    engine = Rooms()
    engine.start(FLOOR, "kitchen", 0.0, 60)
    engine.step(FLOOR, [], floor.scores("kitchen"), 1.0)
    previous = engine.start(FLOOR, "office", 1.0, 60)
    assert (previous.area, previous.recorded) == ("kitchen", 1)
    assert engine.start("upstairs", "attic", 1.0, 60) is None  # another floor records at the same time
    engine.step(FLOOR, [], floor.scores("office"), 2.0)
    engine.clear("office")
    assert FLOOR not in engine.runs and "office" not in engine.areas and "upstairs" in engine.runs
    assert list(engine.areas) == ["kitchen"]
    engine.clear()
    assert (engine.areas, engine.empty, engine.runs) == ({}, {}, {})


def test_samples_are_capped_at_the_latest():
    engine = Rooms(cap=30)
    engine.start(FLOOR, "kitchen", 0.0, 50)
    for t in range(1, 51):
        engine.step(FLOOR, [], {("a", "x"): 2.0 + t / 100}, float(t))
    samples = engine.areas["kitchen"]
    assert len(samples) == 30 and samples[-1] == {("a", "x"): pytest.approx(math.log(2.5))}
    assert samples[0] == {("a", "x"): pytest.approx(math.log(2.21))}


def test_presence_holds_after_the_last_win(engine: Rooms):
    floor = Floor(seed=41)
    rooms = Rooms()
    rooms.areas, rooms.empty = engine.areas, engine.empty
    assert rooms.presence("kitchen", 0.0) is None
    rooms.step(FLOOR, ROOMS, floor.scores("kitchen"), 100.0)
    confidence = rooms.presence("kitchen", 100.0)
    assert confidence >= CONFIDENCE and rooms.decisions[FLOOR].room == "kitchen"
    # Nobody moving: the floor is empty at once, presence holds for the hold time
    rooms.step(FLOOR, ROOMS, floor.scores(None), 101.0)
    assert rooms.decisions[FLOOR].room is None and rooms.decisions[FLOOR].confidence is None
    assert rooms.presence("kitchen", 100.0 + HOLD) == confidence
    assert rooms.presence("kitchen", 100.0 + HOLD + 0.5) is None
    # Another room winning does not end it: two people can be in two rooms
    rooms.step(FLOOR, ROOMS, floor.scores("office"), 110.0)
    assert rooms.presence("kitchen", 110.0) and rooms.presence("office", 110.0)
    # A win below the confidence threshold does not count
    strict = Rooms(confidence=1.01)
    strict.areas, strict.empty = engine.areas, engine.empty
    strict.step(FLOOR, ROOMS, floor.scores("kitchen"), 100.0)
    assert strict.decisions[FLOOR].room == "kitchen" and strict.presence("kitchen", 100.0) is None


def test_storage_round_trip(engine: Rooms):
    data = json.loads(json.dumps(engine.to_dict()))
    assert sorted(data["areas"]) == ["hall", "kitchen", "office"] and list(data["empty"]) == [FLOOR]
    kitchen = data["areas"]["kitchen"]
    assert len(kitchen["links"]) == 9 and len(kitchen["samples"]) == len(engine.areas["kitchen"])
    assert kitchen["links"][0] == f"{N1}>{N2}"
    restored = Rooms()
    restored.load(data)
    floor = Floor(seed=43)
    for _ in range(20):
        scores = floor.scores(random.choice(list(ROOMS)))
        a, b = classify(engine, scores), classify(restored, scores)
        assert a.room == b.room and a.confidence == pytest.approx(b.confidence, abs=1e-3)
    # Missing links stay missing
    partial = Rooms()
    partial.load({"areas": {"den": {"links": [f"{AP}>{N1}", f"{AP}>{N2}"], "samples": [[0.5, None], [None, None]]}}})
    assert list(partial.areas["den"]) == [{(AP, N1): 0.5}]
    assert partial.to_dict()["areas"]["den"] == {"links": [f"{AP}>{N1}"], "samples": [[0.5]]}


@pytest.mark.parametrize(
    "data",
    [
        {"areas": []},
        {"areas": {"den": {"links": ["no-arrow"], "samples": []}}},
        {"areas": {"den": {"links": [f"{AP}>{N1}"], "samples": [[0.1, 0.2]]}}},
        {"empty": {FLOOR: {"samples": []}}},
        {"areas": {"den": {"links": [f"{AP}>{N1}"], "samples": [["x"]]}}},
    ],
)
def test_unreadable_storage_is_refused(engine: Rooms, data):
    rooms = Rooms()
    rooms.load(engine.to_dict())
    with pytest.raises(ValueError):
        rooms.load(data)
    assert sorted(rooms.areas) == ["hall", "kitchen", "office"]  # kept what it had
