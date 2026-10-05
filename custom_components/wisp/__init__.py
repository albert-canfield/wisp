"""Wisp: WiFi Spatial Presence. Link reports from the nodes over UDP, entities per link."""
from __future__ import annotations

from contextlib import suppress

from homeassistant.config_entries import SOURCE_ZEROCONF
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow, UnknownFlow

from .const import DOMAIN, PLATFORMS
from .hub import WispConfigEntry, WispHub


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
