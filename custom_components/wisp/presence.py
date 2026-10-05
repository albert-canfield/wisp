"""Room presence in Home Assistant: floors from the area and floor registries, calibration runs kept
in storage, and a decision per floor every second (the maths is in engine/rooms.py). The same tick
also places one moving person per floor on the hive's layout (engine/floor.py), for the map, or on
the floor's plan once it has one (plans.py): then every position on that floor is in plan metres.

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
from homeassistant.helpers import area_registry as ar, floor_registry as fr, issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    CONF_PRESENCE_HOLD,
    DOMAIN,
    MANUFACTURER,
    MIN_FLOOR_NODES,
    NO_FLOOR,
    ROOM_LINK_AGE,
    ROOMS_INTERVAL,
    STORE_VERSION,
)
from .engine import Decision, HiveState, LinkKey, Rooms, Run, access_points
from .engine.floor import FloorFix, FloorModel
from .engine.rooms import HOLD
from .plans import FloorPlans

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
        self.models: dict[str, FloorModel] = {}  # position per floor, on the hive's layout or the plan
        self.fixes: dict[str, FloorFix] = {}
        self.plans = FloorPlans(self.hass, self.entry.entry_id)
        self._listeners: list[Callable[[], None]] = []
        self._entity_listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._issues: set[str] = set()  # repair issues raised, by id

    async def async_start(self) -> None:
        if data := await self.store.async_load():
            try:
                self.engine.load(data)
            except ValueError as err:
                _LOGGER.warning("Discarding the stored room calibration: %s", err)
        await self.plans.async_load()
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
        self._async_raise_issues({})
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
        self._async_raise_issues({key: f for key, f in self.floors.items() if len(f.nodes) < MIN_FLOOR_NODES})
        if changed or entities:
            self._async_entities_changed()

    @callback
    def _async_raise_issues(self, few: dict[str, Floor]) -> None:
        """A repair issue per floor with too few nodes for rooms and positions; gone once it has them."""
        wanted = {f"{self.entry.entry_id}_few_nodes_{floor_scope(key)}": floor for key, floor in few.items()}
        for issue_id in self._issues - set(wanted):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for issue_id, floor in wanted.items():
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="few_nodes" if floor.key else "few_nodes_home",
                translation_placeholders={"floor": floor.name, "count": str(len(floor.nodes)), "needed": str(MIN_FLOOR_NODES)},
                learn_more_url="https://github.com/albert-canfield/wisp/blob/main/docs/SETUP.md#1-boards",
            )
        self._issues = set(wanted)

    @callback
    def _async_area_updated(self, event: Event[ar.EventAreaRegistryUpdatedData]) -> None:
        area = event.data["area_id"]
        if event.data["action"] == "remove" and area in self.engine.areas:
            self.engine.clear(area)  # the room is gone
            self._async_save()
        self.async_sync_floors(entities=True)

    @callback
    def _async_floor_updated(self, event: Event[fr.EventFloorRegistryUpdatedData]) -> None:
        floor, removed = event.data["floor_id"], event.data["action"] == "remove"
        if removed and floor in self.engine.empty:
            self.engine.forget_floor(floor)
            self._async_save()
        if removed and self.plans.remove(floor):  # its plan goes with it
            self.entry.async_create_task(self.hass, self.plans.async_save(), "wisp save floor plans")
        self.async_sync_floors(entities=True)

    # Every second

    def scores(self, floor: str, now: float) -> dict[LinkKey, float]:
        """Live motion scores of the links the floor's nodes receive."""
        return {key: link.score for key, link in self._live_links(floor, now)}

    def moving(self, floor: str, now: float) -> set[LinkKey]:
        """The floor's live links that report motion."""
        return {key for key, link in self._live_links(floor, now) if link.motion}

    def _live_links(self, floor: str, now: float):
        nodes = self.floors[floor].nodes
        return (
            (key, link)
            for key, link in self.hub.table.links.items()
            if link.receiver in nodes and link.score is not None and now - link.updated <= ROOM_LINK_AGE
        )

    @callback
    def _async_tick(self, _now: Any = None) -> None:
        now = self.hub.clock()
        ended: list[Run | None] = [self.engine.stop(floor) for floor in list(self.engine.runs) if floor not in self.floors]
        live = {}
        hive = self.hub.hive.current(now)
        for floor in self.floors:
            scores, moving = self.scores(floor, now), self.moving(floor, now)
            live[floor] = len(scores)
            ended.append(self.engine.step(floor, self.floor_areas(floor), scores, now, bool(moving)))
            model = self._layout(floor, hive)
            if (fix := model.update(scores, now, moving)) is not None:
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

    # Floor plans

    def _layout(self, floor: str, hive: HiveState | None) -> FloorModel:
        """The floor's positions: the hive's layout, or on its plan the placed ones and the rest fitted."""
        model = self.models.setdefault(floor, FloorModel())
        plan = self.plans.floors.get(floor)
        if plan is None:
            model.set_layout(hive)
        else:
            nodes = self.floors[floor].nodes
            placed = {mac: p for mac, p in plan.nodes.items() if mac in nodes} | plan.access_points
            model.set_layout(hive, placed, plan=(plan.width, plan.height), nodes=nodes)
        return model

    @callback
    def async_plans_changed(self, floor: str) -> None:
        """A plan or its placements changed: positions follow at once, a person's track starts over."""
        self.fixes.pop(floor, None)
        self.models.pop(floor, None)
        if floor in self.floors:
            self._layout(floor, self.hub.hive.current(self.hub.clock()))
        self._async_entities_changed()  # a floor with a plan has position sensors
        self._async_notify()

    def placed(self, floor: str) -> set[str]:
        """What the user placed on the floor's plan: its nodes and any access point."""
        plan = self.plans.floors.get(floor)
        if plan is None or floor not in self.floors:
            return set()
        return (set(plan.nodes) & self.floors[floor].nodes) | set(plan.access_points)

    def positions(self, floor: str) -> dict[str, dict[str, Any]]:
        """Every position on the floor's plan, in its metres, and whether the user placed it."""
        model = self.models.get(floor)
        if model is None:
            return {}
        placed = self.placed(floor)
        return {
            key: {"x": round(x, 2), "y": round(y, 2), "placed": key in placed}
            for key, (x, y) in sorted(model.positions.items())
        }

    def fit(self, floor: str, digits: int = 2) -> dict[str, Any] | None:
        """How the hive's layout was fitted onto the placed nodes; None before two are placed."""
        model = self.models.get(floor)
        fit = model.fit if model else None
        if fit is None or fit.pairs < 2:
            return None
        return {
            "nodes": fit.pairs,
            "scale": round(fit.scale, digits),
            "rotation": round(fit.rotation, 1),
            "mirror": fit.mirror,
            "mirror_guessed": fit.guessed,
            "error": round(fit.error, digits),
        }

    def plan_view(self, floor: str) -> dict[str, Any]:
        """The floor's plan for the panel: its size, the positions, the access points its nodes hear
        or that were placed, and the fit."""
        plan, nodes = self.plans.floors[floor], self.floors[floor].nodes
        positions = self.positions(floor)
        heard = access_points(self.hub.table, self.hub.hive.current(self.hub.clock()), nodes)
        return {
            "plan": plan.summary(),
            "positions": positions,
            "access_points": sorted(set(heard) | set(plan.access_points) | {k for k in positions if k not in nodes}),
            "fit": self.fit(floor),
        }

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

    def map_floors(self) -> list[dict[str, Any]]:
        """Per floor its nodes, and with a plan the plan and every position on it in plan metres.
        Empty for one floor without a plan: the map shows everything, as before."""
        if len(self.floors) < 2 and not any(key in self.plans.floors for key in self.floors):
            return []
        out = []
        for key, floor in self.floors.items():
            entry: dict[str, Any] = {"floor": key or None, "name": floor.name, "nodes": sorted(floor.nodes)}
            if (plan := self.plans.floors.get(key)) is not None:
                entry["plan"] = plan.summary()
                entry["positions"] = self.positions(key)
            out.append(entry)
        return out

    def access_point_positions(self) -> dict[str, tuple[float, float]]:
        """Where the position engine puts each access point on the hive's layout (floors without a
        plan), so the map draws the geometry someone moving is placed on."""
        out: dict[str, tuple[float, float]] = {}
        for key in self.floors:
            model = self.models.get(key)
            if model is None or key in self.plans.floors:
                continue
            for mac, point in model.positions.items():
                if mac not in self.hub.nodes and mac not in out:
                    out[mac] = point
        return out

    def people(self) -> list[dict[str, Any]]:
        """Where someone moves, per floor, in the layout's metres or the floor plan's; empty when nobody moves."""
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
                **(self.plan_view(key) if key in self.plans.floors else {}),
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
                **({"plan": self.plans.floors[key].to_dict(), "fit": self.fit(key, 3)} if key in self.plans.floors else {}),
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
