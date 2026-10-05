"""Base entities: per link on the receiving node's device, per floor and room on the hub's device.
State writes are rate-limited."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HassJob, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import QUIET_WRITE_INTERVAL, WRITE_INTERVAL
from .engine import LinkKey, LinkState
from .hub import WispHub


class WispEntity(Entity):
    """Updates come 5 times a second (links) or every second (rooms); the state is written at most
    once a second, availability and urgent changes at once, and a change too small to matter (see
    _significant) at most once a minute, so the recorder stays light. The last value always lands.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: WispHub) -> None:
        self.hub = hub
        self._written: tuple[bool, Any] | None = None
        self._last_write = 0.0
        self._cancel_flush: CALLBACK_TYPE | None = None
        self._flush_at = 0.0

    def value(self) -> Any:
        """What the entity shows."""
        raise NotImplementedError

    def _urgent(self, value: Any) -> bool:
        """Write at once, even within the interval."""
        return False

    def _significant(self, value: Any) -> bool:
        """Worth a write within a second; otherwise it waits up to QUIET_WRITE_INTERVAL."""
        return True

    def _view(self) -> tuple[bool, Any]:
        return self.available, self.value()

    async def async_added_to_hass(self) -> None:
        # Home Assistant writes the first state right after this
        self._written, self._last_write = self._view(), self.hub.clock()
        self.async_on_remove(self._async_cancel_flush)

    @callback
    def _async_updated(self) -> None:
        view = self._view()
        if view == self._written:
            return
        if self._written is None or view[0] != self._written[0] or self._urgent(view[1]):
            self._async_write()
            return
        now = self.hub.clock()
        due = self._last_write + (WRITE_INTERVAL if self._significant(view[1]) else QUIET_WRITE_INTERVAL)
        if due <= now:
            self._async_write()
        elif self._cancel_flush is None or due < self._flush_at:  # none pending, or this one is sooner
            self._async_cancel_flush()
            self._flush_at = due
            self._cancel_flush = async_call_later(
                self.hass, due - now, HassJob(self._async_flush, "wisp state flush", cancel_on_shutdown=True)
            )

    @callback
    def _async_flush(self, _now: datetime) -> None:
        self._cancel_flush = None
        if self._view() != self._written:
            self._async_write()

    @callback
    def _async_write(self) -> None:
        self._async_cancel_flush()
        self._written, self._last_write = self._view(), self.hub.clock()
        self.async_write_ha_state()

    @callback
    def _async_cancel_flush(self) -> None:
        if self._cancel_flush:
            self._cancel_flush()
            self._cancel_flush = None


class WispLinkEntity(WispEntity):
    """One value of one link (transmitter, receiver), on the receiving node's device."""

    key: str

    def __init__(self, hub: WispHub, link: LinkKey) -> None:
        super().__init__(hub)
        self.link_key = link
        transmitter, receiver = link
        self._attr_unique_id = f"{receiver.replace(':', '')}_{transmitter.replace(':', '')}_{self.key}"
        self._attr_translation_key = self.key
        self._attr_translation_placeholders = {"link": hub.link_name(link)}
        self._attr_device_info = hub.device_info(receiver)

    @property
    def link(self) -> LinkState | None:
        return self.hub.link(self.link_key)

    @property
    def available(self) -> bool:
        return self.hub.link_available(self.link_key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hub.async_listen_link(self.link_key, self._async_updated))


def room_unique_id(hub: WispHub, scope: str, key: str) -> str:
    """Floor and room entities start with the hub's entry id; link entities with a MAC."""
    return f"{hub.entry.entry_id}_{scope}_{key}"


class WispRoomEntity(WispEntity):
    """One value of a floor or a room, on the hub's device, updated every second."""

    key: str

    def __init__(self, hub: WispHub, scope: str) -> None:
        super().__init__(hub)
        self.presence = hub.presence
        self._attr_unique_id = room_unique_id(hub, scope, self.key)
        self._attr_device_info = self.presence.device_info()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.presence.async_listen(self._async_updated))


@callback
def async_follow_rooms(
    hub: WispHub,
    domain: str,
    wanted: Callable[[], dict[str, Callable[[], WispRoomEntity]]],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> CALLBACK_TYPE:
    """Keep a platform's floor and room entities in step with the floors and calibrated areas.

    wanted gives a factory per unique id. New ones are added; the others are removed from the
    entity registry, also those left from before a restart, and then remove themselves.
    """
    added: set[str] = set()
    prefix = f"{hub.entry.entry_id}_"

    @callback
    def sync() -> None:
        want = wanted()
        registry = er.async_get(hub.hass)
        for reg in er.async_entries_for_config_entry(registry, hub.entry.entry_id):
            if reg.domain == domain and reg.unique_id.startswith(prefix) and reg.unique_id not in want:
                registry.async_remove(reg.entity_id)
        added.intersection_update(want)
        new = [make() for unique_id, make in want.items() if unique_id not in added]
        added.update(want)
        if new:
            async_add_entities(new)

    return hub.presence.async_listen_entities(sync)
