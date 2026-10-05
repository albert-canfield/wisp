"""Live map: the websocket subscription behind the card, and the card's registration."""
from __future__ import annotations

import pytest

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.wisp.const import VERSION

from .conftest import (
    AP,
    NODE_A,
    NODE_B,
    REAL_AP,
    REAL_HIVE,
    REAL_LINKS_1,
    REAL_LINKS_2,
    REAL_NODE_1,
    REAL_NODE_2,
    FakeClock,
    FakeUdp,
)
from .fake_node import encode_hive_report, encode_report
from .test_init import HALL, NODE_C, OFFICE, fire, setup_hub

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

EMPTY = {"nodes": [], "access_points": [], "links": [], "hive": None}
LAYOUT = [(NODE_A, -100, 50), (NODE_B, 100, -50)]
ROWS = [(NODE_A, 3, [(AP, -48), (NODE_B, -60)]), (NODE_B, 3, [(AP, -57), (NODE_A, -61)])]


def node_links(walking: bool) -> list[tuple]:
    return [
        (AP, 0, -55, 123, 210, 20, 0),
        (NODE_B, 1, -60, 310 if walking else 104, 150, 10, int(walking)),
        (NODE_C, 1, -70, 100, 0, 5, 0),  # a node that is neither added nor on the layout
    ]


async def subscribe(client) -> int:
    await client.send_json_auto_id({"type": "wisp/map/subscribe"})
    msg = await client.receive_json()
    assert msg["success"], msg
    return msg["id"]


async def next_map(client, sub: int) -> dict:
    msg = await client.receive_json()
    assert (msg["id"], msg["type"]) == (sub, "event")
    return msg["event"]


async def test_snapshot_then_changes_at_most_once_a_second(hass: HomeAssistant, udp: FakeUdp, hass_ws_client) -> None:
    entry = await setup_hub(hass, HALL, OFFICE)
    clock = entry.runtime_data.clock = FakeClock()
    client = await hass_ws_client(hass)
    sub = await subscribe(client)
    assert await next_map(client, sub) == {
        "nodes": [
            {"mac": NODE_A, "name": "Hall", "online": False, "x": None, "y": None},
            {"mac": NODE_B, "name": "Office", "online": False, "x": None, "y": None},
        ],
        "access_points": [],
        "links": [],
        "hive": None,
    }

    udp.receive(encode_report(1, NODE_A, node_links(walking=True)))
    udp.receive(encode_hive_report(1, NODE_A, 0xBEEF, LAYOUT, ROWS))
    await fire(hass, 1)
    assert await next_map(client, sub) == {
        "nodes": [
            {"mac": NODE_A, "name": "Hall", "online": True, "x": -1.0, "y": 0.5},
            {"mac": NODE_B, "name": "Office", "online": False, "x": 1.0, "y": -0.5},
        ],
        "access_points": [  # signal from the hive rows: Office hears it there, not in a link report yet
            {"bssid": AP, "label": "AP b6:70", "heard_by": [{"node": NODE_A, "rssi": -48}, {"node": NODE_B, "rssi": -57}]}
        ],
        "links": [
            {"transmitter": NODE_B, "receiver": NODE_A, "kind": "node", "score": 3.1, "motion": True},
            {"transmitter": AP, "receiver": NODE_A, "kind": "ap", "score": 1.2, "motion": False},
        ],
        "hive": {"hash": "0000beef", "in_sync": True, "nodes": 2, "age": 0},
    }

    # The hive's age alone is no change; two reports within a second make one update
    clock.now += 2
    await fire(hass, 1)
    udp.receive(encode_report(2, NODE_A, node_links(walking=False)))
    udp.receive(encode_report(3, NODE_A, node_links(walking=False)))
    await fire(hass, 1)
    update = await next_map(client, sub)
    assert update["links"][0] == {"transmitter": NODE_B, "receiver": NODE_A, "kind": "node", "score": 1.0, "motion": False}
    assert update["hive"]["age"] == 2

    # Quiet links leave the map; node and access point stay, the layout with them
    clock.now += 11
    await fire(hass, 1)
    update = await next_map(client, sub)
    assert update["links"] == [] and update["access_points"][0]["bssid"] == AP
    assert update["nodes"][0]["x"] == -1.0 and update["nodes"][0]["online"] is True  # hive report still fresh
    clock.now += 5
    await fire(hass, 1)
    assert (await next_map(client, sub))["nodes"][0]["online"] is False


async def test_map_follows_the_hub_through_a_reload(hass: HomeAssistant, udp: FakeUdp, hass_ws_client) -> None:
    entry = await setup_hub(hass, HALL)
    client = await hass_ws_client(hass)
    sub = await subscribe(client)
    assert len((await next_map(client, sub))["nodes"]) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await fire(hass, 1)
    assert await next_map(client, sub) == EMPTY
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await fire(hass, 1)
    assert (await next_map(client, sub))["nodes"][0]["name"] == "Hall"


async def test_map_from_real_packets(hass: HomeAssistant, udp: FakeUdp, hass_ws_client) -> None:
    await setup_hub(hass, (REAL_NODE_1, "192.168.10.75", "Shelf"), (REAL_NODE_2, "192.168.10.76", "Desk"))
    udp.receive(REAL_LINKS_1, "192.168.10.75")
    udp.receive(REAL_LINKS_2, "192.168.10.76")
    udp.receive(REAL_HIVE, "192.168.10.75")
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    sub = await subscribe(client)
    assert await next_map(client, sub) == {
        "nodes": [
            {"mac": REAL_NODE_1, "name": "Shelf", "online": True, "x": -0.2, "y": 0.0},
            {"mac": REAL_NODE_2, "name": "Desk", "online": True, "x": 0.2, "y": 0.0},
        ],
        "access_points": [
            {
                "bssid": REAL_AP,
                "label": "AP 12:f9",
                "heard_by": [{"node": REAL_NODE_2, "rssi": -49}, {"node": REAL_NODE_1, "rssi": -62}],
            }
        ],
        "links": [
            {"transmitter": REAL_NODE_1, "receiver": REAL_NODE_2, "kind": "node", "score": 1.1, "motion": False},
            {"transmitter": REAL_AP, "receiver": REAL_NODE_2, "kind": "ap", "score": 1.0, "motion": False},
            {"transmitter": REAL_NODE_2, "receiver": REAL_NODE_1, "kind": "node", "score": 1.0, "motion": False},
        ],
        "hive": {"hash": "0bcd88ba", "in_sync": True, "nodes": 2, "age": 0},
    }


async def test_card_is_served_and_loaded_on_every_dashboard(hass: HomeAssistant, udp: FakeUdp, hass_client) -> None:
    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")  # stands in for the real frontend, not installed for tests
    hass.data[DATA_EXTRA_MODULE_URL] = urls = set()
    await setup_hub(hass, HALL)
    assert urls == {f"/wisp/wisp-map-card.js?v={VERSION}"}
    resp = await (await hass_client()).get("/wisp/wisp-map-card.js")
    assert resp.status == 200
    assert 'customElements.define("wisp-map-card"' in await resp.text()


async def test_no_card_without_the_frontend(hass: HomeAssistant, udp: FakeUdp) -> None:
    await setup_hub(hass, HALL)
    assert DATA_EXTRA_MODULE_URL not in hass.data
