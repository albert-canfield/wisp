"""Identify, on each node's device: the node's own Identify button, which strobes its status LED
for 10 s (a second press stops it). Pressed through the node's ESPHome entity when Home Assistant
has it, else straight on the node's web page."""
from __future__ import annotations

from http import HTTPStatus

import aiohttp

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import WispEntity
from .hub import WispConfigEntry, WispHub, esphome_entity

IDENTIFY_PATH = "/button/Identify/press"  # ESPHome's web server, see firmware/common/status_led_rgb.yaml


async def async_setup_entry(
    hass: HomeAssistant, entry: WispConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    hub = entry.runtime_data
    added: set[str] = set()

    @callback
    def sync() -> None:
        """A button for every node, also those added later; Home Assistant removes a removed
        node's entities with its subentry."""
        added.intersection_update(hub.nodes)
        for mac, node in hub.nodes.items():
            if mac not in added:
                added.add(mac)
                async_add_entities([Identify(hub, mac)], config_subentry_id=node.subentry_id)

    sync()
    entry.async_on_unload(hub.async_listen_nodes(sync))


def esphome_identify(hass: HomeAssistant, mac: str) -> str | None:
    """The Identify button of the node's ESPHome device, if Home Assistant has one."""
    return esphome_entity(hass, mac, "button", "Identify")


class Identify(WispEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.IDENTIFY
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub: WispHub, mac: str) -> None:
        super().__init__(hub)
        self.mac = mac
        self._attr_unique_id = f"{mac.replace(':', '')}_identify"
        self._attr_device_info = hub.device_info(mac)

    def value(self) -> None:
        return None

    @property
    def available(self) -> bool:
        return self.mac in self.hub.nodes

    async def async_press(self) -> None:
        if (entity_id := esphome_identify(self.hass, self.mac)) is not None:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state != STATE_UNAVAILABLE:
                await self.hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
                return
        node = self.hub.nodes.get(self.mac)
        if node is None:
            raise HomeAssistantError("This node is not part of Wisp any more.")
        url = f"http://{node.address or node.host}{IDENTIFY_PATH}"
        try:
            async with async_get_clientsession(self.hass).post(url, data=b"", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == HTTPStatus.NOT_FOUND:
                    raise HomeAssistantError(f"{node.name} has no Identify button (no status LED in its firmware).")
                if r.status >= 400:
                    raise HomeAssistantError(f"{node.name} answered {r.status} to Identify.")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"{node.name} could not be reached: {err}") from err
