"""Engine: the room tracker (who is where on a floor) on a synthetic floor. Plain pytest, no Home
Assistant needed. The kitchen and the office are drawn on the plan; the hall is not, so it is the
hallway between them and the way off the floor."""
import pytest

from .test_engine_rooms import AP, FLOOR, N1, N2, N3, ROOMS, Floor
from .test_engine_still import SITTING, calibrate

from engine import Rooms  # noqa: E402  (test_engine_rooms puts the integration on the path)
from engine.tracker import RoomTracker, adjacency, breathing_second, from_rooms  # noqa: E402

DRAWN = ("kitchen", "office")


@pytest.fixture(scope="module")
def engine() -> Rooms:
    return calibrate(Floor(seed=151), still=ROOMS)


class Day:
    """A tracker stepped second by second, as presence.py does."""

    def __init__(self, engine: Rooms, exits=(), seed: int = 153, drawn=DRAWN) -> None:
        self.tracker = RoomTracker(from_rooms(engine, FLOOR, list(ROOMS)), drawn, exits)
        self.floor = Floor(seed=seed)
        self.now = 0.0
        self.estimate = None

    def walk(self, room: str, seconds: int = 1) -> None:
        for _ in range(seconds):
            self._step(self.floor.scores(room), True)

    def sit(self, room: str, seconds: int, every: int = 0, breathing=()) -> list:
        """Someone sitting: the room's links a little busy, activity every `every` seconds (0: none)."""
        rooms = []
        for i in range(seconds):
            self._step(self.floor.scores(room, strength=SITTING), bool(every) and i % every == 0, breathing)
            rooms.append(self.estimate.room)
        return rooms

    def quiet(self, seconds: int, breathing=()) -> list:
        """The links as if nobody were there: nobody, or someone sitting perfectly still."""
        rooms = []
        for i in range(seconds):
            self._step(self.floor.scores(None), False, breathing if i % 4 == 0 else ())
            rooms.append(self.estimate.room)
        return rooms

    def _step(self, scores, active, breathing=()) -> None:
        self.now += 1
        self.estimate = self.tracker.step(scores, self.now, active, breathing)


def test_walking_names_the_room(engine: Rooms):
    day = Day(engine)
    day.walk("kitchen", 5)
    assert (day.estimate.room, day.estimate.walking) == ("kitchen", True)
    assert day.estimate.probabilities["kitchen"] > 0.9 and day.estimate.present("kitchen")


def test_sitting_and_working_keeps_the_room(engine: Rooms):
    """After walking in, someone at a desk (activity every few seconds: the owner's desk had it in
    85% of its seconds) stays in the room, not walking, for as long as they sit; no other room
    lights up."""
    day = Day(engine)
    day.walk("kitchen", 5)
    rooms = day.sit("kitchen", 1800, every=5)
    assert set(rooms) == {"kitchen"} and not day.estimate.walking
    assert not day.estimate.present("office") and not day.estimate.present("hall")


def test_nobody_changes_room_without_walking(engine: Rooms):
    """From the kitchen through the hallway into the office: the office, and the kitchen let go."""
    day = Day(engine)
    day.walk("kitchen", 5)
    day.sit("kitchen", 60, every=20)
    day.walk("hall", 4)
    day.walk("office", 5)
    day.sit("office", 30, every=10)
    assert day.estimate.room == "office" and not day.estimate.present("kitchen")


def test_a_quiet_sitter_is_kept_then_may_have_left(engine: Rooms):
    """Someone who walked in and sits perfectly still looks like nobody: they stay where they walked
    to for minutes (a walk out the tracker missed was shown 341 s on the owner's data), then may
    have left unseen. Breathing keeps them longer (below)."""
    day = Day(engine)
    day.walk("office", 5)
    rooms = day.quiet(240)
    assert set(rooms) == {"office"}
    rooms = day.quiet(900)
    assert rooms[-1] is None


def test_leaving_through_the_hallway_empties_the_floor(engine: Rooms):
    day = Day(engine)
    day.walk("kitchen", 5)
    day.sit("kitchen", 60, every=20)
    day.walk("hall", 5)  # to the stairs
    rooms = day.quiet(60)
    assert rooms[-1] is None and rooms.index(None) < 30


def test_a_way_off_lets_someone_leave_from_its_room(engine: Rooms):
    """Stairs or a door outside next to the office: someone who walked there and is gone is gone.
    Without it, they are taken to sit in the office, where they walked to."""
    stay, leave = Day(engine, seed=155), Day(engine, exits=("office",), seed=155)
    for day in (stay, leave):
        day.walk("office", 5)
        day.quiet(120)
    assert stay.estimate.room == "office" and leave.estimate.room is None


def test_without_a_plan_someone_who_sits_down_stays(engine: Rooms):
    """No room drawn (a flat with only a picture of its plan, or none): the door is in one of the
    rooms, so someone who walks in, sits down (a few seconds of shifting) and then sits quietly is
    kept, not taken to have left; a room ticked as the way off lets them leave from there."""
    plain, door = Day(engine, drawn=(), seed=161), Day(engine, drawn=(), exits=("hall",), seed=161)
    assert plain.tracker.ways_off == set(ROOMS) and door.tracker.ways_off == {"hall"}
    for day in (plain, door):
        day.walk("office", 5)
        day.sit("office", 3, every=1)
        assert set(day.quiet(240)) == {"office"}
    door.walk("hall", 5)
    assert door.quiet(60)[-1] is None


def test_the_empty_floor_stays_empty(engine: Rooms):
    day = Day(engine)
    rooms = day.quiet(1800)
    assert set(rooms[5:]) == {None}


def test_breathing_keeps_a_quiet_sitter(engine: Rooms):
    """Breathing on the room's own links (two transmitters) keeps someone sitting perfectly still
    well past the quiet hold; without it, they may have left."""
    own = from_rooms(engine, FLOOR, list(ROOMS)).own["kitchen"]
    breathing = {(AP, N1), (N2, N1)}
    assert breathing <= own
    still, gone = Day(engine, seed=157), Day(engine, seed=157)
    for day, breath in ((still, breathing), (gone, ())):
        day.walk("kitchen", 5)
        day.quiet(2400, breathing=breath)
    assert still.estimate.room == "kitchen" and gone.estimate.room is None


def test_breathing_elsewhere_does_not_hold_a_room(engine: Rooms):
    """Breathing on the office's links does not keep someone in the kitchen."""
    day = Day(engine, seed=159)
    day.walk("kitchen", 5)
    day.quiet(2400, breathing={(AP, N2), (N3, N2)})
    assert day.estimate.room is None


def test_an_unsure_room_is_not_shown_nor_held_by_breathing(engine: Rooms):
    """Started while someone sits (Home Assistant restarted under them): no walk seen, the rooms
    tie. Nobody is shown rather than a guess, and breathing, which counts only for a room shown,
    cannot make the guess stick (on the owner's floor it held the WC for an hour while he sat in
    the office: his desk shadows one of the WC's links)."""
    day = Day(engine, seed=165)
    tracker = day.tracker
    tracker.alpha = [0.0] * len(tracker.states)
    for room in tracker.rooms:
        tracker.alpha[tracker.index[("busy", room)]] = 1 / len(tracker.rooms)
    own = from_rooms(engine, FLOOR, list(ROOMS)).own["kitchen"]
    rooms = day.quiet(600, breathing={(AP, N1), (N2, N1)})
    assert {(AP, N1), (N2, N1)} <= own and set(rooms) == {None}
    assert max(p for room, p in day.estimate.probabilities.items() if room) < tracker.params.show


def test_breathing_needs_two_transmitters_or_a_link_and_its_reverse():
    assert not breathing_second({(N1, N2)})
    assert not breathing_second({(N3, N1), (N3, N2)})  # one transmitter's two links: its own quirk
    assert breathing_second({(N1, N2), (N3, N2)})
    assert breathing_second({(N1, N2), (N2, N1)})


def test_a_recording_pins_who_is_where(engine: Rooms):
    day = Day(engine)
    estimate = day.tracker.pin("office", True, 10.0)
    assert (estimate.room, estimate.walking) == ("office", True)
    assert day.tracker.pin(None, False, 11.0).room is None
    day.tracker.pin("office", False, 12.0)
    fresh = RoomTracker(from_rooms(engine, FLOOR, list(ROOMS)), DRAWN, ())
    fresh.adopt(day.tracker)
    day.tracker = fresh
    rooms = day.sit("office", 30, every=10)
    assert set(rooms) == {"office"}


def test_rooms_open_onto_the_hallway():
    assert adjacency(["kitchen", "office", "hall"], DRAWN) == {
        "kitchen": {"hall"}, "office": {"hall"}, "hall": {"kitchen", "office"},
    }
    # No room drawn: every room is next to every other, and every room a way off
    assert adjacency(["kitchen", "office"], ()) == {"kitchen": {"office"}, "office": {"kitchen"}}
