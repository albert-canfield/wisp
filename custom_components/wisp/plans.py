"""Floor plans: an image per floor, its size in metres, and where the user placed nodes and access
points on it, in metres from the image's top left corner with y down. Kept in storage per hub.
The floor key is the Home Assistant floor id, or NO_FLOOR for the hub's own floor."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, PLANS_STORE_VERSION

_LOGGER = logging.getLogger(__name__)
Point = tuple[float, float]


def plans_store_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.plans"


@dataclass(slots=True)
class FloorPlan:
    url: str
    width: float  # metres
    height: float
    nodes: dict[str, Point] = field(default_factory=dict)  # by MAC
    access_points: dict[str, Point] = field(default_factory=dict)  # by BSSID

    def summary(self) -> dict[str, Any]:
        return {"url": self.url, "width": self.width, "height": self.height}

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "nodes": {mac: list(p) for mac, p in sorted(self.nodes.items())},
            "access_points": {bssid: list(p) for bssid, p in sorted(self.access_points.items())},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FloorPlan:
        def points(raw: Mapping[str, Any]) -> dict[str, Point]:
            return {str(k): (float(x), float(y)) for k, (x, y) in raw.items()}

        return cls(
            str(data["url"]), float(data["width"]), float(data["height"]),
            points(data.get("nodes", {})), points(data.get("access_points", {})),
        )


class FloorPlans:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.store: Store[dict[str, Any]] = Store(hass, PLANS_STORE_VERSION, plans_store_key(entry_id))
        self.floors: dict[str, FloorPlan] = {}

    async def async_load(self) -> None:
        data = await self.store.async_load()
        if not data:
            return
        try:
            self.floors = {str(key): FloorPlan.from_dict(plan) for key, plan in data["floors"].items()}
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            _LOGGER.warning("Discarding the stored floor plans: %s", err)
            self.floors = {}

    async def async_save(self) -> None:
        await self.store.async_save({"floors": {key: plan.to_dict() for key, plan in self.floors.items()}})

    def set_plan(self, floor: str, url: str, width: float, height: float) -> FloorPlan:
        """A new plan, or a new image or size for one: placements stay, in metres."""
        plan = self.floors.get(floor)
        if plan is None:
            plan = self.floors[floor] = FloorPlan(url, width, height)
        else:
            plan.url, plan.width, plan.height = url, width, height
        return plan

    def place(self, floor: str, nodes: Mapping[str, Point | None], access_points: Mapping[str, Point | None]) -> None:
        """Set or (with None) remove positions on the floor's plan."""
        plan = self.floors[floor]
        for target, changes in ((plan.nodes, nodes), (plan.access_points, access_points)):
            for key, point in changes.items():
                if point is None:
                    target.pop(key, None)
                else:
                    target[key] = (round(float(point[0]), 3), round(float(point[1]), 3))

    def remove(self, floor: str) -> bool:
        return self.floors.pop(floor, None) is not None
