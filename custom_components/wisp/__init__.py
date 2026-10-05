"""Wisp: WiFi Spatial Presence. Link reports from the nodes over UDP, entities per link, presence per room, a live map."""
from __future__ import annotations

from contextlib import suppress
import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import SOURCE_ZEROCONF
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow, UnknownFlow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from . import services, websocket
from .const import DOMAIN, PLATFORMS, STORE_VERSION, TITLE, VERSION
from .hub import WispConfigEntry, WispHub
from .presence import store_key

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
FRONTEND = Path(__file__).parent / "frontend"
CARD_URL = f"/{DOMAIN}/wisp-map-card.js"
PANEL_URL = f"/{DOMAIN}/wisp-panel.js"
PANEL_PATH = DOMAIN  # the panel's address: /wisp


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    websocket.async_register(hass)
    services.async_register(hass)
    await _async_register_frontend(hass)
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the map card and the panel from the integration, and load the card on every dashboard."""
    if "frontend" not in hass.config.components:  # no dashboards to load it, as in tests
        return
    try:
        await hass.http.async_register_static_paths([
            StaticPathConfig(CARD_URL, str(FRONTEND / "wisp-map-card.js"), True),
            StaticPathConfig(PANEL_URL, str(FRONTEND / "wisp-panel.js"), True),
        ])
        frontend.add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not register the Wisp map card and panel automatically: %s", err)


async def _async_register_panel(hass: HomeAssistant) -> None:
    """The Wisp panel in the sidebar, for admins. Registered once, it stays while the hub reloads."""
    if "frontend" not in hass.config.components or PANEL_PATH in hass.data.get(frontend.DATA_PANELS, {}):
        return
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_PATH,
            webcomponent_name="wisp-panel",
            sidebar_title=TITLE,
            sidebar_icon="mdi:shoe-print",
            module_url=f"{PANEL_URL}?v={VERSION}",
            config={"card": f"{CARD_URL}?v={VERSION}"},  # the panel loads the card if no dashboard did
            require_admin=True,
        )
    except ValueError as err:  # another integration has /wisp
        _LOGGER.warning("Could not add the Wisp panel: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: WispConfigEntry) -> bool:
    hub = WispHub(hass, entry)
    await hub.async_start()
    entry.runtime_data = hub
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, hub.async_stop))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    _async_adopt_discovered(hass)
    await _async_register_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WispConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        entry.runtime_data.async_stop()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: WispConfigEntry) -> None:
    """Deleting the hub deletes the room calibration too, so a new setup starts clean, and the panel."""
    await Store(hass, STORE_VERSION, store_key(entry.entry_id)).async_remove()
    frontend.async_remove_panel(hass, PANEL_PATH, warn_if_unknown=False)


async def _async_entry_updated(hass: HomeAssistant, entry: WispConfigEntry) -> None:
    """A node subentry was added, changed or removed, or the options changed: no reload, the hub follows."""
    entry.runtime_data.async_sync_nodes()
    entry.runtime_data.presence.async_apply_options()


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
