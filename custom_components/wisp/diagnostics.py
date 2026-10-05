"""Diagnostics download: hub state, known nodes, their last reports and the links."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .hub import WispConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: WispConfigEntry) -> dict[str, Any]:
    return entry.runtime_data.diagnostics()
