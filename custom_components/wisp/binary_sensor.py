"""Binary sensors: motion per link, detected by the node on that link; presence per calibrated room;
on the hub, whether the nodes' hive is in sync."""
from __future__ import annotations

from functools import partial
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .engine import LinkKey
from .entity import WispLinkEntity, WispRoomEntity, async_follow_rooms, room_unique_id
from .hub import WispConfigEntry, WispHub


async def async_setup_entry(
    hass: HomeAssistant, entry: WispConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data

    @callback
    def add_link(link: LinkKey) -> None:
        async_add_entities([Motion(hub, link)], config_subentry_id=hub.subentry_id(link[1]))

    entry.async_on_unload(hub.async_listen_new_links(add_link))

    def wanted() -> dict[str, Any]:
        return {
            room_unique_id(hub, "hub", HiveInSync.key): partial(HiveInSync, hub),
            **{
                room_unique_id(hub, f"area_{area}", Presence.key): partial(Presence, hub, area)
                for area in hub.presence.calibrated_areas()
            },
        }

    entry.async_on_unload(async_follow_rooms(hub, "binary_sensor", wanted, async_add_entities))


class Motion(WispLinkEntity, BinarySensorEntity):
    key = "motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    @property
    def is_on(self) -> bool | None:
        return self.value()

    def value(self) -> bool | None:
        link = self.link
        return link.motion if link else None

    def _urgent(self, value: bool | None) -> bool:
        return True  # motion on or off is written at once


class Presence(WispRoomEntity, BinarySensorEntity):
    """On while the room won within the hold time; confidence of its latest win."""

    key = "presence"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, hub: WispHub, area: str) -> None:
        super().__init__(hub, f"area_{area}")
        self.area = area
        self._attr_translation_key = self.key
        self._attr_translation_placeholders = {"area": hub.presence.area_name(area)}

    @property
    def available(self) -> bool:
        return self.presence.available(self.presence.area_floor(self.area))

    @property
    def is_on(self) -> bool:
        return self.value()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"confidence": self.value()[1]}

    def value(self) -> tuple[bool, float | None]:
        confidence = self.presence.engine.presence(self.area, self.hub.clock())
        return confidence is not None, None if confidence is None else round(confidence, 2)

    def _urgent(self, value: tuple[bool, float | None]) -> bool:
        return value[0] != self._written[1][0]  # on or off is written at once


class HiveInSync(WispRoomEntity, BinarySensorEntity):
    """On while the nodes agree on the hive (the shared map they each solve the layout from)."""

    key = "hive_in_sync"
    _attr_translation_key = "hive_in_sync"
    _attr_icon = "mdi:hexagon-multiple"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: WispHub) -> None:
        super().__init__(hub, "hub")

    @property
    def is_on(self) -> bool | None:
        return self.value()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.value()[1]

    def value(self) -> tuple[bool | None, dict[str, Any]]:
        now = self.hub.clock()
        hive = self.hub.hive.current(now)
        if hive is None or not self.hub.hive.fresh(hive.reporter, now):
            return None, {"nodes": 0}
        return hive.in_sync, {"nodes": len(hive.layout)}

    def _urgent(self, value: tuple[bool | None, dict[str, Any]]) -> bool:
        return value[0] != self._written[1][0]  # in or out of sync is written at once
