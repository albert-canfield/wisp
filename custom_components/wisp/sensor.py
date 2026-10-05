"""Sensors per link: motion score, and signal and spread for diagnostics. Per floor: room and calibration."""
from __future__ import annotations

from functools import partial
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .engine import LinkKey
from .entity import WispLinkEntity, WispRoomEntity, async_follow_rooms, room_unique_id
from .hub import WispConfigEntry, WispHub
from .presence import EMPTY, floor_scope


async def async_setup_entry(
    hass: HomeAssistant, entry: WispConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data

    @callback
    def add_link(link: LinkKey) -> None:
        async_add_entities(
            [MotionScore(hub, link), Signal(hub, link), Spread(hub, link)],
            config_subentry_id=hub.subentry_id(link[1]),
        )

    entry.async_on_unload(hub.async_listen_new_links(add_link))

    def wanted() -> dict[str, Any]:
        out = {}
        for floor in hub.presence.floors_in_use():
            for cls in (Room, Calibration):
                out[room_unique_id(hub, floor_scope(floor), cls.key)] = partial(cls, hub, floor)
        return out

    entry.async_on_unload(async_follow_rooms(hub, "sensor", wanted, async_add_entities))


class LinkSensor(WispLinkEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | int | None:
        return self.value()


class MotionScore(LinkSensor):
    """1 = as quiet as usual, higher = more disturbed."""

    key = "motion_score"
    _attr_suggested_display_precision = 2

    def value(self) -> float | None:
        link = self.link
        return None if link is None or link.score is None else round(link.score, 2)


class Signal(LinkSensor):
    key = "signal"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False  # 4 entities per link would load the recorder

    def value(self) -> int | None:
        link = self.link
        return link.rssi if link else None


class Spread(LinkSensor):
    """How much the signal shape moves right now."""

    key = "spread"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 2
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def value(self) -> float | None:
        link = self.link
        return round(link.spread, 2) if link else None


class FloorSensor(WispRoomEntity, SensorEntity):
    """Named after its floor; the hub's own floor needs no name, the device has it."""

    def __init__(self, hub: WispHub, floor: str) -> None:
        super().__init__(hub, floor_scope(floor))
        self.floor = floor
        if floor:
            self._attr_translation_key = f"floor_{self.key}"
            self._attr_translation_placeholders = {"floor": hub.presence.floors[floor].name}
        else:
            self._attr_translation_key = self.key

    @property
    def native_value(self) -> str | None:
        return self.value()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.value()[1]


class Room(FloorSensor):
    """The room someone moves in on the floor, "none" while nobody moves."""

    key = "room"
    _attr_icon = "mdi:floor-plan"
    _unrecorded_attributes = frozenset({"probabilities"})

    @property
    def available(self) -> bool:
        return self.presence.available(self.floor)

    def value(self) -> tuple[str | None, dict[str, Any]]:
        decision = self.presence.engine.decisions.get(self.floor)
        confidence = None if decision is None or decision.confidence is None else round(decision.confidence, 2)
        attrs = {"confidence": confidence, "probabilities": self.presence.probabilities(decision)}
        return self.presence.room(self.floor), attrs

    def _urgent(self, value: tuple[str | None, dict[str, Any]]) -> bool:
        return value[0] != self._written[1][0]  # another room is written at once


class Calibration(FloorSensor):
    """Idle, or recording a room (or the empty floor) with the seconds left; samples per class."""

    key = "calibration"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "recording"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:walk"
    _unrecorded_attributes = frozenset({"seconds_left", "samples"})

    @property
    def available(self) -> bool:
        return self.hub.transport is not None

    def value(self) -> tuple[str, dict[str, Any]]:
        presence = self.presence
        run = presence.engine.runs.get(self.floor)
        attrs: dict[str, Any] = {
            "recording": None if run is None else EMPTY if run.area is None else presence.area_name(run.area),
            "seconds_left": None if run is None else presence.seconds_left(run),
            "samples": presence.samples(self.floor),
        }
        return ("idle" if run is None else "recording"), attrs

    def _urgent(self, value: tuple[str, dict[str, Any]]) -> bool:
        state, attrs = self._written[1]
        return value[0] != state or value[1]["recording"] != attrs["recording"]  # a run starts or ends
