"""The room tracker (tracker.py) as a method for evaluate.py: forward filtering (what the
integration can run, second by second) and Viterbi (offline, the whole stretch at once: an upper
bound). Parameters come as keyword arguments (evaluate.py --param name=value): any field of
tracker.Params, and hall_only, exits."""
from __future__ import annotations

from collections import deque
from dataclasses import fields

from baseline import Out, active_links
from common import FLOOR
from custom_components.wisp.engine.rooms import Rooms
import tracker as T

TRACKER_ARGS = {"hall_only", "exits"}


def build(calibration: dict, plan: dict, **params) -> T.RoomTracker:
    engine = Rooms()
    engine.load(calibration)
    names = {f.name for f in fields(T.Params)}
    unknown = set(params) - names - TRACKER_ARGS
    if unknown:
        raise TypeError(f"unknown parameters {unknown}")
    p = T.Params(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in params.items() if k in names})
    classes = T.from_rooms(engine, FLOOR, list(calibration["areas"]))
    return T.RoomTracker(classes, plan["rooms"], p, hall_only=bool(params.get("hall_only", True)),
                         exits=tuple(params.get("exits", ())))


class Method:
    name = "tracker"

    def __init__(self, calibration: dict, plan: dict, **params) -> None:
        self.tracker = build(calibration, plan, **params)
        self.nodes = set(plan["nodes"])
        self.seen: deque = deque()

    def step(self, frame: dict, now: float) -> Out:
        active = bool(active_links(frame, self.nodes, self.seen, now))
        e = self.tracker.step(frame["scores"], now, active)
        return Out(e.room, frozenset({e.room} if e.room else ()), e.walking, e.probabilities)


class Viterbi(Method):
    name = "viterbi"

    def __init__(self, calibration: dict, plan: dict, **params) -> None:
        super().__init__(calibration, plan, **params)
        self.buffer: list = []

    def step(self, frame: dict, now: float) -> Out:
        active = bool(active_links(frame, self.nodes, self.seen, now))
        self.buffer.append((self.tracker.fits(frame["scores"]), now, active))
        return Out(None, frozenset(), False)

    def finish(self) -> dict[int, Out]:
        path = T.viterbi(self.tracker, self.buffer)
        return {int(now): Out(room, frozenset({room} if room else ()), kind == T.WALK)
                for (kind, room), (_, now, _) in zip(path, self.buffer, strict=True)}
