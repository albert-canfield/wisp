"""The Wisp panel: its place in the sidebar, the websocket subscription behind it, and the empty
floor calibration it starts after a delay."""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, DATA_PANELS
from homeassistant.const import EVENT_PANELS_UPDATED
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr, floor_registry as fr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.setup import async_setup_component

from custom_components.wisp import _asset_versions
from custom_components.wisp.const import DOMAIN
from custom_components.wisp.websocket import EMPTY_PANEL

from .conftest import IP_A, NODE_A, NODE_B, FakeUdp
from .fake_node import encode_hive_report
from .test_init import HALL, OFFICE, fire, setup_hub
from .test_map import LAYOUT, ROWS, next_map
from .test_rooms import CALIBRATION, House, setup_with_areas

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def frontend(hass: HomeAssistant) -> None:
    """http serves the files; the frontend itself is not installed for tests."""
    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = set()


async def subscribe(client) -> int:
    await client.send_json_auto_id({"type": "wisp/panel/subscribe"})
    msg = await client.receive_json()
    assert msg["success"], msg
    return msg["id"]


async def test_panel_is_served_and_registered_once(hass: HomeAssistant, udp: FakeUdp, hass_client) -> None:
    await frontend(hass)
    updates = async_capture_events(hass, EVENT_PANELS_UPDATED)
    entry = await setup_hub(hass, HALL)
    panel = hass.data[DATA_PANELS]["wisp"]
    response = panel.to_response()
    assert (response["url_path"], response["component_name"], response["title"], response["icon"]) == (
        "wisp", "custom", "Wisp", "mdi:shoe-print"
    )
    assert response["require_admin"] is True
    versions = _asset_versions()  # a short hash per file: an update gives browsers new addresses
    assert response["config"]["card"] == f"/wisp/wisp-map-card.js?v={versions['wisp-map-card.js']}"
    custom = response["config"]["_panel_custom"]
    assert (custom["name"], custom["module_url"], custom["embed_iframe"]) == (
        "wisp-panel", f"/wisp/wisp-panel.js?v={versions['wisp-panel.js']}", False
    )
    client = await hass_client()
    resp = await client.get(custom["module_url"])
    assert resp.status == 200
    assert 'customElements.define("wisp-panel"' in await resp.text()
    assert len(updates) == 1

    # A reload keeps the panel: nothing registers twice, the page open on it stays
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DATA_PANELS]["wisp"] is panel and len(updates) == 1

    # Deleting the hub takes the panel; a new hub brings it back
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert "wisp" not in hass.data[DATA_PANELS]
    await setup_hub(hass, HALL)
    assert "wisp" in hass.data[DATA_PANELS]
    assert (await client.get("/wisp/wisp-panel.js")).status == 200  # served once, still there


async def test_no_panel_without_the_frontend(hass: HomeAssistant, udp: FakeUdp) -> None:
    await setup_hub(hass, HALL)
    assert "wisp" not in hass.data.get(DATA_PANELS, {})


async def test_snapshot_of_nodes_floors_and_areas(hass: HomeAssistant, udp: FakeUdp, hass_ws_client) -> None:
    floors, areas = fr.async_get(hass), ar.async_get(hass)
    ground = floors.async_create("Ground floor", level=0)
    upstairs = floors.async_create("Upstairs", level=1)
    for name, floor in (("Hall", ground), ("Kitchen", ground), ("Bedroom", upstairs), ("Garden", None), ("Office", None)):
        areas.async_create(name, floor_id=floor and floor.floor_id)
    esphome = MockConfigEntry(domain="esphome", unique_id=NODE_A)
    esphome.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=esphome.entry_id, connections={(CONNECTION_NETWORK_MAC, NODE_A)}, name="Wisp 535001"
    )
    entry = await setup_with_areas(hass, (*HALL, "hall"), OFFICE)  # Office has no area: the hub's own floor
    client = await hass_ws_client(hass)
    sub = await subscribe(client)
    assert await next_map(client, sub) == {
        "loaded": True,
        "nodes": [
            {
                "mac": NODE_A, "name": "Hall", "added": True, "host": IP_A, "area": "hall", "area_name": "Hall",
                "floor": "ground_floor", "floor_name": "Ground floor", "online": False, "placed": False,
                "device_id": device.id,
            },
            {
                "mac": NODE_B, "name": "Office", "added": True, "host": "192.168.1.51", "area": None, "area_name": None,
                "floor": None, "floor_name": "Wisp", "online": False, "placed": False, "device_id": None,
            },
        ],
        "floors": [
            {
                "floor": "ground_floor", "name": "Ground floor", "nodes": [NODE_A], "live_links": 0,
                "room": None, "area": None, "confidence": None, "empty_samples": 0, "run": None,
                "areas": [{"area": "hall", "name": "Hall", "nodes": 1, "samples": 0, "presence": False, "confidence": None}],
                "other_areas": [{"area": "kitchen", "name": "Kitchen"}],
            },
            {  # every Home Assistant floor is there, with nodes or not: a plan can wait for them
                "floor": "upstairs", "name": "Upstairs", "nodes": [], "live_links": 0,
                "room": None, "area": None, "confidence": None, "empty_samples": 0, "run": None,
                "areas": [], "other_areas": [{"area": "bedroom", "name": "Bedroom"}],
            },
            {
                "floor": None, "name": "Wisp", "nodes": [NODE_B], "live_links": 0,
                "room": None, "area": None, "confidence": None, "empty_samples": 0, "run": None,
                "areas": [],
                "other_areas": [{"area": "garden", "name": "Garden"}, {"area": "office", "name": "Office"}],
            },
        ],
        "elsewhere": [],
        "areas": [  # for the node area pickers, by floor
            {"area": "hall", "name": "Hall", "floor": "ground_floor", "floor_name": "Ground floor"},
            {"area": "kitchen", "name": "Kitchen", "floor": "ground_floor", "floor_name": "Ground floor"},
            {"area": "bedroom", "name": "Bedroom", "floor": "upstairs", "floor_name": "Upstairs"},
            {"area": "garden", "name": "Garden", "floor": None, "floor_name": None},
            {"area": "office", "name": "Office", "floor": None, "floor_name": None},
        ],
        "min_samples": 20,
        "hive": None,
    }

    # Kitchen while it records, then calibrated: it leaves the other areas for the floor's rooms
    house = House(hass, udp, entry)
    await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "Kitchen", "duration": 25}, blocking=True)
    await house.seconds(5, "kitchen")
    ground_floor = entry.runtime_data.panel_snapshot()["floors"][0]
    assert ground_floor["run"] == {
        "area": "kitchen", "name": "Kitchen", "starts_in": 0, "seconds_left": 20, "recorded": 5, "skipped": 0
    }
    assert [a["area"] for a in ground_floor["areas"]] == ["hall", "kitchen"] and ground_floor["other_areas"] == []
    await house.seconds(20, "kitchen")
    ground_floor = entry.runtime_data.panel_snapshot()["floors"][0]
    assert ground_floor["run"] is None and ground_floor["live_links"] == 2  # Hall hears the access point and Office
    assert (ground_floor["room"], ground_floor["area"], ground_floor["confidence"]) == ("Kitchen", "kitchen", 1.0)
    assert ground_floor["areas"][1] == {
        "area": "kitchen", "name": "Kitchen", "nodes": 0, "samples": 25, "presence": True, "confidence": 1.0
    }

    # Kitchen moves upstairs, where no node is yet: its samples go with it, shown there to clear
    areas.async_update("kitchen", floor_id=upstairs.floor_id)
    await hass.async_block_till_done()
    snapshot = entry.runtime_data.panel_snapshot()
    assert snapshot["elsewhere"] == []
    assert [a["area"] for a in snapshot["floors"][0]["areas"]] == ["hall"]
    assert [a["area"] for a in snapshot["floors"][1]["areas"]] == ["kitchen"]
    # A calibrated area on no floor while no node is without one has nowhere else to be shown
    areas.async_update("kitchen", floor_id=None)
    entry.runtime_data.async_set_node_area(NODE_B, "hall")
    await hass.async_block_till_done()
    assert entry.runtime_data.panel_snapshot()["elsewhere"] == [{"area": "kitchen", "name": "Kitchen", "samples": 25}]


async def test_changes_at_most_once_a_second(hass: HomeAssistant, udp: FakeUdp, hass_ws_client) -> None:
    ar.async_get(hass).async_create("Kitchen")
    entry = await setup_hub(hass, HALL, OFFICE)
    house = House(hass, udp, entry)
    client = await hass_ws_client(hass)
    sub = await subscribe(client)
    first = await next_map(client, sub)
    assert first["loaded"] and first["hive"] is None and first["floors"][0]["run"] is None

    udp.receive(encode_hive_report(1, NODE_A, 0xBEEF, LAYOUT, ROWS))
    await fire(hass, 1)
    snapshot = await next_map(client, sub)
    assert snapshot["hive"] == {"hash": "0000beef", "in_sync": True, "nodes": 2, "age": 0}
    assert [(n["online"], n["placed"]) for n in snapshot["nodes"]] == [(True, True), (False, True)]

    # The hive's age alone is no change; a calibration starting is
    house.clock.now += 2
    await fire(hass, 1)
    await hass.services.async_call(DOMAIN, "calibrate_room", {"area": "kitchen", "duration": 30}, blocking=True)
    await fire(hass, 1)
    snapshot = await next_map(client, sub)
    assert snapshot["hive"]["age"] == 2
    run = snapshot["floors"][0]["run"]
    assert (run["area"], run["name"], run["starts_in"], run["seconds_left"]) == ("kitchen", "Kitchen", 0, 30)

    # The hub can reload under the panel
    assert await hass.config_entries.async_unload(entry.entry_id)
    await fire(hass, 1)
    assert await next_map(client, sub) == EMPTY_PANEL
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await fire(hass, 1)
    assert (await next_map(client, sub))["loaded"] is True


async def test_panel_is_for_admins(
    hass: HomeAssistant, udp: FakeUdp, hass_ws_client, hass_read_only_access_token: str
) -> None:
    await setup_hub(hass, HALL)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({"type": "wisp/panel/subscribe"})
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "unauthorized"
    await client.send_json_auto_id({"type": "wisp/map/subscribe"})  # the map card stays open to everyone
    assert (await client.receive_json())["success"]


async def test_empty_floor_waits_for_everyone_to_leave(hass: HomeAssistant, udp: FakeUdp) -> None:
    house = House(hass, udp, await setup_hub(hass, HALL, OFFICE))
    hub = house.entry.runtime_data
    await hass.services.async_call(DOMAIN, "calibrate_empty", {"duration": 20, "delay": 30}, blocking=True)
    await hass.async_block_till_done()
    assert hub.panel_snapshot()["floors"][0]["run"] == {
        "area": None, "name": None, "starts_in": 30, "seconds_left": 50, "recorded": 0, "skipped": 0
    }
    calibration = hass.states.get(CALIBRATION)
    assert calibration.state == "recording" and calibration.attributes["seconds_left"] == 50

    await house.seconds(30, "kitchen")  # someone still on the way out: nothing recorded
    run = hub.panel_snapshot()["floors"][0]["run"]
    assert (run["starts_in"], run["seconds_left"], run["recorded"], run["skipped"]) == (0, 20, 0, 0)
    await house.seconds(20, None)
    floor = hub.panel_snapshot()["floors"][0]
    assert floor["run"] is None and floor["empty_samples"] == 20
    with pytest.raises(Exception, match="delay"):
        await hass.services.async_call(DOMAIN, "calibrate_empty", {"delay": 301}, blocking=True)


async def test_empty_floor_of_the_hub_by_its_name(hass: HomeAssistant, udp: FakeUdp) -> None:
    ground = fr.async_get(hass).async_create("Ground floor")
    ar.async_get(hass).async_create("Kitchen", floor_id=ground.floor_id)
    entry = await setup_with_areas(hass, (*HALL, "kitchen"), OFFICE)
    presence = entry.runtime_data.presence
    assert list(presence.floors) == ["ground_floor", ""]
    await hass.services.async_call(DOMAIN, "calibrate_empty", {"floor": "Wisp"}, blocking=True)
    assert list(presence.engine.runs) == [""]
    await hass.services.async_call(DOMAIN, "calibrate_empty", {"floor": "ground_floor"}, blocking=True)
    assert sorted(presence.engine.runs) == ["", "ground_floor"]

    # Once every node is on a floor, the hub has none of its own
    sub_b = next(s for s in entry.subentries.values() if s.unique_id == NODE_B)
    hass.config_entries.async_update_subentry(entry, sub_b, data={**sub_b.data, "area": "kitchen"})
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "calibrate_empty", {"floor": "Wisp"}, blocking=True)
    assert err.value.translation_key == "unknown_floor"
