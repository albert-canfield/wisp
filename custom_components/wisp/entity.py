"""Base for per-link entities: created when a link first reports, rate-limited state writes."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HassJob, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later

from .const import WRITE_INTERVAL
from .engine import LinkKey, LinkState
from .hub import WispHub


class WispLinkEntity(Entity):
    """One value of one link (transmitter, receiver), on the receiving node's device.

    Reports arrive 5 times a second; the state is written at most once a second, the last
    value always lands, and availability changes are written at once.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    key: str

    def __init__(self, hub: WispHub, link: LinkKey) -> None:
        self.hub = hub
        self.link_key = link
        transmitter, receiver = link
        self._attr_unique_id = f"{receiver.replace(':', '')}_{transmitter.replace(':', '')}_{self.key}"
        self._attr_translation_key = self.key
        self._attr_translation_placeholders = {"link": hub.link_name(link)}
        self._attr_device_info = hub.device_info(receiver)
        self._written: tuple[bool, Any] | None = None
        self._last_write = 0.0
        self._cancel_flush: CALLBACK_TYPE | None = None

    @property
    def link(self) -> LinkState | None:
        return self.hub.link(self.link_key)

    @property
    def available(self) -> bool:
        return self.hub.link_available(self.link_key)

    def value(self) -> Any:
        """What the entity shows."""
        raise NotImplementedError

    def _urgent(self, value: Any) -> bool:
        """Write at once, even within the interval."""
        return False

    def _view(self) -> tuple[bool, Any]:
        return self.available, self.value()

    async def async_added_to_hass(self) -> None:
        # Home Assistant writes the first state right after this
        self._written, self._last_write = self._view(), self.hub.clock()
        self.async_on_remove(self.hub.async_listen_link(self.link_key, self._async_link_updated))
        self.async_on_remove(self._async_cancel_flush)

    @callback
    def _async_link_updated(self) -> None:
        view = self._view()
        if view == self._written:
            return
        if self._written is None or view[0] != self._written[0] or self._urgent(view[1]):
            self._async_write()
            return
        wait = WRITE_INTERVAL - (self.hub.clock() - self._last_write)
        if wait <= 0:
            self._async_write()
        elif self._cancel_flush is None:
            self._cancel_flush = async_call_later(
                self.hass, wait, HassJob(self._async_flush, "wisp state flush", cancel_on_shutdown=True)
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
