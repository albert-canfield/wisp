"""Websocket API for the map card: a snapshot at once, then changes at most once a second."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, MAP_INTERVAL
from .hub import WispHub

EMPTY_MAP: dict[str, Any] = {"nodes": [], "access_points": [], "links": [], "hive": None}


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_subscribe_map)


def map_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    """The hub's map, or an empty one while Wisp is not loaded. The hub can reload under a card."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    hub: WispHub | None = entries[0].runtime_data if entries else None
    return hub.map_snapshot() if hub else EMPTY_MAP


@websocket_api.websocket_command({vol.Required("type"): "wisp/map/subscribe"})
@callback
def ws_subscribe_map(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    sent: dict[str, Any] | None = None

    @callback
    def send(_now: datetime | None = None) -> None:
        """Send the map if it changed. The hive's age alone is no change: the card counts on."""
        nonlocal sent
        snapshot = map_snapshot(hass)
        hive = snapshot["hive"]
        compare = {**snapshot, "hive": hive and {**hive, "age": None}}
        if compare == sent:
            return
        sent = compare
        connection.send_message(websocket_api.event_message(msg["id"], snapshot))

    connection.subscriptions[msg["id"]] = async_track_time_interval(
        hass, send, MAP_INTERVAL, name="wisp map", cancel_on_shutdown=True
    )
    connection.send_result(msg["id"])
    send()
