"""Websocket API for the map card and the panel: a snapshot at once, then changes at most once a
second. The panel sets floor plans and places nodes and access points on them (admins only)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import math
import re
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, MAP_INTERVAL, NO_FLOOR
from .engine.rooms import MIN_SAMPLES
from .hub import WispHub
from .plan_images import async_delete_unused
from .presence import RoomPresence

EMPTY_MAP: dict[str, Any] = {"nodes": [], "access_points": [], "links": [], "hive": None}
EMPTY_PANEL: dict[str, Any] = {
    "loaded": False, "nodes": [], "floors": [], "elsewhere": [], "min_samples": MIN_SAMPLES, "hive": None
}
MAX_URL = 2048
MAX_PLACED = 64  # positions in one message


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_subscribe_map)
    websocket_api.async_register_command(hass, ws_subscribe_panel)
    websocket_api.async_register_command(hass, ws_set_plan)
    websocket_api.async_register_command(hass, ws_place)
    websocket_api.async_register_command(hass, ws_clear_plan)
    websocket_api.async_register_command(hass, ws_set_node_area)
    websocket_api.async_register_command(hass, ws_set_rooms)
    websocket_api.async_register_command(hass, ws_set_channel)


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


# Floor plans

def _image_url(value: Any) -> str:
    """A path on Home Assistant, such as /local/wisp/ground.png, an http or https address, or
    nothing: a blank plan, drawn as a grid."""
    url = cv.string(value).strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if (
        len(url) > MAX_URL
        or any(c.isspace() or not c.isprintable() for c in url)
        or not ((url.startswith("/") and not parts.scheme) or (parts.scheme in ("http", "https") and parts.netloc))
    ):
        raise vol.Invalid("expected a path such as /local/wisp/ground.png, or an http or https address")
    return url


def _mac(value: Any) -> str:
    mac = format_mac(cv.string(value))
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        raise vol.Invalid(f"not a MAC address: {value}")
    return mac


def _point(value: Any) -> tuple[float, float]:
    """[x, y] in metres."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise vol.Invalid("expected [x, y] in metres")
    x, y = (vol.Coerce(float)(v) for v in value)
    if not (math.isfinite(x) and math.isfinite(y)):
        raise vol.Invalid("expected [x, y] in metres")
    return x, y


FLOOR = vol.Any(None, cv.string)  # a Home Assistant floor id; None or "" for the hub's own floor
MAX_ROOMS = 64
MAX_RECTS = 16  # per room: an L-shaped room is two


def _rect(value: Any) -> tuple[float, float, float, float]:
    """[x, y, width, height] in metres, at least 25 cm a side."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise vol.Invalid("expected [x, y, width, height] in metres")
    x, y, w, h = (vol.Coerce(float)(v) for v in value)
    if not all(map(math.isfinite, (x, y, w, h))) or w < 0.25 or h < 0.25:
        raise vol.Invalid("expected [x, y, width, height] in metres, at least 0.25 m a side")
    return x, y, w, h


ROOMS = vol.All(
    vol.Schema({cv.string: vol.All([_rect], vol.Length(max=MAX_RECTS))}), vol.Length(max=MAX_ROOMS)
)
SIZE = vol.All(vol.Coerce(float), vol.Range(min=1, max=500))  # metres
PLACEMENTS = vol.All(vol.Schema({_mac: vol.Any(None, _point)}), vol.Length(max=MAX_PLACED))


def _presence(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> RoomPresence | None:
    hub = _hub(hass)
    if hub is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Wisp is not loaded.")
        return None
    return hub.presence


def _no_floor(connection: websocket_api.ActiveConnection, msg: dict[str, Any], floor: str) -> None:
    where = f"on the floor {floor}" if floor else "without a floor"
    connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"No Wisp node is {where}.")


@websocket_api.websocket_command({
    vol.Required("type"): "wisp/floor/set_plan",
    vol.Optional("floor"): FLOOR,
    vol.Required("url"): _image_url,
    vol.Required("width"): SIZE,
    vol.Required("height"): SIZE,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_plan(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """A floor's plan: its image (or none, for a grid) and size in metres. Any Home Assistant floor
    can have one before its nodes are there. A new image or size keeps the placed positions."""
    if (presence := _presence(hass, connection, msg)) is None:
        return
    floor = msg.get("floor") or NO_FLOOR
    if not presence.known_floor(floor):
        text = f"Home Assistant has no floor {floor}." if floor else "No Wisp node is without a floor."
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, text)
        return
    old = presence.plans.floors.get(floor)
    old_url = old.url if old else ""
    presence.plans.set_plan(floor, msg["url"], msg["width"], msg["height"])
    await presence.plans.async_save()
    await async_delete_unused(hass, old_url, {p.url for p in presence.plans.floors.values()})
    presence.async_plans_changed(floor)
    connection.send_result(msg["id"], presence.plan_view(floor))


@websocket_api.websocket_command({
    vol.Required("type"): "wisp/floor/place",
    vol.Optional("floor"): FLOOR,
    vol.Optional("nodes", default={}): PLACEMENTS,
    vol.Optional("access_points", default={}): PLACEMENTS,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_place(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Nodes (by MAC) and access points (by BSSID) on a floor's plan, in metres from its top left
    corner with y down; None lets Wisp place one again. Others keep their positions."""
    if (presence := _presence(hass, connection, msg)) is None:
        return
    floor = msg.get("floor") or NO_FLOOR
    if floor not in presence.floors:
        _no_floor(connection, msg, floor)
        return
    if (plan := presence.plans.floors.get(floor)) is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "This floor has no plan yet.")
        return
    nodes = presence.floors[floor].nodes
    for mac, point in msg["nodes"].items():
        if point is not None and mac not in nodes:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"The node {mac} is not on this floor.")
            return
    for point in (*msg["nodes"].values(), *msg["access_points"].values()):
        if point is not None and not (0 <= point[0] <= plan.width and 0 <= point[1] <= plan.height):
            connection.send_error(
                msg["id"], websocket_api.ERR_INVALID_FORMAT,
                f"{point[0]:g}, {point[1]:g} is off the plan, which is {plan.width:g} by {plan.height:g} m.",
            )
            return
    presence.plans.place(floor, msg["nodes"], msg["access_points"])
    await presence.plans.async_save()
    presence.async_plans_changed(floor)
    connection.send_result(msg["id"], presence.plan_view(floor))


@websocket_api.websocket_command({vol.Required("type"): "wisp/floor/clear", vol.Optional("floor"): FLOOR})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_clear_plan(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Removes a floor's plan and the positions placed on it: the floor is back on the hive's layout."""
    if (presence := _presence(hass, connection, msg)) is None:
        return
    floor = msg.get("floor") or NO_FLOOR
    old = presence.plans.floors.get(floor)
    if presence.plans.remove(floor):
        await presence.plans.async_save()
        await async_delete_unused(hass, old.url, {p.url for p in presence.plans.floors.values()})
        presence.async_plans_changed(floor)
    connection.send_result(msg["id"])


@websocket_api.websocket_command({
    vol.Required("type"): "wisp/node/set_area",
    vol.Required("mac"): _mac,
    vol.Required("area"): vol.Any(None, cv.string),
})
@websocket_api.require_admin
@callback
def ws_set_node_area(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The area a node stands in, set on its devices as anywhere in Home Assistant; its floor
    follows. None takes it out of every area."""
    hub = _hub(hass)
    if hub is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Wisp is not loaded.")
        return
    if msg["mac"] not in hub.nodes:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"No Wisp node {msg['mac']}.")
        return
    area = msg["area"] or None
    if area is not None and ar.async_get(hass).async_get_area(area) is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"No area {area}.")
        return
    hub.async_set_node_area(msg["mac"], area)
    connection.send_result(msg["id"])


@websocket_api.websocket_command({
    vol.Required("type"): "wisp/floor/set_rooms",
    vol.Optional("floor"): FLOOR,
    vol.Required("rooms"): ROOMS,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_rooms(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The rooms drawn on a floor's plan: per Home Assistant area, rectangles in plan metres. They
    give the house's outline, and someone moving is kept inside it, and inside the room room
    presence is sure of. Replaces the floor's rooms."""
    if (presence := _presence(hass, connection, msg)) is None:
        return
    floor = msg.get("floor") or NO_FLOOR
    if (plan := presence.plans.floors.get(floor)) is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "This floor has no plan yet.")
        return
    areas = ar.async_get(hass)
    for area, rects in msg["rooms"].items():
        if areas.async_get_area(area) is None:
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"No area {area}.")
            return
        for x, y, w, h in rects:
            if x < -0.01 or y < -0.01 or x + w > plan.width + 0.01 or y + h > plan.height + 0.01:
                connection.send_error(
                    msg["id"], websocket_api.ERR_INVALID_FORMAT,
                    f"A rectangle of {areas.async_get_area(area).name} is off the plan, which is "
                    f"{plan.width:g} by {plan.height:g} m.",
                )
                return
    presence.plans.set_rooms(floor, msg["rooms"])
    await presence.plans.async_save()
    presence.async_plans_changed(floor)
    connection.send_result(msg["id"], presence.plan_view(floor))


@websocket_api.websocket_command({
    vol.Required("type"): "wisp/floor/set_channel",
    vol.Optional("floor"): FLOOR,
    vol.Required("channel"): vol.All(vol.Coerce(int), vol.Range(min=0, max=13)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_channel(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The WiFi channel for every node on a floor (0: automatic, one channel for the house). Each
    node moves to that channel and the access point on it, so a floor can use its own access
    point. Answers which nodes took it and which failed, and why."""
    hub = _hub(hass)
    if hub is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Wisp is not loaded.")
        return
    floor = msg.get("floor") or NO_FLOOR
    if floor not in hub.presence.floors:
        _no_floor(connection, msg, floor)
        return
    done, failed = [], []
    for mac in sorted(hub.presence.floors[floor].nodes):
        try:
            await hub.async_set_channel(mac, msg["channel"])
            done.append(mac)
        except HomeAssistantError as err:
            failed.append({"mac": mac, "name": hub.nodes[mac].name, "error": str(err)})
    connection.send_result(msg["id"], {"set": done, "failed": failed})
