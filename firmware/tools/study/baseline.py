"""Today's room presence, as presence.py drives it each second: Rooms.step with the floor's live
scores, motion flags, signal and activity (the nodes' confirmations, or presence.py's own check
for older firmware), then "presence first": the room someone walks in now, else the room whose
presence won last while it holds; walking only while the moving decision is sure.

The hand rules it drives left the engine with integration 0.1.17 (the room tracker replaced them):
run it on the engine of commit c2411a5, e.g. `git worktree add /tmp/wisp-c2411a5 c2411a5` and
WISP_ENGINE_ROOT=/tmp/wisp-c2411a5."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from common import ACTIVE_WITHIN, FLOOR
from custom_components.wisp.engine.rooms import Rooms


@dataclass(frozen=True, slots=True)
class Out:
    room: str | None  # where the person is, None: nobody on the floor
    rooms: frozenset[str]  # every area shown present (the presence binary sensors)
    walking: bool
    p: dict | None = None  # optional per-class probabilities, for diagnostics


def active_links(frame: dict, nodes: set[str], seen: deque, now: float) -> set:
    """presence.py _active: the links active this second, from the nodes' confirmations when every
    live link's node confirms (firmware 0.1.6+), else a link moving both ways within 2 s with a
    third node seeing motion on a link to either end (4 nodes: every other node may support)."""
    moving = frame["moving"]
    seen.append((now, frozenset(moving)))
    while seen and now - seen[0][0] > ACTIVE_WITHIN:
        seen.popleft()
    if len(nodes) >= 3:
        confirmed = frame["confirmed"]
        if confirmed or (frame["scores"] and frame["confirms"]):
            return set(confirmed)
    links = set().union(*(m for _, m in seen))
    active: set = set()
    for a, b in moving:
        if a not in nodes or (b, a) not in links:
            continue
        others = nodes - {a, b}
        support = {c for key in links for c in key if c in others and {a, b} & set(key)}
        if not others or len(support) >= 1:
            active |= {(a, b), (b, a)}
    return active


class Baseline:
    name = "baseline"

    def __init__(self, calibration: dict, plan: dict, **_: object) -> None:
        self.engine = Rooms()
        self.engine.load(calibration)
        self.areas = list(calibration["areas"].keys())
        self.nodes = set(plan["nodes"])
        self.seen: deque = deque()

    def step(self, frame: dict, now: float) -> Out:
        e = self.engine
        active = active_links(frame, self.nodes, self.seen, now)
        kwargs = {"breathing_links": frame["breathing"]} if frame["breathing"] else {}
        e.step(FLOOR, self.areas, frame["scores"], now, bool(frame["moving"]), frame["signal"], bool(active),
               active, **kwargs)
        d = e.decisions.get(FLOOR)
        sure = d.room if d is not None and d.room is not None and (d.confidence or 0) >= e.confidence else None
        held = [(w[0], a) for a in self.areas if (w := e.wins.get(a)) and e.presence(a, now) is not None]
        room = sure or (max(held)[1] if held else None)
        rooms = frozenset(a for a in self.areas if e.presence(a, now) is not None)
        return Out(room, rooms, sure is not None)
