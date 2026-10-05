"""Sensors per link: motion score, and signal and spread for diagnostics."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .engine import LinkKey
from .entity import WispLinkEntity
from .hub import WispConfigEntry


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
