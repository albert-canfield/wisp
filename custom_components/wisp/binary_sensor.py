"""Binary sensor per link: motion detected by the node on that link."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
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
        async_add_entities([Motion(hub, link)], config_subentry_id=hub.subentry_id(link[1]))

    entry.async_on_unload(hub.async_listen_new_links(add_link))


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
