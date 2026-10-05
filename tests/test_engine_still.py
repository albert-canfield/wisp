"""Engine: still presence from the signal, still calibration and the separation check, on synthetic
floors. Plain pytest, no Home Assistant needed."""
import json
import math

import pytest

from .test_engine_rooms import AP, FLOOR, N1, N2, N3, ROOMS, Floor, calibrated

from engine import ClassModel, Rooms, features  # noqa: E402  (test_engine_rooms puts the integration on the path)
from engine.rooms import (  # noqa: E402
    CONFIDENCE,
    EMPTY,
    HOLD,
    MOVING,
    SIGNAL,
    SIGNAL_LINKS,
    SIGNAL_VAR_FLOOR,
    SIGNAL_WINDOW,
    STILL,
    STILL_SECONDS,
    decide_still,
    separation,
)

SITTING = 0.12  # a still person's scores: a little above quiet on the room's links, never a motion flag


def step(engine: Rooms, floor: Floor, now: float, room: str | None = None, walking: bool = False,
         areas=ROOMS, **signal):
    scores = floor.scores(room, strength=1.0 if walking else SITTING)
    return engine.step(FLOOR, list(areas), scores, now, signal=floor.signal(room, walking, **signal))


def calibrate(floor: Floor, rooms=ROOMS, still=(), empty: bool = True, seconds: int = 60) -> Rooms:
    """Runs as the hub drives them, with signal: rooms walked in, rooms sat still in, the empty floor."""
    engine = Rooms()
    areas = [*rooms, *(a for a in still if a not in rooms)]
    now = 0.0
    runs = [(area, False) for area in rooms] + [(area, True) for area in still] + ([(None, False)] if empty else [])
    for area, sitting in runs:
        engine.start(FLOOR, area, now, seconds, still=sitting)
        while FLOOR in engine.runs:
            now += 1
            step(engine, floor, now, area, walking=area is not None and not sitting, areas=areas)
    return engine


def fresh(engine: Rooms, **kwargs) -> Rooms:
    """A new engine on the same samples: no wins, decisions or signal readings yet."""
    rooms = Rooms(**kwargs)
    rooms.areas, rooms.still, rooms.empty = engine.areas, engine.still, engine.empty
    return rooms


@pytest.fixture(scope="module")
def engine() -> Rooms:
    return calibrate(Floor(seed=51))


def test_signal_is_averaged_over_a_few_seconds():
    engine = Rooms()
    key, other = (AP, N1), (AP, N2)
    engine.start(FLOOR, None, 0.0, 9)
    for t, rssi in enumerate([-50, -52, -54, -56, -58, -60], 1):
        engine.step(FLOOR, [], {key: 1.0, other: 1.0}, float(t), signal={key: rssi, other: -70 if t < 6 else None})
    latest = engine.empty[FLOOR][-1]
    assert SIGNAL_WINDOW == 5 and latest[(*key, SIGNAL)] == pytest.approx(-56.0)  # -52 to -60
    assert (*other, SIGNAL) not in latest and latest[other] == 0.0  # no frames: no signal, the score stays
    engine.step(FLOOR, [], {key: 1.0, other: 1.0}, 7.0, signal={key: -40, other: -80})
    latest = engine.empty[FLOOR][-1]
    assert latest[(*other, SIGNAL)] == -80.0  # a link missing a second starts over
    assert latest[(*key, SIGNAL)] == pytest.approx(-53.6)
    engine.step(FLOOR, [], {key: 1.0}, 8.0)  # without signal
    assert (*key, SIGNAL) not in engine.empty[FLOOR][-1]


def test_signal_variance_has_a_floor_and_kinds_are_summed():
    model = ClassModel([{("a", "x"): 0.4, ("a", "x", SIGNAL): -50.0}] * 25)
    assert model.links[("a", "x", SIGNAL)] == (pytest.approx(-50.0), SIGNAL_VAR_FLOOR) and model.signal == 1
    ll, shared = model.fit({("a", "x"): 0.4, ("a", "x", SIGNAL): -50.0, ("b", "x", SIGNAL): -60.0})
    score_only = model.fit({("a", "x"): 0.4})[0]
    # The signal's mean per link counts once per SIGNAL_LINKS of the vector's signal features: here 2
    signal = -0.5 * math.log(2 * math.pi * SIGNAL_VAR_FLOOR) * 2 / SIGNAL_LINKS
    assert shared == (1, 1) and ll == pytest.approx(score_only + signal)
    # Left out of its class, a sample is scored as if the class had never seen it
    vectors = [{("a", "x"): 0.1 * i, ("a", "x", SIGNAL): -50.0 - i % 7} for i in range(30)]
    held = ClassModel(vectors).fit(vectors[3], held_out=True)
    assert held[0] == pytest.approx(ClassModel(vectors[:3] + vectors[4:]).fit(vectors[3])[0])


def test_someone_sitting_still_keeps_presence(engine: Rooms):
    """Someone walks into the kitchen and sits down: the signal keeps the room on well past the hold."""
    rooms, floor, now = fresh(engine), Floor(seed=53), 1000.0
    for _ in range(5):
        now += 1
        step(rooms, floor, now, "kitchen", walking=True)
    assert rooms.presence("kitchen", now) >= CONFIDENCE and not rooms.still_present("kitchen", now)
    for _ in range(300):
        now += 1
        step(rooms, floor, now, "kitchen")
        assert rooms.presence("kitchen", now) is not None
    assert rooms.decisions[FLOOR].room is None  # by motion, nobody moves
    still = rooms.still_decisions[FLOOR]
    assert (still.still, still.room) == (True, "kitchen") and still.confidence >= CONFIDENCE
    assert set(still.probabilities) == {"kitchen", "office", "hall", None}
    assert rooms.still_present("kitchen", now) and rooms.streaks[FLOOR] == ("kitchen", 300)
    assert rooms.presence("office", now) is None and rooms.presence("hall", now) is None

    # Leaving: the signal comes back, the empty floor wins and the hold runs out as after motion
    on = []
    for _ in range(int(HOLD) + 15):
        now += 1
        step(rooms, floor, now, None)
        on.append(rooms.presence("kitchen", now) is not None)
    assert all(on[: int(HOLD)]) and on.index(False) <= HOLD + SIGNAL_WINDOW and not any(on[on.index(False):])
    assert rooms.still_decisions[FLOOR].room is None and FLOOR not in rooms.streaks


def test_still_presence_turns_on_after_seconds_in_a_row(engine: Rooms):
    """Someone already sitting in the office when the hub starts: on after STILL_SECONDS, not before."""
    rooms, floor = fresh(engine), Floor(seed=57)
    first = None
    for t in range(1, 30):
        step(rooms, floor, float(t), "office")
        if first is None and rooms.presence("office", float(t)) is not None:
            first = t
    assert STILL_SECONDS <= first <= STILL_SECONDS + 3
    assert rooms.still_present("office", 29.0)
    assert rooms.presence("kitchen", 29.0) is None and rooms.presence("hall", 29.0) is None


def test_each_room_is_told_by_its_own_drop(engine: Rooms):
    for room in ROOMS:
        rooms, floor = fresh(engine), Floor(seed=59)
        for t in range(1, 61):
            step(rooms, floor, float(t), room)
        assert {area for area in ROOMS if rooms.presence(area, 60.0) is not None} == {room}, room


def test_empty_floor_stays_empty(engine: Rooms):
    rooms, floor = fresh(engine), Floor(seed=61)
    for t in range(1, 601):
        step(rooms, floor, float(t), None)
        assert not any(rooms.presence(area, float(t)) for area in ROOMS)
    decision = rooms.still_decisions[FLOOR]
    assert decision.room is None and decision.confidence == max(decision.probabilities.values())


def test_one_link_alike_is_not_a_room(engine: Rooms):
    """The whole pattern counts: a drop on one of the kitchen's links, the others as usual, is nobody."""
    rooms, floor = fresh(engine), Floor(seed=63)
    for t in range(1, 121):
        step(rooms, floor, float(t), None, losses={(AP, N1): 3.0})
    assert not any(rooms.presence(area, 120.0) for area in ROOMS)


def test_signal_unlike_every_class_cannot_tell(engine: Rooms):
    """Node 3 moved: its links are 8 dB weaker. The hall is the nearest class, but nothing fits."""
    rooms, floor = fresh(engine), Floor(seed=67)
    shift = {key: -8.0 for key in floor.links if N3 in key}
    for t in range(1, 121):
        step(rooms, floor, float(t), None, shift=shift)
    decision = rooms.still_decisions[FLOOR]
    assert (decision.room, decision.confidence) == (None, None) and max(decision.probabilities.values()) > 0.9
    assert not any(rooms.presence(area, 120.0) for area in ROOMS)


def test_no_empty_calibration_no_still_presence():
    engine, floor, now = calibrate(Floor(seed=71), empty=False), Floor(seed=73), 1000.0
    assert engine.still_models(FLOOR, ROOMS) == {}
    for _ in range(3):
        now += 1
        step(engine, floor, now, "kitchen", walking=True)
    assert engine.presence("kitchen", now) is not None
    for _ in range(int(HOLD) + 5):
        now += 1
        step(engine, floor, now, "kitchen")
    assert engine.presence("kitchen", now) is None and engine.still_decisions[FLOOR] is None


def test_still_calibration_records_still_seconds():
    floor, engine = Floor(seed=79), Rooms()
    assert engine.start(FLOOR, "kitchen", 0.0, 6, still=True) is None
    assert engine.runs[FLOOR].still
    for t in range(1, 7):
        ended = step(engine, floor, float(t), "kitchen", walking=t in (2, 5))  # fidgeting: those seconds go
    assert (ended.recorded, ended.skipped, ended.still) == (4, 2, True)
    assert list(engine.still) == ["kitchen"] and engine.areas == {}
    assert all(len(vector) == 18 for vector in engine.still["kitchen"])  # 9 log scores, 9 signals
    engine.start(FLOOR, "kitchen", 10.0, 5, still=True)
    assert engine.start(FLOOR, None, 10.0, 5, still=True).still  # replaced the kitchen's
    assert not engine.runs[FLOOR].still  # the empty class has no still kind
    engine.clear("kitchen")
    assert engine.still == {} and FLOOR in engine.runs


def test_still_classes_decide_while_nobody_moves():
    """With still calibration, each room's still class takes over from its moving class's signal."""
    engine = calibrate(Floor(seed=83), still=("kitchen", "office"))
    models = engine.still_models(FLOOR, ROOMS)
    assert models["kitchen"] is engine._model(STILL, "kitchen") and models["hall"].samples == 60  # hall: borrowed
    assert models["hall"].links[(AP, N3)] == models[None].links[(AP, N3)]  # its log scores: the empty floor's
    for room in ROOMS:
        rooms, floor = fresh(engine), Floor(seed=89)
        for t in range(1, 41):
            step(rooms, floor, float(t), room)
        assert {area for area in ROOMS if rooms.presence(area, 40.0) is not None} == {room}, room
    # Log scores without any signal: nothing to hold against the empty floor's
    assert decide_still(features(Floor(seed=97).scores("kitchen", strength=SITTING)), models) is None


def test_samples_from_before_signal_still_load_and_classify():
    old = calibrated()  # moving and empty classes from log scores only
    data = json.loads(json.dumps(old.to_dict()))
    assert "signal" not in data["areas"]["kitchen"] and data["still"] == {}
    del data["still"]  # stored before still classes existed
    engine = Rooms()
    engine.load(data)
    assert sorted(engine.areas) == sorted(ROOMS) and engine.still == {}
    floor, now = Floor(seed=101), 1000.0
    step(engine, floor, now, "office", walking=True)
    assert engine.decisions[FLOOR].room == "office" and engine.presence("office", now) >= CONFIDENCE
    assert engine.still_models(FLOOR, ROOMS) == {}  # no signal in the empty class: no reference yet
    # Calibrating again brings signal; the old samples stay and count for the log scores
    engine.start(FLOOR, None, now, 60)
    while FLOOR in engine.runs:
        now += 1
        step(engine, floor, now, None)
    assert len(engine.empty[FLOOR]) == 120 and engine._model(EMPTY, FLOOR).signal == 9
    assert engine.still_models(FLOOR, ROOMS).keys() == {None}  # the rooms have no signal yet
    engine.start(FLOOR, "office", now, 60, still=True)
    while FLOOR in engine.runs:
        now += 1
        step(engine, floor, now, "office")
    for _ in range(int(HOLD) + 30):
        now += 1
        step(engine, floor, now, "office")
    assert engine.still_present("office", now)


def test_storage_keeps_signal_and_still_classes():
    engine = calibrate(Floor(seed=103), rooms={"kitchen": ROOMS["kitchen"]}, still=("kitchen",), seconds=25)
    data = json.loads(json.dumps(engine.to_dict()))
    assert sorted(data) == ["areas", "empty", "still"]
    still = data["still"]["kitchen"]
    assert len(still["links"]) == 9 and len(still["samples"]) == len(still["signal"]) == 25
    assert all(isinstance(x, float) and -70 < x < -35 for x in still["signal"][0])
    restored = Rooms()
    restored.load(data)
    assert restored.still["kitchen"][0] == pytest.approx(engine.still["kitchen"][0], abs=0.01)
    assert restored.to_dict() == data
    # Signal rows must match the samples
    bad = {"still": {"den": {"links": [f"{AP}>{N1}"], "samples": [[0.1]], "signal": [[-50.0], [-51.0]]}}}
    with pytest.raises(ValueError):
        restored.load(bad)
    partial = Rooms()
    partial.load({"still": {"den": {"links": [f"{AP}>{N1}", f"{AP}>{N2}"], "samples": [[0.1, None]],
                                    "signal": [[None, -60.0]]}}})
    assert list(partial.still["den"]) == [{(AP, N1): 0.1, (AP, N2, SIGNAL): -60.0}]


def test_separation_of_well_told_classes():
    engine = calibrate(Floor(seed=107), still=("kitchen",))
    result = separation(engine.classes(FLOOR, ROOMS))
    by_class = {s.cls: s for s in result}
    assert set(by_class) == {(EMPTY, None), *((MOVING, room) for room in ROOMS), (STILL, "kitchen")}
    for s in result:
        assert s.samples == 60 and s.correct >= 0.9, s
    assert separation({(EMPTY, None): engine.empty[FLOOR]}) == []  # nothing to tell apart


def test_separation_finds_classes_that_look_alike():
    """A study calibrated walking where the office is, and a den sat still where no link passes."""
    floor = Floor(seed=109)
    engine = calibrate(floor, still=("kitchen",))
    now = 1000.0
    for area, still, room in (("study", False, "office"), ("den", True, None)):
        engine.start(FLOOR, area, now, 60, still=still)
        while FLOOR in engine.runs:
            now += 1
            step(engine, floor, now, room, walking=not still)
    by_class = {s.cls: s for s in separation(engine.classes(FLOOR, [*ROOMS, "study", "den"]), limit=40)}
    office, study, den = by_class[(MOVING, "office")], by_class[(MOVING, "study")], by_class[(STILL, "den")]
    assert office.confused_with == (MOVING, "study") and study.confused_with == (MOVING, "office")
    assert office.correct < 0.8 and study.correct < 0.8 and office.correct + office.confused == pytest.approx(1.0)
    assert den.confused_with == (EMPTY, None) and den.correct < 0.8
    assert by_class[(STILL, "kitchen")].correct >= 0.9 and by_class[(MOVING, "hall")].correct >= 0.9
