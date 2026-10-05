"""Room presence from the links of a floor: calibration samples per room, a classifier per floor.

Once a second a floor gives a feature vector: the log motion score of every live link on it, so
1.0 (as quiet as usual) is 0. Missing links are left out, never taken as 0. Each room learns from
vectors recorded while someone moves in it; each floor learns an empty class while nobody moves
on it. A floor is classified only while one of its links reports motion (the node's detector, so
its Motion threshold, with hysteresis); without those flags, while a link scores QUIET or more.
Replaying 1.5 quiet hours, a link touched QUIET 16 times, the motion flags came on once.
The caller owns the clock (seconds) and says which floor each area is on.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from typing import Any

from .links import LinkKey

type Vector = dict[LinkKey, float]
type ClassId = str | None  # an area, or None for the floor's empty class

QUIET = 1.5  # every live score below this: nobody moving (a node's motion flag turns off here)
CONFIDENCE = 0.6  # a room wins presence from this probability
HOLD = 60.0  # s presence stays on after the room last won: a still person makes little signal
MIN_SAMPLES = 20  # samples before a class takes part, and per link before the link counts in it
SAMPLE_CAP = 600  # latest vectors kept per class: 10 minutes
VAR_FLOOR = 0.01  # per link, in log score: about 10 %
SCORE_FLOOR = 0.1  # a score of 0 has no log


def features(scores: Mapping[LinkKey, float | None]) -> Vector:
    """Log motion score per link; links without a score are left out."""
    return {key: math.log(max(score, SCORE_FLOOR)) for key, score in scores.items() if score is not None}


class ClassModel:
    """Diagonal Gaussian of one class: mean and variance per link, for links seen in enough samples."""

    __slots__ = ("links", "samples")

    def __init__(self, vectors: Iterable[Vector], min_samples: int = MIN_SAMPLES) -> None:
        sums: dict[LinkKey, list[float]] = {}  # count, sum, sum of squares
        self.samples = 0
        for vector in vectors:
            self.samples += 1
            for key, x in vector.items():
                s = sums.setdefault(key, [0, 0.0, 0.0])
                s[0] += 1
                s[1] += x
                s[2] += x * x
        self.links: dict[LinkKey, tuple[float, float]] = {}  # mean, variance
        for key, (n, total, squares) in sums.items():
            if n >= min_samples:
                mean = total / n
                self.links[key] = (mean, max(squares / n - mean * mean, VAR_FLOOR))

    def score(self, vector: Vector) -> tuple[float, int]:
        """Mean log-likelihood per link over the links this class shares with the vector, and how many."""
        total, shared = 0.0, 0
        for key, x in vector.items():
            gauss = self.links.get(key)
            if gauss is None:
                continue
            mean, var = gauss
            total -= 0.5 * ((x - mean) ** 2 / var + math.log(2 * math.pi * var))
            shared += 1
        return (total / shared if shared else -math.inf), shared


@dataclass(frozen=True, slots=True)
class Decision:
    room: str | None  # the winning area; None when nobody is moving
    confidence: float | None  # the winner's probability; None when the quiet rule decided
    probabilities: dict[ClassId, float]  # per class that took part
    motion: float  # largest live score
    links: int  # live links


def decide(
    scores: Mapping[LinkKey, float | None],
    models: Mapping[ClassId, ClassModel],
    quiet: float = QUIET,
    moving: bool | None = None,
) -> Decision | None:
    """One floor, one second. moving: whether a link reports motion (None: judge by quiet). None
    without live links, or with motion and no room to tell."""
    vector = features(scores)
    if not vector:
        return None
    motion = max(score for score in scores.values() if score is not None)
    if motion < quiet if moving is None else not moving:
        return Decision(None, None, {}, motion, len(vector))
    scored = {cls: model.score(vector) for cls, model in models.items()}
    scored = {cls: s for cls, s in scored.items() if s[1]}
    if not scored:
        return None
    # A class sharing under half the links of the best covered one (calibrated before a node
    # joined, or for an area now on another floor) sits out.
    best = max(shared for _, shared in scored.values())
    taking_part = {cls: ll for cls, (ll, shared) in scored.items() if 2 * shared >= best}
    if all(cls is None for cls in taking_part):
        return None
    # The links of a floor see the same person, so their evidence is not independent: the
    # log-likelihood per link keeps the confidence honest and the same scale on every floor.
    top = max(taking_part.values())
    weights = {cls: math.exp(ll - top) for cls, ll in taking_part.items()}
    total = sum(weights.values())
    probabilities = {cls: w / total for cls, w in weights.items()}
    room = max(probabilities, key=probabilities.__getitem__)
    return Decision(room, probabilities[room], probabilities, motion, len(vector))


@dataclass(slots=True)
class Run:
    """A calibration recording on one floor: for a room, or for the floor's empty class."""

    floor: str
    area: str | None  # None: the empty class
    ends: float
    starts: float = -math.inf  # nothing is recorded until after it: time to leave the floor
    recorded: int = 0
    skipped: int = 0  # seconds left out: quiet while recording a room, or no live links


class Rooms:
    """Calibration samples, recording runs, the latest decision per floor and presence per area."""

    def __init__(
        self,
        hold: float = HOLD,
        *,
        quiet: float = QUIET,
        confidence: float = CONFIDENCE,
        min_samples: int = MIN_SAMPLES,
        cap: int = SAMPLE_CAP,
    ) -> None:
        self.hold = hold
        self.quiet = quiet
        self.confidence = confidence
        self.min_samples = min_samples
        self.cap = cap
        self.areas: dict[str, deque[Vector]] = {}
        self.empty: dict[str, deque[Vector]] = {}  # by floor
        self.runs: dict[str, Run] = {}  # by floor, one at a time
        self.decisions: dict[str, Decision | None] = {}  # the latest, by floor
        self.wins: dict[str, tuple[float, float]] = {}  # area: time and confidence of its latest win
        self._models: dict[tuple[bool, str], ClassModel] = {}  # (empty class, area or floor)

    # Calibration

    def start(self, floor: str, area: str | None, now: float, duration: float, delay: float = 0.0) -> Run | None:
        """Record for an area, or the floor's empty class (None), for duration seconds after delay.
        Returns the floor's run it replaced."""
        previous = self.runs.get(floor)
        self.runs[floor] = Run(floor, area, now + delay + duration, now + delay if delay else -math.inf)
        return previous

    def stop(self, floor: str) -> Run | None:
        return self.runs.pop(floor, None)

    def clear(self, area: str | None = None) -> None:
        """Forget an area's samples, or every class. Runs recording what was cleared stop."""
        if area is None:
            self.areas.clear()
            self.empty.clear()
            self.runs.clear()
            self.wins.clear()
            self._models.clear()
            return
        self.areas.pop(area, None)
        self.wins.pop(area, None)
        self._models.pop((False, area), None)
        for floor, run in list(self.runs.items()):
            if run.area == area:
                del self.runs[floor]

    def forget_floor(self, floor: str) -> None:
        """A floor that no longer exists: its empty class, run and decision go."""
        self.empty.pop(floor, None)
        self._models.pop((True, floor), None)
        self.runs.pop(floor, None)
        self.decisions.pop(floor, None)

    def _record(self, run: Run, vector: Vector, motion: float, moving: bool | None) -> None:
        """moving: as in decide, so a room learns from the seconds it will be asked about."""
        still = motion < self.quiet if moving is None else not moving
        if not vector or (run.area is not None and still):
            run.skipped += 1  # a still moment says nothing about where someone is
            return
        empty = run.area is None
        key = run.floor if empty else run.area
        classes = self.empty if empty else self.areas
        if key not in classes:
            classes[key] = deque(maxlen=self.cap)
        classes[key].append(vector)
        self._models.pop((empty, key), None)
        run.recorded += 1

    # Decisions

    def models(self, floor: str, areas: Iterable[str]) -> dict[ClassId, ClassModel]:
        """The classes of a floor with enough samples: its areas, then its empty class."""
        out: dict[ClassId, ClassModel] = {}
        for area in areas:
            if (model := self._model(False, area)) is not None:
                out[area] = model
        if (model := self._model(True, floor)) is not None:
            out[None] = model
        return out

    def _model(self, empty: bool, key: str) -> ClassModel | None:
        samples = (self.empty if empty else self.areas).get(key)
        if samples is None or len(samples) < self.min_samples:
            return None
        model = self._models.get((empty, key))
        if model is None:
            model = self._models[(empty, key)] = ClassModel(samples, self.min_samples)
        return model

    def step(
        self,
        floor: str,
        areas: Iterable[str],
        scores: Mapping[LinkKey, float | None],
        now: float,
        moving: bool | None = None,
    ) -> Run | None:
        """One second of one floor: record for its run, then decide (moving: see decide). Returns
        its run if it just ended."""
        ended = None
        run = self.runs.get(floor)
        if run is not None:
            if run.starts < now <= run.ends:
                vector = features(scores)
                self._record(run, vector, max((s for s in scores.values() if s is not None), default=0.0), moving)
            if now >= run.ends:
                ended = self.runs.pop(floor)
        decision = decide(scores, self.models(floor, areas), self.quiet, moving)
        self.decisions[floor] = decision
        if decision and decision.room is not None and decision.confidence >= self.confidence:
            self.wins[decision.room] = (now, decision.confidence)
        return ended

    def presence(self, area: str, now: float) -> float | None:
        """Confidence of the area's latest win while its presence holds, else None."""
        win = self.wins.get(area)
        return win[1] if win is not None and now - win[0] <= self.hold else None

    # Storage

    def to_dict(self) -> dict[str, Any]:
        """Samples per class: its links once, then one row per sample, None where a link was missing."""
        return {
            "areas": {area: _pack(samples) for area, samples in self.areas.items()},
            "empty": {floor: _pack(samples) for floor, samples in self.empty.items()},
        }

    def load(self, data: Mapping[str, Any]) -> None:
        """Samples from to_dict. Raises ValueError for data it cannot read, and keeps what it had."""
        try:
            areas = {str(area): self._unpack(packed) for area, packed in data.get("areas", {}).items()}
            empty = {str(floor): self._unpack(packed) for floor, packed in data.get("empty", {}).items()}
        except (AttributeError, KeyError, TypeError, ValueError) as err:
            raise ValueError(f"unreadable calibration: {err!r}") from err
        self.areas = {area: samples for area, samples in areas.items() if samples}
        self.empty = {floor: samples for floor, samples in empty.items() if samples}
        self._models.clear()

    def _unpack(self, packed: Mapping[str, Any]) -> deque[Vector]:
        links = [tuple(link.split(">")) for link in packed["links"]]
        if any(len(key) != 2 for key in links):
            raise ValueError("link ids are transmitter>receiver")
        samples: deque[Vector] = deque(maxlen=self.cap)
        for row in packed["samples"]:
            if len(row) != len(links):
                raise ValueError(f"{len(row)} values for {len(links)} links")
            vector = {key: float(x) for key, x in zip(links, row, strict=True) if x is not None}
            if vector:
                samples.append(vector)
        return samples


def _pack(samples: Iterable[Vector]) -> dict[str, Any]:
    samples = list(samples)
    links = sorted({key for vector in samples for key in vector})
    return {
        "links": [f"{tx}>{rx}" for tx, rx in links],
        "samples": [[None if (x := vector.get(key)) is None else round(x, 3) for key in links] for vector in samples],
    }
