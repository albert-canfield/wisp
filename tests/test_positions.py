"""Someone moving, placed on the hive's layout: the map feed and diagnostics."""
from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant

from custom_components.wisp.diagnostics import async_get_config_entry_diagnostics

from .conftest import AP, IP_A, IP_B, NODE_A, NODE_B, FakeClock, FakeUdp
from .fake_node import encode_hive_report, encode_report
from .test_init import HALL, OFFICE, fire, setup_hub

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def run(hass: HomeAssistant, udp: FakeUdp, scores: tuple[int, int, int, int], seconds: int = 3):
    """Hall and Office 5 m apart with the access point off to one side; scores x100 for
    (Hall hears AP, Hall hears Office, Office hears AP, Office hears Hall)."""
    entry = await setup_hub(hass, HALL, OFFICE)
    hub = entry.runtime_data
    hub.clock = FakeClock()
    udp.receive(encode_hive_report(1, NODE_A, 0x1234, [(NODE_A, -250, 0), (NODE_B, 250, 0)], [
        (NODE_A, 1, [(NODE_B, -60), (AP, -52)]),
        (NODE_B, 1, [(NODE_A, -61), (AP, -58)]),
    ]), IP_A)
    a_ap, a_b, b_ap, b_a = scores
    for seq in range(1, seconds + 1):
        udp.receive(encode_report(seq, NODE_A, [(AP, 0, -52, a_ap, 300, 20, 1), (NODE_B, 1, -60, a_b, 300, 10, 1)], uptime=60), IP_A)
        udp.receive(encode_report(seq, NODE_B, [(AP, 0, -58, b_ap, 300, 20, 1), (NODE_A, 1, -61, b_a, 300, 10, 1)], uptime=60), IP_B)
        hub.clock.now += 1
        await fire(hass, 1)
    return entry, hub


async def test_someone_moving_is_placed(hass: HomeAssistant, udp: FakeUdp) -> None:
    entry, hub = await run(hass, udp, (110, 340, 105, 320))  # the Hall to Office line is disturbed
    people = hub.map_snapshot()["people"]
    assert len(people) == 1
    person = people[0]
    assert set(person) == {"floor", "name", "x", "y", "quality", "walking"}
    assert person["walking"] is False  # the same line disturbed again and again: someone in place
    assert -2.5 <= person["x"] <= 2.5  # somewhere along the Hall to Office line, not at the access point
    assert abs(person["y"]) < 1.5
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    positions = diagnostics["rooms"]["positions"][""]
    assert AP in positions["placed"]  # the access point was placed from the rows
    assert positions["fix"]["quality"] > 0


async def test_quiet_floor_places_nobody(hass: HomeAssistant, udp: FakeUdp) -> None:
    _, hub = await run(hass, udp, (101, 100, 102, 99))
    assert "people" not in hub.map_snapshot()
