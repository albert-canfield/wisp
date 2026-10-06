"""Room tracker: one person per floor, followed by a hidden Markov model over its rooms.

Pure Python, no Home Assistant imports, written to move into custom_components/wisp/engine/.
Inputs each second are what presence.py already has: the live link scores, whether the nodes
confirm motion (the confirmed flags, or presence.py's own check for older firmware), and the
floor's calibration classes (engine/rooms.py ClassModel) and plan rooms.

States: nobody on the floor (EMPTY), and per calibrated room someone
  WALK  walking in it,
  BUSY  still in it but moving a little (typing, shifting in a chair: the still calibration),
  REST  still in it and quiet (reading, sitting still: the links look as if nobody were there).
Transitions follow the plan: a walk goes on in its room or into a room next to it (by default a
drawn room opens only onto the hallway, the undrawn space between the rooms, which touches them
all); the floor is entered and left only through the hallway (and any exit rooms given: rooms
next to the stairs); someone still starts walking in the room they are in. So nobody changes
room without walking, and nobody leaves without walking out. Someone still who never shows
activity fades slowly (a resting person stirs now and then, so quiet seconds count a little
against them), and after quiet_hold seconds without activity may also have left unseen, at
`gone` per second. Nobody stays still in the hallway for long (hall_gone per second).

Emissions per second: each state's class fits the log scores (ClassModel.fit, the mean
log-likelihood per link, times fit_weight; 1 is the softmax Rooms.decide uses). EMPTY and REST
on the empty class, WALK on the room's walking class, BUSY on the better of the room's still class
and the empty class, but only on seconds with activity: on quiet seconds a still class fitted a
drifting empty floor better than the empty class did (an older calibration of the owner's lit a
room for 20 minutes on an empty evening). And whether the nodes confirm activity this second,
P(active | walking, busy, rest, empty): never on the owner's labelled empty floor, every few
seconds to minutes at a desk. REST and EMPTY emit alike: only the walk in and out tells them
apart, which is the point: someone sitting quietly is where they last walked to.

The forward pass gives every state's probability each second; the room shown is the likeliest
(a room's three states summed, against the empty floor). Cost per second: the classes' fits as
today (2R+1 diagonal Gaussians over the live links) and a forward step over 3R+1 states, R the
rooms: about 100 multiply-adds for 5 rooms on top of the fits, and 3R+1 floats of state.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import math

LinkKey = tuple[str, str]
Rect = tuple[float, float, float, float]
Fits = tuple[float, dict[str, float], dict[str, float]]  # empty class, walking classes, still classes
EMPTY, WALK, BUSY, REST = "empty", "walk", "busy", "rest"
SCORE_FLOOR = 0.1  # as engine/rooms.py


@dataclass(frozen=True, slots=True)
class Params:
    """Chosen on the owner's recording of 2026-10-05, leave one window out over four calibrations
    (firmware/tools/study/tune.py and REPORT.md)."""

    # Transitions, per second
    walk_stay: float = 0.80  # a walk goes on in the same room
    walk_stop: float = 0.10  # walking -> still (busy) in the same room
    walk_move: float = 0.10  # walking -> walking in a room next to it (shared among them)
    walk_leave: float = 0.20  # walking in the hallway -> off the floor (each row is normalised)
    start_walk: float = 0.005  # still -> walking in the same room
    calm: float = 0.10  # busy -> rest
    stir: float = 0.005  # rest -> busy
    enter: float = 1e-3  # empty floor -> walking in the hallway
    appear: float = 1e-6  # empty floor -> walking in another room (the way in was missed)
    quiet_hold: float = 600.0  # s without activity before someone still may have left unseen
    gone: float = 0.01  # still -> empty, per second once quiet_hold has passed
    hall_gone: float = 0.05  # still in the hallway -> empty, per second
    # Emissions
    fit_weight: float = 1.0  # the classes' mean log-likelihood per link times this
    p_active: tuple[float, float, float, float] = (0.90, 0.70, 0.003, 0.003)  # walk, busy, rest, empty
    # Output
    switch: float = 0.0  # probability margin before the room shown changes


@dataclass(frozen=True, slots=True)
class Estimate:
    room: str | None  # None: nobody on the floor
    walking: bool
    probabilities: dict[str | None, float]  # per room (its states summed), None: the empty floor
    walking_probability: float


@dataclass
class Classes:
    """From the calibration: per room a walking class and maybe a still class, and the floor's
    empty class; each with fit(vector) -> (mean log-likelihood per link, shared counts), as
    engine/rooms.py ClassModel."""

    walking: Mapping[str, object]
    still: Mapping[str, object]
    empty: object


def from_rooms(engine, floor: str, areas: Iterable[str]) -> Classes | None:
    """The classes of a Rooms engine (engine/rooms.py) with its calibration loaded; None without
    an empty class or any walking class."""
    walking = {a: m for a in areas if (m := engine.model("moving", a)) is not None}
    still = {a: m for a in walking if (m := engine.model("still", a)) is not None}
    empty = engine.model("empty", floor)
    if not walking or empty is None:
        return None
    return Classes(walking, still, empty)


def _touch(a: Rect, b: Rect, tol: float = 0.05, overlap: float = 0.3) -> bool:
    """Rectangles that share a wall over at least overlap metres."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if abs(ax + aw - bx) <= tol or abs(bx + bw - ax) <= tol:
        return min(ay + ah, by + bh) - max(ay, by) >= overlap
    if abs(ay + ah - by) <= tol or abs(by + bh - ay) <= tol:
        return min(ax + aw, bx + bw) - max(ax, bx) >= overlap
    return False


def adjacency(rooms: Iterable[str], drawn: Mapping[str, Sequence[Rect]], hall_only: bool = True) -> dict[str, set[str]]:
    """Rooms next to each room: every room is next to a room not drawn (the hallway, the space
    between); drawn rooms next to each other only when they share a wall and not hall_only."""
    rooms = list(rooms)
    adj: dict[str, set[str]] = {r: set() for r in rooms}
    for i, a in enumerate(rooms):
        for b in rooms[i + 1:]:
            if a not in drawn or b not in drawn:
                ok = True
            elif hall_only:
                ok = False
            else:
                ok = any(_touch(ra, rb) for ra in drawn[a] for rb in drawn[b])
            if ok:
                adj[a].add(b)
                adj[b].add(a)
    return adj


def _logsumexp(values: Sequence[float]) -> float:
    top = max(values)
    if top == -math.inf:
        return -math.inf
    return top + math.log(sum(math.exp(v - top) for v in values))


@dataclass
class RoomTracker:
    classes: Classes
    plan_rooms: Mapping[str, Sequence[Rect]]  # area: rectangles, as the floor plan stores them
    params: Params = field(default_factory=Params)
    hall_only: bool = True  # drawn rooms open only onto the hallway; False: also onto rooms they touch
    exits: Iterable[str] = ()  # rooms besides the hallway that lead off the floor (next to the stairs)

    def __post_init__(self) -> None:
        self.rooms = list(self.classes.walking)
        self.adj = adjacency(self.rooms, self.plan_rooms, self.hall_only)
        self.ways_out = {r for r in self.rooms if r not in self.plan_rooms} | (set(self.exits) & set(self.rooms))
        self.states: list[tuple[str, str | None]] = [(EMPTY, None)]
        for kind in (WALK, BUSY, REST):
            self.states += [(kind, r) for r in self.rooms]
        self.index = {s: i for i, s in enumerate(self.states)}
        self.log_alpha: list[float] | None = None
        self.last_active = -math.inf
        self.shown: str | None = None
        self._transitions: dict[bool, list[list[tuple[int, float]]]] = {}

    def reset(self) -> None:
        """Forget who is where (a restart: the first seconds decide again)."""
        self.log_alpha = None
        self.last_active = -math.inf
        self.shown = None

    # Model

    def transitions(self, quiet: bool) -> list[list[tuple[int, float]]]:
        """Per state, its (next state, log probability); quiet: quiet_hold has passed."""
        if quiet in self._transitions:
            return self._transitions[quiet]
        p = self.params
        rows: list[list[tuple[int, float]]] = []
        for kind, room in self.states:
            nxt: dict[int, float] = {}
            if kind == EMPTY:
                entries = sorted(self.ways_out) or self.rooms
                for r in self.rooms:
                    nxt[self.index[(WALK, r)]] = p.enter / len(entries) if r in entries else p.appear
                nxt[0] = 1 - sum(nxt.values())
            elif kind == WALK:
                if room in self.ways_out:
                    nxt[0] = p.walk_leave
                nxt[self.index[(BUSY, room)]] = p.walk_stop
                nxt[self.index[(WALK, room)]] = p.walk_stay
                near = sorted(self.adj[room])
                for r in near:
                    nxt[self.index[(WALK, r)]] = p.walk_move / len(near)
                total = sum(nxt.values())
                nxt = {j: q / total for j, q in nxt.items()}
            else:
                stays = room in self.classes.still or room in self.plan_rooms  # not the undrawn hallway
                gone = (p.gone if quiet else 0.0) if stays else p.hall_gone
                nxt[self.index[(WALK, room)]] = p.start_walk
                nxt[self.index[(REST if kind == BUSY else BUSY, room)]] = p.calm if kind == BUSY else p.stir
                if gone:
                    nxt[0] = gone
                nxt[self.index[(kind, room)]] = 1 - sum(nxt.values())
            rows.append([(j, math.log(q)) for j, q in nxt.items() if q > 0])
        self._transitions[quiet] = rows
        return rows

    def fits(self, scores: Mapping[LinkKey, float | None]) -> Fits | None:
        """This second's fit of the empty class, each room's walking class and each still class;
        None without live links."""
        vector = {k: math.log(max(s, SCORE_FLOOR)) for k, s in scores.items() if s is not None}
        if not vector:
            return None
        c = self.classes
        return (c.empty.fit(vector)[0], {r: m.fit(vector)[0] for r, m in c.walking.items()},
                {r: m.fit(vector)[0] for r, m in c.still.items()})

    def emissions(self, fits: Fits | None, active: bool) -> list[float]:
        """Log-likelihood of this second under each state, up to a shared constant."""
        p = self.params
        if fits is None:
            return [0.0] * len(self.states)  # no live link: nothing to tell
        fe, walk, still = fits
        busy = {r: max(f, fe) for r, f in still.items()} if active else {}  # still classes: activity only
        act = {
            kind: math.log(q if active else 1 - q)
            for kind, q in zip((WALK, BUSY, REST, EMPTY), p.p_active, strict=True)
        }
        out = []
        for kind, room in self.states:
            f = walk[room] if kind == WALK else busy.get(room, fe) if kind == BUSY else fe
            if f == -math.inf:  # a class sharing no link with this second
                f = fe
            out.append(p.fit_weight * f + act[kind])
        return out

    # Filtering

    def step(self, scores: Mapping[LinkKey, float | None], now: float, active: bool) -> Estimate:
        """One second: scores of the floor's live links, whether the nodes confirm motion."""
        return self.step_fits(self.fits(scores), now, active)

    def step_fits(self, fits: Fits | None, now: float, active: bool) -> Estimate:
        if active:
            self.last_active = now
        emit = self.emissions(fits, active)
        n = len(self.states)
        if self.log_alpha is None:
            alpha = [math.log(0.5) + emit[0]] + [math.log(0.5 / (n - 1)) + e for e in emit[1:]]
        else:
            incoming: list[list[float]] = [[] for _ in range(n)]
            quiet = now - self.last_active > self.params.quiet_hold
            for i, row in enumerate(self.transitions(quiet)):
                a = self.log_alpha[i]
                if a == -math.inf:
                    continue
                for j, lp in row:
                    incoming[j].append(a + lp)
            alpha = [(_logsumexp(v) if v else -math.inf) + emit[j] for j, v in enumerate(incoming)]
        norm = _logsumexp(alpha)
        self.log_alpha = [a - norm for a in alpha]
        rooms: dict[str | None, float] = {None: 0.0}
        walking = 0.0
        for (kind, room), a in zip(self.states, self.log_alpha, strict=True):
            q = math.exp(a)
            rooms[room] = rooms.get(room, 0.0) + q
            if kind == WALK:
                walking += q
        best = max(rooms, key=rooms.__getitem__)
        if self.shown not in rooms or rooms[best] > rooms[self.shown] + self.params.switch:
            self.shown = best
        return Estimate(self.shown, walking > 0.5, rooms, walking)


def viterbi(tracker: RoomTracker, seconds: Sequence[tuple[Fits | None, float, bool]]) -> list[tuple[str, str | None]]:
    """The likeliest state sequence over a whole recording, (fits, time, active) per second:
    offline, it sees the future, so it bounds what filtering can do."""
    n = len(tracker.states)
    last_active = -math.inf
    delta: list[float] | None = None
    back: list[list[int]] = []
    for fits, now, active in seconds:
        if active:
            last_active = now
        emit = tracker.emissions(fits, active)
        if delta is None:
            delta = [math.log(0.5) + emit[0]] + [math.log(0.5 / (n - 1)) + e for e in emit[1:]]
            continue
        best = [-math.inf] * n
        arg = [0] * n
        for i, row in enumerate(tracker.transitions(now - last_active > tracker.params.quiet_hold)):
            d = delta[i]
            if d == -math.inf:
                continue
            for j, lp in row:
                if d + lp > best[j]:
                    best[j], arg[j] = d + lp, i
        top = max(best[j] + emit[j] for j in range(n))
        delta = [best[j] + emit[j] - top for j in range(n)]
        back.append(arg)
    if delta is None:
        return []
    j = max(range(n), key=delta.__getitem__)
    path = [j]
    for arg in reversed(back):
        j = arg[j]
        path.append(j)
    path.reverse()
    return [tracker.states[j] for j in path]
