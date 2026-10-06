"""Room presence from the links of a floor: calibration samples per room, a classifier per floor.

Once a second a floor gives a feature vector: the log motion score of every live link on it, so
1.0 (as quiet as usual) is 0, and its signal (RSSI in dBm, the mean of the latest SIGNAL_WINDOW
seconds), since a body near a link's path weakens it. Missing links are left out, never taken as 0.
Each room learns from vectors recorded while someone moves in it and, apart, while someone sits
still in it; each floor learns an empty class while nobody is on it.

Each second every class of the floor is fitted once to the live log scores (Fits): the moving
decision here and the room tracker (tracker.py), which says who is where over time, share them.
The moving decision classifies the floor only while one of its links reports motion (the node's
detector, so its Motion threshold, with hysteresis); without those flags, while a link scores QUIET
or more. Replaying 1.5 quiet hours, a link touched QUIET 16 times, the motion flags came on once.
It picks among the rooms' moving classes and the empty class, and stays for diagnostics and for
floors without an empty class, where the tracker has no reference.

The still classification (log scores and signal, the empty class against each room's still
class) names no rooms: signal strength drifts more with nobody there than a seated person
changes it. It stays for the separation check and diagnostics, worked out only when asked.
The caller owns the clock (seconds) and says which floor each area is on.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any

from .links import LinkKey

type SignalKey = tuple[str, str, str]  # (transmitter, receiver, SIGNAL)
type Feature = LinkKey | SignalKey
type Vector = dict[Feature, float]
type ClassId = str | None  # an area, or None for the floor's empty class
type ClassKey = tuple[str, str | None]  # (EMPTY, None), or (MOVING or STILL, area)
type Fit = tuple[float, tuple[int, int]]  # ClassModel.fit: mean log-likelihood per feature, features shared

QUIET = 1.5  # every live score below this: nobody moving (a node's motion flag turns off here)
CONFIDENCE = 0.6  # a moving decision names its room from this probability
MIN_SAMPLES = 20  # samples before a class takes part, and per feature before it counts in the class
SAMPLE_CAP = 600  # latest vectors kept per class: 10 minutes
VAR_FLOOR = 0.01  # per link, in log score: about 10 %
SCORE_FLOOR = 0.1  # a score of 0 has no log
SIGNAL = "rssi"  # last part of a signal feature's key
SIGNAL_WINDOW = 5  # s: a link's signal is its mean RSSI over the latest readings, one a second
SIGNAL_VAR_FLOOR = 1.0  # dB squared, per link: one link's noise does not dominate
SIGNAL_LINKS = 4  # links a still body weakens, about: the signal's mean log-likelihood per link
# counts once per this many live links, so a few weakened links are not diluted on a large floor
OWN_LINKS = 0.5  # a room's own links: those its walking class disturbs at least this share of its most disturbed
STILL_FIT = 4.0  # mean squared z-score of the winner's signal at most: beyond, the signal is unlike
# every class (a node moved, the empty floor changed) and the still classification cannot tell
SEPARATION_SAMPLES = 60  # samples tried per class in the separation check, spread over its recording
EMPTY, MOVING, STILL = "empty", "moving", "still"  # kinds of class
_LOG_2PI = math.log(2 * math.pi)


def features(scores: Mapping[LinkKey, float | None]) -> Vector:
    """Log motion score per link; links without a score are left out."""
    return {key: math.log(max(score, SCORE_FLOOR)) for key, score in scores.items() if score is not None}


def signal_features(signal: Mapping[LinkKey, float]) -> Vector:
    """Signal (dBm) per link, keyed (transmitter, receiver, SIGNAL)."""
    return {(tx, rx, SIGNAL): float(rssi) for (tx, rx), rssi in signal.items()}


def _var_floor(key: Feature) -> float:
    return SIGNAL_VAR_FLOOR if len(key) == 3 else VAR_FLOOR


class ClassModel:
    """Diagonal Gaussian of one class: mean and variance per feature, for features seen in enough samples."""

    __slots__ = ("links", "samples", "signal", "sums", "terms")

    def __init__(self, vectors: Iterable[Vector], min_samples: int = MIN_SAMPLES) -> None:
        sums: dict[Feature, list[float]] = {}  # count, sum, sum of squares
        self.samples = 0
        for vector in vectors:
            self.samples += 1
            for key, x in vector.items():
                s = sums.setdefault(key, [0, 0.0, 0.0])
                s[0] += 1
                s[1] += x
                s[2] += x * x
        self.sums = {key: s for key, s in sums.items() if s[0] >= min_samples}
        self.links: dict[Feature, tuple[float, float]] = {}  # per feature: mean, variance
        for key, (n, total, squares) in self.sums.items():
            mean = total / n
            self.links[key] = (mean, max(squares / n - mean * mean, _var_floor(key)))
        self._prepare()

    def _prepare(self) -> None:
        """Per feature its mean, 1 / (2 variance) and log normaliser, so a fit takes no log: it runs
        for every class of a floor each second."""
        self.terms = {key: (mean, 0.5 / var, 0.5 * (_LOG_2PI + math.log(var))) for key, (mean, var) in self.links.items()}
        self.signal = sum(len(key) == 3 for key in self.links)  # signal features

    @classmethod
    def borrowed(cls, scores: ClassModel, signal: ClassModel) -> ClassModel:
        """The log scores of one class with the signal of another."""
        model = cls(())
        model.links = {k: g for k, g in scores.links.items() if len(k) == 2} | {
            k: g for k, g in signal.links.items() if len(k) == 3
        }
        model._prepare()
        model.samples = signal.samples
        return model

    def score(self, vector: Vector) -> tuple[float, int]:
        """Mean log-likelihood per feature over the features this class shares with the vector, and how many."""
        total, shared = 0.0, 0
        for key, x in vector.items():
            gauss = self.links.get(key)
            if gauss is None:
                continue
            mean, var = gauss
            total -= 0.5 * ((x - mean) ** 2 / var + math.log(2 * math.pi * var))
            shared += 1
        return (total / shared if shared else -math.inf), shared

    def fit(self, vector: Vector, held_out: bool = False) -> Fit:
        """Mean log-likelihood per feature of each kind (log scores, signal) the class shares with
        the vector, the signal's weighted by the vector's signal features over SIGNAL_LINKS, and
        summed: motion and attenuation are two views of one person. And how many of each kind it
        shares. held_out: the vector is one of this class's samples, left out of it."""
        totals, shared, signal = [0.0, 0.0], [0, 0], 0
        terms = self.terms
        for key, x in vector.items():
            kind = len(key) - 2  # 0 for a log score, 1 for a signal feature
            signal += kind
            if held_out:
                if (sums := self.sums.get(key)) is None or sums[0] < 2:
                    continue
                n = sums[0] - 1
                mean = (sums[1] - x) / n
                var = max((sums[2] - x * x) / n - mean * mean, _var_floor(key))
                totals[kind] -= 0.5 * ((x - mean) ** 2 / var + math.log(2 * math.pi * var))
            elif (term := terms.get(key)) is not None:
                d = x - term[0]
                totals[kind] -= d * d * term[1] + term[2]
            else:
                continue
            shared[kind] += 1
        ll = totals[0] / shared[0] if shared[0] else 0.0
        if shared[1]:
            ll += totals[1] / shared[1] * signal / SIGNAL_LINKS
        return ll, (shared[0], shared[1])

    def misfit(self, vector: Vector) -> float:
        """Mean squared z-score of the signal features this class shares with the vector; 0 without any."""
        total, shared = 0.0, 0
        for key, x in vector.items():
            if len(key) == 3 and (gauss := self.links.get(key)) is not None:
                total += (x - gauss[0]) ** 2 / gauss[1]
                shared += 1
        return total / shared if shared else 0.0


def _probabilities[K](scored: Mapping[K, Fit]) -> dict[K, float]:
    """Softmax of fit() over the classes taking part (see taking_part). The links of a floor see
    the same person, so their evidence is not independent: a mean log-likelihood per feature (see
    fit) keeps the confidence honest."""
    counted = {cls: scored[cls][0] for cls in taking_part(scored)}
    if not counted:
        return {}
    top = max(counted.values())
    weights = {cls: math.exp(ll - top) for cls, ll in counted.items()}
    total = sum(weights.values())
    return {cls: w / total for cls, w in weights.items()}


def taking_part[K](scored: Mapping[K, Fit]) -> list[K]:
    """The classes whose fit counts: a class sharing under half the features of a kind that the
    best covered class shares (calibrated before a node joined, or for an area now on another
    floor) sits out, and so does one sharing none."""
    scored = {cls: s for cls, s in scored.items() if any(s[1])}
    if not scored:
        return []
    best = [max(shared[kind] for _, shared in scored.values()) for kind in (0, 1)]
    return [cls for cls, (_, shared) in scored.items() if all(2 * n >= b for n, b in zip(shared, best, strict=True))]


def _moving(motion: float, quiet: float, moving: bool | None) -> bool:
    """Someone moves on the floor: a link reports motion, or without flags (None) scores quiet or more."""
    return motion >= quiet if moving is None else moving


@dataclass(frozen=True, slots=True)
class Fits:
    """One second of a floor under each of its classes, fitted to the live log scores once and
    shared by the moving decision and the room tracker: per class ClassModel.fit, by area."""

    empty: Fit | None  # None without an empty class
    walking: dict[str, Fit]
    still: dict[str, Fit]


def fit_classes(
    vector: Vector, empty: ClassModel | None, walking: Mapping[str, ClassModel], still: Mapping[str, ClassModel]
) -> Fits:
    """Every class fitted to the vector once (log scores only: see Rooms.step)."""
    return Fits(
        None if empty is None else empty.fit(vector),
        {area: model.fit(vector) for area, model in walking.items()},
        {area: model.fit(vector) for area, model in still.items()},
    )


@dataclass(frozen=True, slots=True)
class Decision:
    room: str | None  # the winning area; None when nobody is moving, or the empty class wins
    confidence: float | None  # the winner's probability; None when the quiet rule decided, or
    # (still) when the signal is unlike every class
    probabilities: dict[ClassId, float]  # per class that took part
    motion: float  # largest live score
    links: int  # live links
    still: bool = False  # from the still classification


def _decision(scored: Mapping[ClassId, Fit], motion: float, links: int) -> Decision | None:
    """The moving decision from the classes' fits: None with no room to tell."""
    probabilities = _probabilities(scored)
    if all(cls is None for cls in probabilities):
        return None
    room = max(probabilities, key=probabilities.__getitem__)
    return Decision(room, probabilities[room], probabilities, motion, links)


def decide(
    scores: Mapping[LinkKey, float | None],
    models: Mapping[ClassId, ClassModel],
    quiet: float = QUIET,
    moving: bool | None = None,
) -> Decision | None:
    """One floor, one second, on the log scores. moving: whether a link reports motion (None:
    judge by quiet). None without live links, or with motion and no room to tell."""
    vector = features(scores)
    if not vector:
        return None
    motion = max(score for score in scores.values() if score is not None)
    if not _moving(motion, quiet, moving):
        return Decision(None, None, {}, motion, len(vector))
    return _decision({cls: model.fit(vector) for cls, model in models.items()}, motion, len(vector))


def decide_still(
    vector: Vector, models: Mapping[ClassId, ClassModel], motion: float = 0.0, fit: float = STILL_FIT
) -> Decision | None:
    """One floor, one second while nobody moves: the room someone is still in, from the log scores
    and the signal of every live link. models: the empty class (None) and a still model per room.
    None without the empty class sharing signal with the vector, or with no room to tell."""
    empty = models.get(None)
    if empty is None:
        return None
    scored = {cls: model.fit(vector) for cls, model in models.items()}
    if not scored[None][1][1]:
        return None  # no signal to hold against the empty floor's
    probabilities = _probabilities(scored)
    if None not in probabilities or len(probabilities) < 2:
        return None
    links = sum(len(key) == 2 for key in vector)
    room = max(probabilities, key=probabilities.__getitem__)
    if room is not None and models[room].misfit(vector) > fit:
        return Decision(None, None, probabilities, motion, links, still=True)
    return Decision(room, probabilities[room], probabilities, motion, links, still=True)


@dataclass(frozen=True, slots=True)
class Separation:
    """How one class of a floor stands apart from the others, on its own samples."""

    cls: ClassKey
    samples: int  # samples tried
    correct: float  # share classified as their own class
    confused_with: ClassKey | None  # the class most of the others went to
    confused: float  # share that went there


def separation(
    classes: Mapping[ClassKey, Sequence[Vector]], min_samples: int = MIN_SAMPLES, limit: int = SEPARATION_SAMPLES
) -> list[Separation]:
    """How well a floor's classes tell apart: up to limit samples of each class, spread over its
    recording, each left out of its class and classified among every class with enough samples,
    as the floor would see it (a moving sample on its log scores, the others on log scores and
    signal). Empty with fewer than two such classes."""
    models = {cls: ClassModel(samples, min_samples) for cls, samples in classes.items() if len(samples) >= min_samples}
    if len(models) < 2:
        return []
    out = []
    for cls in models:
        samples = classes[cls]
        n = min(limit, len(samples))
        won: dict[ClassKey, int] = {}
        for i in range(n):
            vector = samples[i * len(samples) // n]
            if cls[0] == MOVING:
                vector = {key: x for key, x in vector.items() if len(key) == 2}
            probabilities = _probabilities({other: m.fit(vector, other == cls) for other, m in models.items()})
            if probabilities:
                winner = max(probabilities, key=probabilities.__getitem__)
                won[winner] = won.get(winner, 0) + 1
        others = {other: count for other, count in won.items() if other != cls}
        confused = max(others, key=others.__getitem__) if others else None
        out.append(Separation(cls, n, won.get(cls, 0) / n, confused, 0.0 if confused is None else others[confused] / n))
    return out


@dataclass(slots=True)
class Run:
    """A calibration recording on one floor: for a room, or for the floor's empty class."""

    floor: str
    area: str | None  # None: the empty class
    ends: float
    starts: float = -math.inf  # nothing is recorded until after it: time to leave the floor
    recorded: int = 0
    skipped: int = 0  # seconds left out: quiet while recording a room moving, activity while
    # recording the empty floor, or no live links
    still: bool = False  # a room's still class: someone sits still in it


class Rooms:
    """Calibration samples, recording runs, and per floor the latest second: its class fits and
    moving decision."""

    def __init__(
        self,
        *,
        quiet: float = QUIET,
        min_samples: int = MIN_SAMPLES,
        cap: int = SAMPLE_CAP,
    ) -> None:
        self.quiet = quiet
        self.min_samples = min_samples
        self.cap = cap
        self.areas: dict[str, deque[Vector]] = {}  # moving samples, by area
        self.still: dict[str, deque[Vector]] = {}  # still samples, by area
        self.empty: dict[str, deque[Vector]] = {}  # by floor
        self.runs: dict[str, Run] = {}  # by floor, one at a time
        self.fits: dict[str, Fits | None] = {}  # the latest second's, by floor; None without live links
        self.decisions: dict[str, Decision | None] = {}  # the latest moving decision, by floor
        self.vectors: dict[str, Vector] = {}  # the latest vector, by floor: for the still classification
        self._models: dict[tuple[str, str], ClassModel] = {}  # (kind, area or floor)
        self._signal: dict[str, dict[LinkKey, deque[float]]] = {}  # latest readings per link, by floor

    # Calibration

    def start(
        self, floor: str, area: str | None, now: float, duration: float, delay: float = 0.0, still: bool = False
    ) -> Run | None:
        """Record for an area (moving, or still), or the floor's empty class (None), for duration
        seconds after delay. Returns the floor's run it replaced."""
        previous = self.runs.get(floor)
        starts = now + delay if delay else -math.inf
        self.runs[floor] = Run(floor, area, now + delay + duration, starts, still=still and area is not None)
        return previous

    def stop(self, floor: str) -> Run | None:
        return self.runs.pop(floor, None)

    def clear(self, area: str | None = None) -> None:
        """Forget an area's samples, or every class. Runs recording what was cleared stop."""
        if area is None:
            self.areas.clear()
            self.still.clear()
            self.empty.clear()
            self.runs.clear()
            self.fits.clear()
            self._models.clear()
            return
        self.areas.pop(area, None)
        self.still.pop(area, None)
        self._models.pop((MOVING, area), None)
        self._models.pop((STILL, area), None)
        for floor, run in list(self.runs.items()):
            if run.area == area:
                del self.runs[floor]

    def forget_floor(self, floor: str) -> None:
        """A floor that no longer exists: its empty class, run and decisions go."""
        self.empty.pop(floor, None)
        self._models.pop((EMPTY, floor), None)
        self.runs.pop(floor, None)
        self.rest(floor)

    def rest(self, floor: str) -> None:
        """A floor no longer stepped (no node on it now): its latest second and signal readings go."""
        self.fits.pop(floor, None)
        self.decisions.pop(floor, None)
        self.vectors.pop(floor, None)
        self._signal.pop(floor, None)

    def _record(self, run: Run, vector: Vector, motion: float, moving: bool | None, active: bool | None) -> None:
        """moving: as in decide, so a class learns from the seconds it will be asked about. active:
        the nodes confirm motion; the empty floor skips those seconds, so its class never learns
        someone moving (2 of the owner's 5 empty runs had someone moving: 47% of his empty samples)."""
        if (
            not vector
            or (run.area is None and active)
            or (run.area is not None and not run.still and not _moving(motion, self.quiet, moving))
        ):
            run.skipped += 1  # a room's moving class skips still moments; still keeps small motion,
            return  # since sitting and working (typing, shifting in a chair) is never motionless
        kind = EMPTY if run.area is None else STILL if run.still else MOVING
        key = run.floor if run.area is None else run.area
        classes = self._samples(kind)
        if key not in classes:
            classes[key] = deque(maxlen=self.cap)
        classes[key].append(vector)
        self._models.pop((kind, key), None)
        run.recorded += 1

    def _samples(self, kind: str) -> dict[str, deque[Vector]]:
        return self.empty if kind == EMPTY else self.still if kind == STILL else self.areas

    # Decisions

    def models(self, floor: str, areas: Iterable[str]) -> dict[ClassId, ClassModel]:
        """The classes of a floor's moving classification with enough samples: its areas, then its empty class."""
        out: dict[ClassId, ClassModel] = {}
        for area in areas:
            if (model := self.model(MOVING, area)) is not None:
                out[area] = model
        if (model := self.model(EMPTY, floor)) is not None:
            out[None] = model
        return out

    def still_models(self, floor: str, areas: Iterable[str]) -> dict[ClassId, ClassModel]:
        """The classes of a floor's still classification: per area its still class, or before it has
        one its moving class's signal on the empty class's log scores (someone still leaves them as
        quiet as nobody), then the empty class. None without an empty class with signal: it is the
        reference."""
        empty = self.model(EMPTY, floor)
        if empty is None or not empty.signal:
            return {}
        out: dict[ClassId, ClassModel] = {}
        for area in areas:
            if (model := self.model(STILL, area)) is not None:
                out[area] = model
            elif (model := self.model(MOVING, area)) is not None and model.signal:
                out[area] = ClassModel.borrowed(empty, model)
        out[None] = empty
        return out

    def model(self, kind: str, key: str) -> ClassModel | None:
        """A class (EMPTY with a floor, MOVING or STILL with an area), once it has enough samples."""
        samples = self._samples(kind).get(key)
        if samples is None or len(samples) < self.min_samples:
            return None
        model = self._models.get((kind, key))
        if model is None:
            model = self._models[(kind, key)] = ClassModel(samples, self.min_samples)
        return model

    def fit(self, floor: str, areas: Iterable[str], vector: Vector) -> Fits:
        """The floor's classes fitted to one second's log scores: its empty class, and per area
        its moving and still classes."""
        walking: dict[str, ClassModel] = {}
        still: dict[str, ClassModel] = {}
        for area in areas:
            if (model := self.model(MOVING, area)) is not None:
                walking[area] = model
            if (model := self.model(STILL, area)) is not None:
                still[area] = model
        return fit_classes(vector, self.model(EMPTY, floor), walking, still)

    def _average(self, floor: str, signal: Mapping[LinkKey, float | None]) -> dict[LinkKey, float]:
        """Mean signal per link over its latest SIGNAL_WINDOW readings; a link missing a second starts over."""
        history = self._signal.get(floor, {})
        kept: dict[LinkKey, deque[float]] = {}
        for key, rssi in signal.items():
            if rssi is None:
                continue
            readings = history.get(key) or deque(maxlen=SIGNAL_WINDOW)
            readings.append(float(rssi))
            kept[key] = readings
        self._signal[floor] = kept
        return {key: sum(readings) / len(readings) for key, readings in kept.items()}

    def step(
        self,
        floor: str,
        areas: Iterable[str],
        scores: Mapping[LinkKey, float | None],
        now: float,
        moving: bool | None = None,
        signal: Mapping[LinkKey, float | None] | None = None,
        active: bool | None = None,
    ) -> Run | None:
        """One second of one floor: record for its run, then fit its classes once (fits[floor], for
        the room tracker too) and decide (moving: see decide; signal: RSSI per live link this
        second; active: whether the nodes confirm motion this second, None when unknown). The
        fits use the log scores only: signal strength drifted more with nobody there than a body
        changes it. Returns its run if it just ended."""
        scores_vector = features(scores)
        vector = scores_vector | signal_features(self._average(floor, signal or {}))
        motion = max((score for score in scores.values() if score is not None), default=0.0)
        ended = None
        run = self.runs.get(floor)
        recording = run is not None and run.starts < now <= run.ends
        if run is not None:
            if recording:
                self._record(run, vector, motion, moving, active)
            if now >= run.ends:
                ended = self.runs.pop(floor)
        self.vectors[floor] = vector
        if not scores_vector:
            self.fits[floor] = self.decisions[floor] = None
            return ended
        if recording:
            self.fits[floor] = None  # the recording says where everyone is: nothing to fit
            self._known(floor, run, motion, len(scores_vector))
            return ended
        areas = list(areas)
        fits = self.fits[floor] = self.fit(floor, areas, scores_vector)
        if not _moving(motion, self.quiet, moving):
            self.decisions[floor] = Decision(None, None, {}, motion, len(scores_vector))
        else:
            scored: dict[ClassId, Fit] = dict(fits.walking)
            if fits.empty is not None:
                scored[None] = fits.empty
            self.decisions[floor] = _decision(scored, motion, len(scores_vector))
        return ended

    def _known(self, floor: str, run: Run, motion: float, links: int) -> None:
        """While a recording runs, its instructions say where everyone is: moving or still in its
        room, or off the floor for the empty floor. The decision follows that instead of
        classifying with the classes being recorded."""
        if run.area is None:
            self.decisions[floor] = Decision(None, None, {None: 1.0}, motion, links)
        elif run.still:
            self.decisions[floor] = Decision(None, None, {}, motion, links)
        else:
            self.decisions[floor] = Decision(run.area, 1.0, {run.area: 1.0}, motion, links)

    def still_decision(self, floor: str, areas: Iterable[str]) -> Decision | None:
        """The still classification of the floor's latest second, for diagnostics: worked out only
        when asked, since it names no rooms (see decide_still)."""
        vector = self.vectors.get(floor)
        if not vector:
            return None
        motion = (self.decisions.get(floor) or Decision(None, None, {}, 0.0, 0)).motion
        return decide_still(vector, self.still_models(floor, areas), motion)

    def own_links(self, area: str) -> set[LinkKey]:
        """The links someone walking in the area disturbs most, from its walking class, both ways:
        breathing on them is someone in it; elsewhere on the floor it is not."""
        model = self.model(MOVING, area)
        if model is None:
            return set()
        means = {key: gauss[0] for key, gauss in model.links.items() if len(key) == 2}
        top = max(means.values(), default=0.0)
        own = {key for key, mean in means.items() if top > 0 and mean >= OWN_LINKS * top}
        return own | {(b, a) for a, b in own}

    def classes(self, floor: str, areas: Iterable[str]) -> dict[ClassKey, list[Vector]]:
        """The samples of a floor's classes, copied, for separation off the event loop."""
        out: dict[ClassKey, list[Vector]] = {}
        if floor in self.empty:
            out[(EMPTY, None)] = list(self.empty[floor])
        for area in areas:
            for kind in (MOVING, STILL):
                if (samples := self._samples(kind).get(area)) is not None:
                    out[(kind, area)] = list(samples)
        return out

    # Storage

    def to_dict(self) -> dict[str, Any]:
        """Samples per class: its links once, then one row per sample, None where a link was missing;
        with signal, a row of it per sample too."""
        return {
            "areas": {area: _pack(samples) for area, samples in self.areas.items()},
            "empty": {floor: _pack(samples) for floor, samples in self.empty.items()},
            "still": {area: _pack(samples) for area, samples in self.still.items()},
        }

    def load(self, data: Mapping[str, Any]) -> None:
        """Samples from to_dict, also from before signal and still classes. Raises ValueError for
        data it cannot read, and keeps what it had."""
        try:
            areas = {str(area): self._unpack(packed) for area, packed in data.get("areas", {}).items()}
            empty = {str(floor): self._unpack(packed) for floor, packed in data.get("empty", {}).items()}
            still = {str(area): self._unpack(packed) for area, packed in data.get("still", {}).items()}
        except (AttributeError, KeyError, TypeError, ValueError) as err:
            raise ValueError(f"unreadable calibration: {err!r}") from err
        self.areas = {area: samples for area, samples in areas.items() if samples}
        self.empty = {floor: samples for floor, samples in empty.items() if samples}
        self.still = {area: samples for area, samples in still.items() if samples}
        self._models.clear()

    def _unpack(self, packed: Mapping[str, Any]) -> deque[Vector]:
        links = [tuple(link.split(">")) for link in packed["links"]]
        if any(len(key) != 2 for key in links):
            raise ValueError("link ids are transmitter>receiver")
        rows = packed["samples"]
        signal = packed.get("signal")  # absent from samples recorded before signal features
        if signal is not None and len(signal) != len(rows):
            raise ValueError(f"{len(signal)} signal rows for {len(rows)} samples")
        samples: deque[Vector] = deque(maxlen=self.cap)
        for i, row in enumerate(rows):
            if len(row) != len(links) or (signal is not None and len(signal[i]) != len(links)):
                raise ValueError(f"{len(row)} values for {len(links)} links")
            vector: Vector = {key: float(x) for key, x in zip(links, row, strict=True) if x is not None}
            if signal is not None:
                vector |= {(*key, SIGNAL): float(x) for key, x in zip(links, signal[i], strict=True) if x is not None}
            if vector:
                samples.append(vector)
        return samples


def _pack(samples: Iterable[Vector]) -> dict[str, Any]:
    samples = list(samples)
    links = sorted({key[:2] for vector in samples for key in vector})
    packed: dict[str, Any] = {
        "links": [f"{tx}>{rx}" for tx, rx in links],
        "samples": [[None if (x := vector.get(key)) is None else round(x, 3) for key in links] for vector in samples],
    }
    if any(len(key) == 3 for vector in samples for key in vector):
        packed["signal"] = [
            [None if (x := vector.get((*key, SIGNAL))) is None else round(x, 2) for key in links] for vector in samples
        ]
    return packed
