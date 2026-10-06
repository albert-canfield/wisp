"""The integration's room tracker (custom_components/wisp/engine/tracker.py) as a method for
evaluate.py, driven as presence.py drives it: the fits from the calibration, the floor's plan
(drawn rooms, exits), activity from the links. Parameters: any field of tracker.Params, and exits."""
from __future__ import annotations

from collections import deque
from dataclasses import fields

from baseline import Out, active_links
from common import FLOOR
from custom_components.wisp.engine.rooms import Rooms
from custom_components.wisp.engine import tracker as T


class Method:
    name = "engine"

    def __init__(self, calibration: dict, plan: dict, **params) -> None:
        engine = Rooms()
        engine.load(calibration)
        names = {f.name for f in fields(T.Params)}
        p = T.Params(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in params.items() if k in names})
        classes = T.from_rooms(engine, FLOOR, list(calibration["areas"]))
        self.tracker = T.RoomTracker(classes, list(plan["rooms"]), tuple(params.get("exits", ())), p)
        self.nodes = set(plan["nodes"])
        self.seen: deque = deque()

    def step(self, frame: dict, now: float) -> Out:
        active = bool(active_links(frame, self.nodes, self.seen, now))
        e = self.tracker.step(frame["scores"], now, active, frame["breathing"])
        return Out(e.room, frozenset(a for a in e.probabilities if a and e.present(a)), e.walking, e.probabilities)
