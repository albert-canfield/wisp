"""Services: calibrate a room (someone moving in it, or still) or the empty floor, clear calibration.
Progress shows on each floor's calibration sensor."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, config_validation as cv, floor_registry as fr

from .const import CALIBRATION_SECONDS, CONF_AREA, CONF_DELAY, CONF_DURATION, CONF_FLOOR, DOMAIN, NO_FLOOR
from .hub import WispHub

SERVICE_CALIBRATE_ROOM = "calibrate_room"
SERVICE_CALIBRATE_EMPTY = "calibrate_empty"
SERVICE_CLEAR_CALIBRATION = "clear_calibration"
SERVICE_STOP_CALIBRATION = "stop_calibration"

DURATION = vol.All(vol.Coerce(int), vol.Range(min=10, max=600))  # seconds; 600 samples are kept per room
DELAY = vol.All(vol.Coerce(int), vol.Range(min=0, max=300))  # seconds to leave the floor first
CONF_MODE = "mode"  # what a room records: someone walking around in it, or someone sitting still
MODES = ("moving", "still")


@callback
def async_register(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN,
        SERVICE_CALIBRATE_ROOM,
        _async_calibrate_room,
        vol.Schema({
            vol.Required(CONF_AREA): cv.string,
            vol.Optional(CONF_MODE, default=MODES[0]): vol.In(MODES),
            vol.Optional(CONF_DURATION, default=CALIBRATION_SECONDS): DURATION,
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CALIBRATE_EMPTY,
        _async_calibrate_empty,
        vol.Schema({
            vol.Optional(CONF_FLOOR): cv.string,
            vol.Optional(CONF_DURATION, default=CALIBRATION_SECONDS): DURATION,
            vol.Optional(CONF_DELAY, default=0): DELAY,
        }),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_CALIBRATION,
        _async_stop_calibration,
        vol.Schema({vol.Optional(CONF_FLOOR): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_CALIBRATION,
        _async_clear_calibration,
        vol.Schema({vol.Optional(CONF_AREA): cv.string}),
    )


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders or None
    )


def _hub(hass: HomeAssistant, nodes: bool = True) -> WispHub:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise _error("not_loaded")
    hub: WispHub = entries[0].runtime_data
    if nodes and not hub.nodes:
        raise _error("no_nodes")
    return hub


def _area(hass: HomeAssistant, value: str) -> ar.AreaEntry:
    """An area by id, as the selector gives it, or by name, as people write it."""
    registry = ar.async_get(hass)
    area = registry.async_get_area(value) or registry.async_get_area_by_name(value)
    if area is None:
        raise _error("unknown_area", area=value)
    return area


async def _async_calibrate_room(call: ServiceCall) -> None:
    hub = _hub(call.hass)
    area = _area(call.hass, call.data[CONF_AREA])
    floor = area.floor_id or NO_FLOOR
    if floor not in hub.presence.floors:
        if not floor:
            raise _error("area_without_floor", area=area.name)
        entry = fr.async_get(call.hass).async_get_floor(floor)
        raise _error("no_nodes_on_floor", area=area.name, floor=entry.name if entry else floor)
    hub.presence.async_calibrate(floor, area.id, call.data[CONF_DURATION], still=call.data[CONF_MODE] == "still")


async def _async_calibrate_empty(call: ServiceCall) -> None:
    hub = _hub(call.hass)
    value = call.data.get(CONF_FLOOR)
    floors = [_floor(call.hass, hub, value)] if value else list(hub.presence.floors)
    for floor in floors:
        hub.presence.async_calibrate(floor, None, call.data[CONF_DURATION], call.data[CONF_DELAY])


def _floor(hass: HomeAssistant, hub: WispHub, value: str) -> str:
    """A floor with nodes by id, as the selector gives it; the hub's own floor by its name; or by name."""
    registry = fr.async_get(hass)
    floor = registry.async_get_floor(value)
    own = hub.presence.floors.get(NO_FLOOR)
    if floor is None and own is not None and value == own.name:
        return NO_FLOOR
    floor = floor or registry.async_get_floor_by_name(value)
    if floor is None:
        raise _error("unknown_floor", floor=value)
    if floor.floor_id not in hub.presence.floors:
        raise _error("floor_without_nodes", floor=floor.name)
    return floor.floor_id


async def _async_stop_calibration(call: ServiceCall) -> None:
    """Ends a floor's recording early (or every floor's), keeping what it recorded."""
    hub = _hub(call.hass, nodes=False)
    value = call.data.get(CONF_FLOOR)
    floors = [_floor(call.hass, hub, value)] if value else list(hub.presence.engine.runs)
    for floor in floors:
        hub.presence.async_stop_run(floor)


async def _async_clear_calibration(call: ServiceCall) -> None:
    hub = _hub(call.hass, nodes=False)
    area = None
    if value := call.data.get(CONF_AREA):
        registry = ar.async_get(call.hass)
        found = registry.async_get_area(value) or registry.async_get_area_by_name(value)
        area = found.id if found else value  # a deleted area's samples can go too
    hub.presence.async_clear(area)
