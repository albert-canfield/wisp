"""Wisp: WiFi Spatial Presence. Link reports from the nodes over UDP, entities per link, a live map."""
from __future__ import annotations

from contextlib import suppress
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import SOURCE_ZEROCONF
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow, UnknownFlow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .const import DOMAIN, PLATFORMS, VERSION
from .hub import WispConfigEntry, WispHub

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARD_URL = f"/{DOMAIN}/wisp-map-card.js"
CARD_FILE = Path(__file__).parent / "frontend" / "wisp-map-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    websocket.async_register(hass)
    await _async_register_card(hass)
    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the map card from the integration and load it on every dashboard."""
    if "frontend" not in hass.config.components:  # no dashboards to load it, as in tests
        return
    try:
        await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_FILE), True)])
        add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not register the Wisp map card automatically: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: WispConfigEntry) -> bool:
    hub = WispHub(hass, entry)
    await hub.async_start()
    entry.runtime_data = hub
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, hub.async_stop))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_nodes_changed))
    _async_adopt_discovered(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WispConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        entry.runtime_data.async_stop()
    return ok


async def _async_nodes_changed(hass: HomeAssistant, entry: WispConfigEntry) -> None:
    """A node subentry was added, changed or removed: no reload, the hub follows."""
    entry.runtime_data.async_sync_nodes()


@callback
def _async_adopt_discovered(hass: HomeAssistant) -> None:
    """Nodes found before the hub existed join it now, without another confirmation."""
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN, match_context={"source": SOURCE_ZEROCONF}):
        # The flow that created the hub has the hub's unique id and is still finishing
        if flow.get("step_id") == "discovery_confirm" and flow["context"].get("unique_id") != DOMAIN:
            hass.async_create_task(_async_confirm(hass, flow["flow_id"]), "wisp adopt node")


async def _async_confirm(hass: HomeAssistant, flow_id: str) -> None:
    with suppress(UnknownFlow, AbortFlow):
        await hass.config_entries.flow.async_configure(flow_id, {})
