"""Room tracker: one person per floor, followed by a hidden Markov model over its rooms.

States: nobody on the floor (EMPTY), and per room with a walking calibration someone
  WALK  walking in it,
  BUSY  still in it and moving a little (typing, shifting in a chair: the still calibration),
  REST  still in it and quiet (reading, sitting still: the links look as if nobody were there).
Moves follow the floor plan. Rooms drawn on it open only onto the rooms not drawn (the hallway:
the space between them, which touches them all); without a calibrated room undrawn, or without
rooms drawn, every room is next to every other. The floor is entered and left only through a way
off: an undrawn room while others are drawn, or a room marked as an exit (stairs or a door outside
next to it). Without either (no plan, or only a picture of one) every room is a way off at 1/N of
the rate, the door being in one of N rooms: otherwise anyone who stops walking and sits quietly
would as likely have left. Someone still starts walking in the room they are in. So nobody
changes room without walking, and nobody leaves without walking out. Someone still who shows no
activity for quiet_hold seconds may also have left unseen, at `gone` per second; nobody stays
still for long in a room no one sits in (undrawn while others are drawn, and without a still
calibration: a hallway).

Emissions per second: each state's class fits the live log scores (Fits, shared with Rooms'
moving decision; the mean log-likelihood per link times fit_weight, 1 being the softmax Rooms
uses). EMPTY and REST on the empty class, WALK on the room's walking class, BUSY on the better of
the room's still class and the empty class, but only on seconds with activity: on quiet seconds
a still class fitted a drifting empty floor better than the empty class did (an older calibration
of the owner's lit a room for 20 minutes on an empty evening). And whether the nodes confirm
activity this second, P(active | walking, busy, rest, empty): never on the owner's labelled empty
floor, every few seconds to minutes at a desk. Without breathing, REST and EMPTY emit alike: only
the walk in and out tells them apart, which is the point: someone sitting quietly is where they
last walked to.

Breathing (node firmware 0.1.7, detection switched on): a second counts as breathing when links
from at least two transmitters show it, or a link and its reverse (breathing_second): the owner's
night of 2026-10-06 (8 h, nobody downstairs) had 28 s of one link breathing in 3 short episodes
and no second with two, a quiet morning had the C3 node's own two links breathing with neither
reverse, while sitting quietly showed 1 to 7 of 16 links. It is
evidence for someone resting or busy in the room shown when that room's own links are among
them (Rooms.own_links: the links its walking class disturbs most): P(breathing | rest there) 0.3,
P(breathing | busy there) 0.05 (desk work breathes on fewer links: any link 19% of evaluations,
against 68% sitting quietly), against P(breathing | nobody, walking, or anyone elsewhere) 0.001,
ten times the night's bound (no two-link second in 28,800, so at most about 1e-4). A second
without breathing says nothing: the detection is off by default, and a quiet sitter shows it only
in some seconds. Breathing counts only for the room shown, so it never starts presence or moves
it to another room: someone must walk in.

The forward pass gives every state's probability each second; the room shown is the likeliest
(a room's three states summed, against the empty floor), and only while it holds `show` (0.6) of
the probability: started under someone sitting (Home Assistant restarted), with no walk seen, the
rooms tie and a guess would stick, breathing counting for the room shown. On the owner's labelled
evening (firmware/tools/study/REPORT.md) it got every present second right, with no room changes
while sitting, against 84% for the hand rules it replaced (an occupied desk lost after 3 quiet
minutes); error over the labelled seconds 0.0% (1.4% showing the likeliest room however unsure,
3.4% then leaving one window out), walking recall 84% against 57%. A walk out it misses leaves
someone shown for about 5 minutes (292 s replayed).
Cost per second: the classes' fits (2R+1 diagonal Gaussians over the live links, R rooms, shared
with Rooms) and a forward step over 3R+1 states (about 100 multiply-adds for 5 rooms).
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
import math
from typing import TYPE_CHECKING

from .links import LinkKey
from .rooms import EMPTY as EMPTY_CLASS, MOVING, STILL, ClassModel, Fits, features, taking_part

if TYPE_CHECKING:
    from .rooms import Rooms

EMPTY, WALK, BUSY, REST = "empty", "walk", "busy", "rest"  # states
PRESENT = 0.5  # a room's presence from this probability (or while it is the room shown)
def breathing_second(links: Collection[LinkKey]) -> bool:
    """Whether the floor's breathing links make this a breathing second: links from at least two
    transmitters, or a link and its reverse. One transmitter alone is not someone: on the owner's
    floor the C3 node's links showed breathing on two links it sent, neither reverse, with the
    floor quiet; a body changes a link both ways and the links around it."""
    links = set(links)
    return len({tx for tx, _ in links}) >= 2 or any((rx, tx) in links for tx, rx in links)


@dataclass(frozen=True, slots=True)
class Params:
    """Per second. Chosen on the owner's labelled evening of 2026-10-05, leave one window out over
    four calibration snapshots (firmware/tools/study/tune.py and REPORT.md: every fold chose doors
    only to the hallway and fit weight 1; the error moved most with P(active | busy), fit_weight,
    start_walk and calm). Breathing from the night of 2026-10-06 and docs/TUNING.md."""

    # Transitions
    walk_stay: float = 0.80  # a walk goes on in the same room
    walk_stop: float = 0.10  # walking -> busy in the same room (straight to rest instead, at 0.01: 4.1%,
    # and the empty floor shown occupied after the labelled walk out: still on the floor means settling in)
    walk_move: float = 0.10  # walking -> walking in a room next to it, shared among them
    walk_leave: float = 0.20  # walking in a way off -> off the floor (each row is normalised: 0.17)
    start_walk: float = 0.005  # still -> walking in the same room (doubled: 3.7% error to 6.9%)
    calm: float = 0.10  # busy -> rest (doubled: 7.8%)
    stir: float = 0.005  # rest -> busy (doubled: 4.1%, and a missed walk out shown 223 s, not 341)
    enter: float = 1e-3  # empty floor -> walking in a way off, shared among them
    appear: float = 1e-6  # empty floor -> walking in another room (the way in was missed)
    quiet_hold: float = 600.0  # s without activity before someone still may have left unseen
    gone: float = 0.01  # still -> empty, per second once quiet_hold has passed
    hall_gone: float = 0.05  # still in a room nobody stays in -> empty
    # Emissions
    fit_weight: float = 1.0  # times each class's mean log-likelihood per link (halved: 12% error)
    p_active: tuple[float, float, float, float] = (0.90, 0.70, 0.003, 0.003)  # walk, busy, rest, empty
    # (busy 0.5 instead: 10.5%; the busy desk at 21:23 was active 85% of its seconds)
    p_breathing: tuple[float, float, float] = (0.30, 0.05, 0.001)  # rest, busy there; anyone else
    # Output
    switch: float = 0.0  # probability margin before the room shown changes
    show: float = 0.6  # probability a room needs to be shown; below it, nobody is shown. On the
    # labelled evening 0.6 and 0.7: 0.0 and 0.1% error (0: 1.4%), 0.8 missed present seconds. Started
    # while the owner sat at his desk (after an update), the rooms tied at 0.2 to 0.26 and the WC,
    # a hair ahead, was shown and then held by breathing on its links (the desk shadows one)


@dataclass(frozen=True, slots=True)
class Estimate:
    room: str | None  # where the floor's person is; None: nobody on the floor
    walking: bool  # the walking states hold over half the probability
    probabilities: dict[str | None, float]  # per room, its states summed; None: the empty floor
    walking_probability: float

    @property
    def confidence(self) -> float | None:
        """The probability of what is shown: the room, or the empty floor; None without any (no
        classes to weigh yet)."""
        return self.probabilities.get(self.room)

    def present(self, area: str) -> bool:
        """The area's presence: the room shown, or one with a probability of PRESENT or more."""
        return area == self.room or self.probabilities.get(area, 0.0) >= PRESENT


@dataclass(frozen=True, slots=True)
class Classes:
    """A floor's calibration as the tracker uses it: per room a walking class and maybe a still
    class, the floor's empty class, and per room its own links (for breathing)."""

    walking: Mapping[str, ClassModel]
    still: Mapping[str, ClassModel]
    empty: ClassModel
    own: Mapping[str, frozenset[LinkKey]] = field(default_factory=dict)


def from_rooms(rooms: Rooms, floor: str, areas: Iterable[str]) -> Classes | None:
    """The classes of a floor from Rooms (rooms.py); None without an empty class or any walking
    class: without them there is nothing to follow anyone by."""
    walking = {a: m for a in areas if (m := rooms.model(MOVING, a)) is not None}
    empty = rooms.model(EMPTY_CLASS, floor)
    if not walking or empty is None:
        return None
    still = {a: m for a in walking if (m := rooms.model(STILL, a)) is not None}
    return Classes(walking, still, empty, {a: frozenset(rooms.own_links(a)) for a in walking})


def adjacency(rooms: Collection[str], drawn: Collection[str]) -> dict[str, set[str]]:
    """The rooms next to each room. Drawn rooms open only onto the undrawn ones (the hallway, the
    space between them): touching rooms counted as next to each other did worse in every fold of
    the study. Without an undrawn room every room is next to every other."""
    between = set(rooms) - set(drawn)
    return {
        r: {o for o in rooms if o != r and (not between or r in between or o in between)}
        for r in rooms
    }


class RoomTracker:
    """One floor's person, second by second: step() with the floor's fits, activity and breathing."""

    def __init__(
        self,
        classes: Classes,
        drawn: Collection[str] = (),
        exits: Collection[str] = (),
        params: Params | None = None,
    ) -> None:
        """drawn: the areas drawn on the floor's plan; exits: rooms that lead off the floor besides
        the undrawn ones."""
        self.classes = classes
        self.params = p = params or Params()
        self.rooms = sorted(classes.walking)
        rooms = set(self.rooms)
        drawn = set(drawn) & rooms
        self.adjacent = adjacency(self.rooms, drawn)
        self.ways_off = ((rooms - drawn) if drawn else set()) | (set(exits) & rooms)
        self.leave = p.walk_leave
        if not self.ways_off:  # no idea where the door is: in one of the rooms
            self.ways_off, self.leave = rooms, p.walk_leave / len(rooms)
        self.stays = {r for r in self.rooms if r in classes.still or r in drawn or not drawn}  # where someone can sit
        self.states: list[tuple[str, str | None]] = [(EMPTY, None)]
        for kind in (WALK, BUSY, REST):
            self.states += [(kind, r) for r in self.rooms]
        self.index = {s: i for i, s in enumerate(self.states)}
        n = len(self.states)
        self._prior = [0.5] + [0.5 / (n - 1)] * (n - 1)  # a restart: the first seconds decide
        self._incoming = {quiet: self._transitions(quiet) for quiet in (False, True)}
        active = {kind: q for kind, q in zip((WALK, BUSY, REST, EMPTY), p.p_active, strict=True)}
        self._act = {
            flag: [math.log(active[kind] if flag else 1 - active[kind]) for kind, _ in self.states]
            for flag in (False, True)
        }
        rest, busy, other = p.p_breathing
        self._breath = (math.log(rest / other), math.log(busy / other))  # against everyone else
        self.alpha: list[float] | None = None
        self.last_active = -math.inf
        self.last_breathing = -math.inf  # the floor's latest breathing second
        self.shown: str | None = None

    def _transitions(self, quiet: bool) -> list[list[tuple[int, float]]]:
        """Per state, where its probability comes from: (previous state, probability). quiet:
        quiet_hold has passed without activity."""
        p, index = self.params, self.index
        rows: list[dict[int, float]] = []
        for kind, room in self.states:
            nxt: dict[int, float] = {}
            if kind == EMPTY:
                for r in self.rooms:
                    nxt[index[(WALK, r)]] = p.enter / len(self.ways_off) if r in self.ways_off else p.appear
                nxt[0] = 1 - sum(nxt.values())
            elif kind == WALK:
                if room in self.ways_off:
                    nxt[0] = self.leave
                nxt[index[(BUSY, room)]] = p.walk_stop
                nxt[index[(WALK, room)]] = p.walk_stay
                near = sorted(self.adjacent[room])
                for r in near:
                    nxt[index[(WALK, r)]] = p.walk_move / len(near)
                total = sum(nxt.values())
                nxt = {j: q / total for j, q in nxt.items()}
            else:
                gone = (p.gone if quiet else 0.0) if room in self.stays else p.hall_gone
                nxt[index[(WALK, room)]] = p.start_walk
                nxt[index[(REST if kind == BUSY else BUSY, room)]] = p.calm if kind == BUSY else p.stir
                if gone:
                    nxt[0] = gone
                nxt[index[(kind, room)]] = 1 - sum(nxt.values())
            rows.append(nxt)
        incoming: list[list[tuple[int, float]]] = [[] for _ in self.states]
        for i, nxt in enumerate(rows):
            for j, q in nxt.items():
                if q > 0:
                    incoming[j].append((i, q))
        return incoming

    def reset(self) -> None:
        """Forget who is where (a restart: the first seconds decide again)."""
        self.alpha = None
        self.last_active = self.last_breathing = -math.inf
        self.shown = None

    def pin(self, room: str | None, walking: bool, now: float) -> Estimate:
        """While a calibration records, its instructions say where everyone is: walking or still
        in its room, or off the floor (room None). A room the tracker does not know yet (its first
        walking calibration) starts it over."""
        n = len(self.states)
        if room is None:
            self.alpha = [1.0] + [0.0] * (n - 1)
            self.shown = None
        elif room in self.classes.walking:
            self.alpha = [0.0] * n
            self.alpha[self.index[(WALK if walking else BUSY, room)]] = 1.0
            self.last_active = now
            self.shown = room
        else:
            self.reset()
        return recorded(room, walking)

    def adopt(self, old: RoomTracker) -> None:
        """Carry on from a tracker of the same floor built before the calibration or the plan
        changed: states it shares keep their probability; with none left, start over."""
        alpha = [0.0] * len(self.states)
        if old.alpha is not None:
            for state, q in zip(old.states, old.alpha, strict=True):
                if (j := self.index.get(state)) is not None:
                    alpha[j] = q
        total = sum(alpha)
        self.alpha = [q / total for q in alpha] if total > 0 else None
        self.last_active, self.last_breathing = old.last_active, old.last_breathing
        self.shown = old.shown if old.shown in self.classes.walking else None

    # Each second

    def fit(self, scores: Mapping[LinkKey, float | None]) -> Fits | None:
        """This second's fits from the scores, for use without Rooms (Rooms.step shares its own)."""
        vector = features(scores)
        if not vector:
            return None
        c = self.classes
        return Fits(c.empty.fit(vector), {r: m.fit(vector) for r, m in c.walking.items()},
                    {r: m.fit(vector) for r, m in c.still.items()})

    def step(
        self, scores: Mapping[LinkKey, float | None], now: float, active: bool, breathing: Collection[LinkKey] = ()
    ) -> Estimate:
        return self.step_fits(self.fit(scores), now, active, breathing)

    def step_fits(self, fits: Fits | None, now: float, active: bool, breathing: Collection[LinkKey] = ()) -> Estimate:
        """One second: the floor's fits (None without live links), whether the nodes confirm
        motion, and the links that show breathing."""
        if active:
            self.last_active = now
        emit = self._emissions(fits, active, breathing, now)
        top = max(emit)
        like = [math.exp(e - top) for e in emit]
        if self.alpha is None:
            prior = self._prior
        else:
            alpha = self.alpha
            incoming = self._incoming[now - self.last_active > self.params.quiet_hold]
            prior = [sum(alpha[i] * q for i, q in row) for row in incoming]
        post = [a * b for a, b in zip(prior, like, strict=True)]
        total = sum(post)
        if not total > 0:  # every likely state underflowed: this second alone decides
            post, total = like, sum(like)
        self.alpha = [q / total for q in post]
        return self._estimate()

    def _emissions(self, fits: Fits | None, active: bool, breathing: Collection[LinkKey], now: float) -> list[float]:
        """Log-likelihood of this second under each state, up to a shared constant."""
        out = list(self._act[active])
        r = len(self.rooms)
        if fits is not None and fits.empty is not None:
            scored = {(WALK, a): f for a, f in fits.walking.items()} | {(BUSY, a): f for a, f in fits.still.items()}
            scored[(EMPTY, None)] = fits.empty
            counted = set(taking_part(scored))
            if (EMPTY, None) in counted:  # the reference: without it, nothing to tell
                w = self.params.fit_weight
                fe = fits.empty[0]
                out[0] += w * fe
                for k, room in enumerate(self.rooms, 1):
                    f = fits.walking.get(room)
                    out[k] += w * (f[0] if f is not None and (WALK, room) in counted else fe)
                    busy = fe
                    if active and (f := fits.still.get(room)) is not None and (BUSY, room) in counted:
                        busy = max(f[0], fe)  # still classes: activity only
                    out[k + r] += w * busy
                    out[k + 2 * r] += w * fe
        if breathing_second(breathing):
            self.last_breathing = now
            room = self.shown
            if room is not None and self.classes.own.get(room, frozenset()) & set(breathing):
                k = self.rooms.index(room) + 1
                out[k + 2 * r] += self._breath[0]
                out[k + r] += self._breath[1]
        return out

    def _estimate(self) -> Estimate:
        alpha, r = self.alpha, len(self.rooms)
        rooms: dict[str | None, float] = {None: alpha[0]}
        walking = 0.0
        for k, room in enumerate(self.rooms, 1):
            walk = alpha[k]
            walking += walk
            rooms[room] = walk + alpha[k + r] + alpha[k + 2 * r]
        best = max(rooms, key=rooms.__getitem__)
        if self.shown not in rooms or rooms[best] > rooms[self.shown] + self.params.switch:
            self.shown = best
        if self.shown is not None and rooms[self.shown] < self.params.show:
            self.shown = None  # someone, maybe, but where is unclear
        return Estimate(self.shown, walking > 0.5, rooms, walking)


def recorded(room: str | None, walking: bool) -> Estimate:
    """What a calibration's instructions say: someone walking or still in room, or nobody (None)."""
    walking = walking and room is not None
    return Estimate(room, walking, {room: 1.0}, 1.0 if walking else 0.0)


def from_decision(decision, confidence: float) -> Estimate:
    """A floor without an empty class has no tracker: the room someone moves in now, from Rooms'
    moving decision when it is at least confidence sure, and nobody otherwise."""
    room = decision.room if decision is not None and (decision.confidence or 0.0) >= confidence else None
    probabilities = dict(decision.probabilities) if decision is not None else {}
    return Estimate(room, room is not None, probabilities, 1.0 if room is not None else 0.0)
