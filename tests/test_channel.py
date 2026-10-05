"""The WiFi channel per floor: each node's access point, channel and setting in the panel, and
setting a floor's channel through ESPHome or the node's web page."""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from .conftest import AP, IP_A, IP_B, NODE_A, NODE_B, FakeUdp
from .fake_node import encode_report
from .test_init import HALL, OFFICE, setup_hub
from .test_plans import error, ok

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def esphome_node(hass: HomeAssistant, mac: str, channel: str, fixed: str) -> str:
    """An ESPHome device for the node with its Grid channel sensor and Fixed grid channel number."""
    entry = MockConfigEntry(domain="esphome")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, connections={(CONNECTION_NETWORK_MAC, mac)}, name=f"wisp {mac[-5:]}"
    )
    registry = er.async_get(hass)
    sensor = registry.async_get_or_create(
        "sensor", "esphome", f"{mac}-sensor-grid_channel", device_id=device.id, original_name="Grid channel", config_entry=entry
    )
    number = registry.async_get_or_create(
        "number", "esphome", f"{mac}-number-fixed_grid_channel", device_id=device.id, original_name="Fixed grid channel",
        config_entry=entry,
    )
    hass.states.async_set(sensor.entity_id, channel)
    hass.states.async_set(number.entity_id, fixed)
    return number.entity_id


async def test_floor_channel(hass: HomeAssistant, udp: FakeUdp, hass_ws_client, aioclient_mock) -> None:
    entry = await setup_hub(hass, HALL, OFFICE)
    hub = entry.runtime_data
    udp.receive(encode_report(1, NODE_A, [(AP, 0, -50, 101, 100, 20, 0)]), IP_A)
    await hass.async_block_till_done()
    number_a = esphome_node(hass, NODE_A, "11", "11")
    snapshot = hub.panel_snapshot()
    hall, office = snapshot["nodes"]
    assert hall["wifi"] == {"ap": AP, "rssi": -50, "channel": 11, "fixed": 11}
    assert office["wifi"] == {"ap": None, "rssi": None, "channel": None, "fixed": None}  # not in ESPHome yet
    assert snapshot["floors"][0]["channel"] == 11  # the one setting the floor's nodes show

    client = await hass_ws_client(hass)
    calls = async_mock_service(hass, "number", "set_value")
    aioclient_mock.post(f"http://{IP_B}/number/Fixed%20grid%20channel/set?value=6", status=200)
    result = await ok(client, type="wisp/floor/set_channel", channel=6)
    assert result == {"set": [NODE_A, NODE_B], "failed": []}
    assert [(c.data["entity_id"], c.data["value"]) for c in calls] == [(number_a, 6)]  # through ESPHome
    assert aioclient_mock.call_count == 1  # Office straight on its web page

    aioclient_mock.clear_requests()
    aioclient_mock.post(f"http://{IP_B}/number/Fixed%20grid%20channel/set?value=0", status=404)
    result = await ok(client, type="wisp/floor/set_channel", channel=0)
    assert result["set"] == [NODE_A] and result["failed"] == [
        {"mac": NODE_B, "name": "Office", "error": "Office has no channel setting yet: update its firmware."}
    ]
    assert (await error(client, type="wisp/floor/set_channel", channel=14))[0] == "invalid_format"
    assert (await error(client, type="wisp/floor/set_channel", floor="attic", channel=1))[0] == "not_found"
