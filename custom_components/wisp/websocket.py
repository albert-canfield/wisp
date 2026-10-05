"""Websocket API for the map card and the panel: a snapshot at once, then changes at most once a second."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, MAP_INTERVAL
from .engine.rooms import MIN_SAMPLES
from .hub import WispHub

EMPTY_MAP: dict[str, Any] = {"nodes": [], "access_points": [], "links": [], "hive": None}
EMPTY_PANEL: dict[str, Any] = {
    "loaded": False, "nodes": [], "floors": [], "elsewhere": [], "min_samples": MIN_SAMPLES, "hive": None
}


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_subscribe_map)
    websocket_api.async_register_command(hass, ws_subscribe_panel)


def _hub(hass: HomeAssistant) -> WispHub | None:
    """The loaded hub. It can reload under a card or the panel."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


def map_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    """The hub's map, or an empty one while Wisp is not loaded."""
    hub = _hub(hass)
    return hub.map_snapshot() if hub else EMPTY_MAP


def panel_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    """The hub's nodes, floors and hive for the panel, or an empty panel while Wisp is not loaded."""
    hub = _hub(hass)
    return {"loaded": True, **hub.panel_snapshot()} if hub else EMPTY_PANEL


@callback
def _async_send_changes(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    snapshot: Callable[[HomeAssistant], dict[str, Any]],
    name: str,
) -> None:
    sent: dict[str, Any] | None = None

    @callback
    def send(_now: datetime | None = None) -> None:
        """Send the snapshot if it changed. The hive's age alone is no change: the frontend counts on."""
        nonlocal sent
        data = snapshot(hass)
        hive = data["hive"]
        compare = {**data, "hive": hive and {**hive, "age": None}}
        if compare == sent:
            return
        sent = compare
        connection.send_message(websocket_api.event_message(msg["id"], data))

    connection.subscriptions[msg["id"]] = async_track_time_interval(
        hass, send, MAP_INTERVAL, name=name, cancel_on_shutdown=True
    )
    connection.send_result(msg["id"])
    send()


@websocket_api.websocket_command({vol.Required("type"): "wisp/map/subscribe"})
@callback
def ws_subscribe_map(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    _async_send_changes(hass, connection, msg, map_snapshot, "wisp map")


@websocket_api.websocket_command({vol.Required("type"): "wisp/panel/subscribe"})
@websocket_api.require_admin
@callback
def ws_subscribe_panel(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The panel is for admins: node addresses and the calibration controls."""
    _async_send_changes(hass, connection, msg, panel_snapshot, "wisp panel")
