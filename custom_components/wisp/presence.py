"""Room presence in Home Assistant: floors from the area and floor registries, calibration runs kept
in storage, and a decision per floor every second (the maths is in engine/rooms.py). The same tick
also places one moving person per floor on the hive's layout (engine/floor.py), for the map.

Rooms are areas. A node's floor is the floor of its area; nodes and areas without a floor share one
floor named after the hub. A link belongs to the floor of the node that receives it.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import math
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, callback
from homeassistant.helpers import area_registry as ar, floor_registry as fr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    CONF_PRESENCE_HOLD,
    DOMAIN,
    MANUFACTURER,
    NO_FLOOR,
    ROOM_LINK_AGE,
    ROOMS_INTERVAL,
    STORE_VERSION,
)
from .engine import Decision, LinkKey, Rooms, Run
from .engine.floor import FloorFix, FloorModel
from .engine.rooms import HOLD

if TYPE_CHECKING:
    from .hub import WispHub

_LOGGER = logging.getLogger(__name__)
NONE = "none"  # the room while nobody moves, and the empty class among the probabilities
EMPTY = "empty"  # the empty class in calibration


def store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.calibration"


def floor_scope(floor: str) -> str:
    """Floor part of a unique id. The hub's own floor has no id."""
    return f"floor_{floor}" if floor else "house"


@dataclass(slots=True)
class Floor:
    key: str  # Home Assistant floor id, or NO_FLOOR
    name: str
    level: int | None
    nodes: set[str] = field(default_factory=set)


class RoomPresence:
    def __init__(self, hub: WispHub) -> None:
        self.hub = hub
        self.hass = hub.hass
        self.entry = hub.entry
        self.engine = Rooms(hold=self.hold_option())
        self.store: Store[dict[str, Any]] = Store(self.hass, STORE_VERSION, store_key(self.entry.entry_id))
        self.floors: dict[str, Floor] = {}
        self.live: dict[str, int] = {}  # live links per floor, at the latest second
        self.models: dict[str, FloorModel] = {}  # position per floor, on the hive's layout
        self.fixes: dict[str, FloorFix] = {}
        self._listeners: list[Callable[[], None]] = []
        self._entity_listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []

    async def async_start(self) -> None:
        if data := await self.store.async_load():
            try:
                self.engine.load(data)
            except ValueError as err:
                _LOGGER.warning("Discarding the stored room calibration: %s", err)
        self._unsubs = [
            async_track_time_interval(
                self.hass, self._async_tick, ROOMS_INTERVAL, name="wisp rooms", cancel_on_shutdown=True
            ),
            self.hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, self._async_area_updated),
            self.hass.bus.async_listen(fr.EVENT_FLOOR_REGISTRY_UPDATED, self._async_floor_updated),
        ]

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self.engine.runs:  # keep what the unfinished runs recorded
            recorded = any(run.recorded for run in self.engine.runs.values())
            self.engine.runs.clear()
            if recorded:
                self._async_save()

    def hold_option(self) -> float:
        return float(self.entry.options.get(CONF_PRESENCE_HOLD, HOLD))

    @callback
    def async_apply_options(self) -> None:
        self.engine.hold = self.hold_option()

    # Floors and areas

    def area_floor(self, area_id: str | None) -> str | None:
        """The floor an area is on: its Home Assistant floor or NO_FLOOR. None for an unknown area."""
        area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        return None if area is None else area.floor_id or NO_FLOOR

    def area_name(self, area_id: str) -> str:
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else area_id

    def floor_areas(self, floor: str) -> list[str]:
        """Calibrated areas on a floor."""
        return sorted(area for area in self.engine.areas if self.area_floor(area) == floor)

    def calibrated_areas(self) -> list[str]:
        """Calibrated areas Home Assistant still has."""
        return sorted(area for area in self.engine.areas if self.area_floor(area) is not None)

    def floors_in_use(self) -> list[str]:
        """Floors calibrated or calibrating: they have room entities. Others make none."""
        engine = self.engine
        return [f for f in self.floors if f in engine.runs or f in engine.empty or self.floor_areas(f)]

    @callback
    def async_sync_floors(self, entities: bool = False) -> None:
        """Group the nodes by floor, after a node, area or floor changed. The room entities follow
        when the floors changed, or always with entities set (an area may have moved floor)."""
        registry = fr.async_get(self.hass)
        floors: dict[str, Floor] = {}
        for node in self.hub.nodes.values():
            key = self.area_floor(node.area) or NO_FLOOR
            if key not in floors:
                entry = registry.async_get_floor(key) if key else None
                floors[key] = Floor(key, entry.name if entry else key or self.entry.title, entry and entry.level)
            floors[key].nodes.add(node.mac)
        ordered = sorted(floors.values(), key=lambda f: (not f.key, f.level is None, f.level or 0, f.name))
        changed = [(f.key, f.name) for f in ordered] != [(f.key, f.name) for f in self.floors.values()]
        self.floors = {f.key: f for f in ordered}
        for key in [key for key in self.engine.decisions if key not in self.floors]:
            del self.engine.decisions[key]
        if changed or entities:
            self._async_entities_changed()

    @callback
    def _async_area_updated(self, event: Event[ar.EventAreaRegistryUpdatedData]) -> None:
        area = event.data["area_id"]
        if event.data["action"] == "remove" and area in self.engine.areas:
            self.engine.clear(area)  # the room is gone
            self._async_save()
        self.async_sync_floors(entities=True)

    @callback
    def _async_floor_updated(self, event: Event[fr.EventFloorRegistryUpdatedData]) -> None:
        if event.data["action"] == "remove" and event.data["floor_id"] in self.engine.empty:
            self.engine.forget_floor(event.data["floor_id"])
            self._async_save()
        self.async_sync_floors(entities=True)

    # Every second

    def scores(self, floor: str, now: float) -> dict[LinkKey, float]:
        """Live motion scores of the links the floor's nodes receive."""
        nodes = self.floors[floor].nodes
        return {
            key: link.score
            for key, link in self.hub.table.links.items()
            if link.receiver in nodes and link.score is not None and now - link.updated <= ROOM_LINK_AGE
        }

    @callback
    def _async_tick(self, _now: Any = None) -> None:
        now = self.hub.clock()
        ended: list[Run | None] = [self.engine.stop(floor) for floor in list(self.engine.runs) if floor not in self.floors]
        live = {}
        hive = self.hub.hive.current(now)
        for floor in self.floors:
            scores = self.scores(floor, now)
            live[floor] = len(scores)
            ended.append(self.engine.step(floor, self.floor_areas(floor), scores, now))
            model = self.models.setdefault(floor, FloorModel())
            model.set_layout(hive)
            if (fix := model.update(scores, now)) is not None:
                self.fixes[floor] = fix
            else:
                self.fixes.pop(floor, None)
        for floor in [f for f in self.models if f not in self.floors]:
            del self.models[floor]
            self.fixes.pop(floor, None)
        self.live = live
        if any(ended):
            if any(run and run.recorded for run in ended):
                self._async_save()
            self._async_entities_changed()  # an area's first calibration brings its presence entity
        self._async_notify()

    # Calibration

    @callback
    def async_calibrate(self, floor: str, area: str | None, duration: float, delay: float = 0.0) -> None:
        """Record for an area, or the floor's empty class (None), after delay. Replaces the floor's run."""
        previous = self.engine.start(floor, area, self.hub.clock(), duration, delay)
        if previous and previous.recorded:
            self._async_save()
        self._async_entities_changed()  # the first run on a floor brings its sensors
        self._async_notify()

    @callback
    def async_stop_run(self, floor: str) -> None:
        """End a floor's run now, keeping what it recorded."""
        run = self.engine.stop(floor)
        if run is None:
            return
        if run.recorded:
            self._async_save()
        self._async_entities_changed()  # a first calibration that ends early still makes its sensor
        self._async_notify()

    @callback
    def async_clear(self, area: str | None = None) -> None:
        """Forget an area's calibration, or all of it."""
        self.engine.clear(area)
        self._async_save()
        self._async_entities_changed()
        self._async_notify()

    @callback
    def _async_save(self) -> None:
        self.entry.async_create_task(
            self.hass, self.store.async_save(self.engine.to_dict()), "wisp save room calibration"
        )

    # Entities

    def device_info(self) -> DeviceInfo:
        """The hub's own device: floors and rooms belong to no single node."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.title,
            manufacturer=MANUFACTURER,
            model="Hub",
            entry_type=DeviceEntryType.SERVICE,
        )

    @callback
    def async_listen(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener every second, after the decisions."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_listen_entities(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call listener now and whenever the floors or the calibrated areas change."""
        self._entity_listeners.append(listener)
        listener()
        return lambda: self._entity_listeners.remove(listener)

    @callback
    def _async_notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def _async_entities_changed(self) -> None:
        for listener in list(self._entity_listeners):
            listener()

    def available(self, floor: str | None) -> bool:
        """The hub runs and the floor has live links."""
        return self.hub.transport is not None and floor is not None and self.live.get(floor, 0) > 0

    def room(self, floor: str) -> str | None:
        """The room sensor's state: the area someone moves in, NONE, or None when it cannot tell."""
        decision = self.engine.decisions.get(floor)
        if decision is None:
            return None
        return NONE if decision.room is None else self.area_name(decision.room)

    def probabilities(self, decision: Decision | None, by_name: bool = True, digits: int = 2) -> dict[str, float]:
        """Per class, most likely first: by area name (or id), NONE for nobody moving."""
        if decision is None:
            return {}
        return {
            NONE if cls is None else self.area_name(cls) if by_name else cls: round(p, digits)
            for cls, p in sorted(decision.probabilities.items(), key=lambda item: -item[1])
        }

    def samples(self, floor: str) -> dict[str, int]:
        """Calibration samples per class on a floor, by area name."""
        out = {self.area_name(area): len(self.engine.areas[area]) for area in self.floor_areas(floor)}
        if floor in self.engine.empty:
            out[EMPTY] = len(self.engine.empty[floor])
        return out

    def seconds_left(self, run: Run) -> int:
        return max(0, math.ceil(run.ends - self.hub.clock()))

    def starts_in(self, run: Run) -> int:
        """Seconds until a delayed run records, 0 once it does."""
        return math.ceil(max(0.0, run.starts - self.hub.clock()))

    # Map and diagnostics

    def snapshot(self) -> list[dict[str, Any]] | None:
        """Per floor: the room, its confidence and presence per calibrated area. None before any calibration."""
        engine = self.engine
        if not (engine.areas or engine.empty or engine.runs):
            return None
        now = self.hub.clock()
        out = []
        for key, floor in self.floors.items():
            decision = engine.decisions.get(key)
            out.append({
                "floor": key or None,
                "name": floor.name,
                "room": self.room(key),
                "area": decision.room if decision else None,
                "confidence": None if decision is None or decision.confidence is None else round(decision.confidence, 2),
                "presence": [
                    {"area": area, "name": self.area_name(area), "on": engine.presence(area, now) is not None}
                    for area in self.floor_areas(key)
                ],
            })
        return out

    def people(self) -> list[dict[str, Any]]:
        """Where someone moves, per floor, in the layout's metres; empty when nobody moves."""
        return [
            {
                "floor": key or None,
                "name": self.floors[key].name,
                "x": round(fix.x, 2),
                "y": round(fix.y, 2),
                "quality": round(fix.quality, 2),
            }
            for key, fix in self.fixes.items()
            if key in self.floors
        ]

    def panel(self) -> dict[str, Any]:
        """Per floor: its room, its run and every area with a node or samples, then the other areas
        on it to calibrate. Calibrated areas on no floor with nodes come last, to clear."""
        engine = self.engine
        now = self.hub.clock()
        nodes_in: dict[str, int] = {}
        for node in self.hub.nodes.values():
            if node.area:
                nodes_in[node.area] = nodes_in.get(node.area, 0) + 1
        all_areas = ar.async_get(self.hass).async_list_areas()
        floors = []
        for key, floor in self.floors.items():
            decision = engine.decisions.get(key)
            run = engine.runs.get(key)
            shown = {area for area in nodes_in if self.area_floor(area) == key} | set(self.floor_areas(key))
            if run and run.area:  # its first samples are on the way
                shown.add(run.area)
            areas = []
            for area in shown:
                win = engine.presence(area, now)
                areas.append({
                    "area": area,
                    "name": self.area_name(area),
                    "nodes": nodes_in.get(area, 0),
                    "samples": len(engine.areas.get(area, ())),
                    "presence": win is not None,
                    "confidence": None if win is None else round(win, 2),
                })
            floors.append({
                "floor": key or None,
                "name": floor.name,
                "nodes": sorted(floor.nodes),
                "live_links": self.live.get(key, 0),
                "room": self.room(key),
                "area": decision.room if decision else None,
                "confidence": None if decision is None or decision.confidence is None else round(decision.confidence, 2),
                "empty_samples": len(engine.empty.get(key, ())),
                "run": None if run is None else {
                    "area": run.area,
                    "name": None if run.area is None else self.area_name(run.area),
                    "starts_in": self.starts_in(run),
                    "seconds_left": self.seconds_left(run),
                    "recorded": run.recorded,
                    "skipped": run.skipped,
                },
                "areas": sorted(areas, key=lambda a: a["name"].casefold()),
                "other_areas": [
                    {"area": area.id, "name": area.name}
                    for area in sorted(all_areas, key=lambda a: a.name.casefold())
                    if (area.floor_id or NO_FLOOR) == key and area.id not in shown
                ],
            })
        elsewhere = [
            {"area": area, "name": self.area_name(area), "samples": len(samples)}
            for area, samples in engine.areas.items()
            if self.area_floor(area) not in self.floors
        ]
        return {
            "floors": floors,
            "elsewhere": sorted(elsewhere, key=lambda a: a["name"].casefold()),
            "min_samples": engine.min_samples,
        }

    def diagnostics(self) -> dict[str, Any]:
        engine = self.engine
        now = self.hub.clock()

        def links(samples) -> int:
            return len({key for vector in samples for key in vector})

        def decision(d: Decision | None) -> dict[str, Any] | None:
            if d is None:
                return None
            return {
                "room": d.room,
                "confidence": d.confidence,
                "probabilities": self.probabilities(d, by_name=False, digits=3),
                "motion": d.motion,
                "links": d.links,
            }

        floors = []
        for key, floor in self.floors.items():
            run = engine.runs.get(key)
            floors.append({
                "floor": key or None,
                "name": floor.name,
                "nodes": sorted(floor.nodes),
                "live_links": self.live.get(key, 0),
                "areas": self.floor_areas(key),
                "empty_samples": len(engine.empty.get(key, ())),
                "calibrating": {
                    "area": run.area,
                    "seconds_left": self.seconds_left(run),
                    "recorded": run.recorded,
                    "skipped": run.skipped,
                } if run else None,
                "decision": decision(engine.decisions.get(key)),
            })
        positions = {
            key or "": {
                "placed": {m: [round(p[0], 2), round(p[1], 2)] for m, p in model.positions.items()},
                "fix": None if (fix := self.fixes.get(key)) is None else {
                    "x": round(fix.x, 2), "y": round(fix.y, 2), "raw": [round(fix.raw_x, 2), round(fix.raw_y, 2)],
                    "quality": round(fix.quality, 2),
                },
            }
            for key, model in self.models.items()
        }
        return {
            "positions": positions,
            "settings": {
                "quiet": engine.quiet,
                "confidence": engine.confidence,
                "hold_s": engine.hold,
                "min_samples": engine.min_samples,
                "sample_cap": engine.cap,
                "link_age_s": ROOM_LINK_AGE,
            },
            "floors": floors,
            "areas": {
                area: {
                    "name": self.area_name(area),
                    "floor": self.area_floor(area),
                    "samples": len(samples),
                    "links": links(samples),
                    "presence": engine.presence(area, now),
                }
                for area, samples in sorted(engine.areas.items())
            },
            "empty": [
                {"floor": floor or None, "samples": len(samples), "links": links(samples)}
                for floor, samples in sorted(engine.empty.items())
            ],
        }
