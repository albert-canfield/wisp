"""Floor plans: the websocket commands that set them and place nodes, their storage, the map and
panel feeds, and someone moving placed in plan metres."""
from __future__ import annotations

import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, floor_registry as fr

from custom_components.wisp.const import DOMAIN

from .conftest import AP, IP_A, IP_B, NODE_A, NODE_B, FakeClock, FakeUdp
from .fake_node import encode_hive_report, encode_report
from .test_init import HALL, OFFICE, fire, setup_hub
from .test_map import subscribe as subscribe_map
from .test_panel import subscribe as subscribe_panel
from .test_rooms import node_subentry, setup_with_areas

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

URL = "/local/wisp/ground.png"
# Hall and Office 5 m apart on the hive's layout, the access point off to one side
HIVE = encode_hive_report(1, NODE_A, 0x1234, [(NODE_A, -250, 0), (NODE_B, 250, 0)], [
    (NODE_A, 1, [(NODE_B, -60), (AP, -52)]),
    (NODE_B, 1, [(NODE_A, -61), (AP, -58)]),
])


async def ws(client, **msg) -> dict:
    await client.send_json_auto_id(msg)
    return await client.receive_json()


async def ok(client, **msg):
    reply = await ws(client, **msg)
    assert reply["success"], reply
    return reply["result"]


async def error(client, **msg) -> tuple[str, str]:
    reply = await ws(client, **msg)
    assert not reply["success"], reply
    return reply["error"]["code"], reply["error"]["message"]


async def walk(hass: HomeAssistant, udp: FakeUdp, hub, seconds: int = 3, seq: int = 0) -> None:
    """Someone on the Hall to Office line: that link is busy, the access point's are quiet."""
    for n in range(seq + 1, seq + seconds + 1):
        udp.receive(encode_report(n, NODE_A, [(AP, 0, -52, 110, 300, 20, 1), (NODE_B, 1, -60, 340, 300, 10, 1)], uptime=60), IP_A)
        udp.receive(encode_report(n, NODE_B, [(AP, 0, -58, 105, 300, 20, 1), (NODE_A, 1, -61, 320, 300, 10, 1)], uptime=60), IP_B)
        hub.clock.now += 1
        await fire(hass, 1)


async def test_someone_moving_lands_on_the_plan(
    hass: HomeAssistant, udp: FakeUdp, hass_ws_client, hass_storage: dict
) -> None:
    entry = await setup_hub(hass, HALL, OFFICE)
    hub = entry.runtime_data
    hub.clock = FakeClock()
    udp.receive(HIVE, IP_A)
    await walk(hass, udp, hub)
    (person,) = hub.map_snapshot()["people"]
    assert -2.5 <= person["x"] <= 2.5 and abs(person["y"]) < 1.5  # the hive's layout
    assert "floors" not in hub.map_snapshot()
    assert hass.states.get("sensor.wisp_position_x") is None  # no plan, no position sensors

    client = await hass_ws_client(hass)
    view = await ok(client, type="wisp/floor/set_plan", url=URL, width=10, height=6)
    assert view["plan"] == {"url": URL, "width": 10.0, "height": 6.0}
    assert view["fit"] is None and view["access_points"] == [AP]
    # Nothing placed yet: the layout's middle on the plan's centre, y turned down
    assert view["positions"][NODE_A] == {"x": 2.5, "y": 3.0, "placed": False}
    assert view["positions"][NODE_B] == {"x": 7.5, "y": 3.0, "placed": False}
    assert "people" not in hub.map_snapshot()  # the track starts over in the plan's metres

    view = await ok(client, type="wisp/floor/place", nodes={NODE_A: [2, 4], NODE_B.upper(): [8, 4]})
    assert view["positions"][NODE_A] == {"x": 2.0, "y": 4.0, "placed": True}
    assert view["positions"][NODE_B] == {"x": 8.0, "y": 4.0, "placed": True}
    assert view["fit"] | {"rotation": None} == {
        "nodes": 2, "scale": 1.2, "rotation": None, "mirror": True, "mirror_guessed": True, "error": 0.0
    }
    ap = view["positions"][AP]
    assert not ap["placed"] and 0 <= ap["x"] <= 10  # placed from the rows, in plan metres
    await walk(hass, udp, hub, seq=3)
    snapshot = hub.map_snapshot()
    (person,) = snapshot["people"]
    assert 2.0 <= person["x"] <= 8.0 and abs(person["y"] - 4.0) < 1.5  # on the Hall to Office line of the plan
    x, y = hass.states.get("sensor.wisp_position_x"), hass.states.get("sensor.wisp_position_y")
    assert x.attributes["unit_of_measurement"] == "m" and 0 < x.attributes["quality"] <= 1
    assert abs(float(x.state) - person["x"]) < 0.5 and abs(float(y.state) - person["y"]) < 0.5
    assert snapshot["floors"] == [{
        "floor": None,
        "name": "Wisp",
        "nodes": [NODE_A, NODE_B],
        "plan": {"url": URL, "width": 10.0, "height": 6.0},
        "positions": view["positions"],
    }]
    # The map's own keys stay as they were: node x and y are the hive's layout
    assert snapshot["nodes"][0] | {"online": None} == {"mac": NODE_A, "name": "Hall", "online": None, "x": -2.5, "y": 0.0}

    # The access point placed by hand; stored, and back after a restart
    await ok(client, type="wisp/floor/place", access_points={AP: [5, 0.5]})
    stored = hass_storage[f"wisp.{entry.entry_id}.plans"]
    assert stored["version"] == 1 and stored["data"] == {"floors": {"": {
        "url": URL, "width": 10.0, "height": 6.0,
        "nodes": {NODE_A: [2.0, 4.0], NODE_B: [8.0, 4.0]},
        "access_points": {AP: [5.0, 0.5]},
    }}}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    hub = entry.runtime_data
    hub.clock = FakeClock()
    udp.receive(HIVE, IP_A)
    await fire(hass, 1)
    (floor,) = hub.panel_snapshot()["floors"]
    assert floor["plan"] == {"url": URL, "width": 10.0, "height": 6.0}
    assert floor["positions"][AP] == {"x": 5.0, "y": 0.5, "placed": True}

    # None lets Wisp place one again; a new image or size keeps the rest
    view = await ok(client, type="wisp/floor/place", nodes={NODE_B: None})
    assert view["positions"][NODE_B]["placed"] is False and view["fit"] is None
    view = await ok(client, type="wisp/floor/set_plan", floor=None, url="https://example.com/plan.svg", width=12, height=7.5)
    assert view["plan"]["url"] == "https://example.com/plan.svg" and view["positions"][NODE_A]["placed"]

    # Removing the plan takes its positions; the map is the hive's again
    assert await ok(client, type="wisp/floor/clear") is None
    assert "floors" not in hub.map_snapshot() and "plan" not in hub.panel_snapshot()["floors"][0]
    assert hass_storage[f"wisp.{entry.entry_id}.plans"]["data"] == {"floors": {}}
    assert await ok(client, type="wisp/floor/clear") is None  # nothing to remove is fine

    # Deleting the hub deletes its plans
    await ok(client, type="wisp/floor/set_plan", url=URL, width=10, height=6)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"wisp.{entry.entry_id}.plans" not in hass_storage


async def test_commands_check_their_input(hass: HomeAssistant, udp: FakeUdp, hass_ws_client) -> None:
    floors, areas = fr.async_get(hass), ar.async_get(hass)
    ground, upstairs = floors.async_create("Ground floor"), floors.async_create("Upstairs")
    areas.async_create("Kitchen", floor_id=ground.floor_id)
    areas.async_create("Bedroom", floor_id=upstairs.floor_id)
    await setup_with_areas(hass, (*HALL, "kitchen"), (*OFFICE, "bedroom"))
    client = await hass_ws_client(hass)
    plan = {"type": "wisp/floor/set_plan", "floor": "ground_floor", "url": URL, "width": 10, "height": 6}

    for bad in ("javascript:alert(1)", "ftp://nas/plan.png", "local/plan.png", "", "/local/my plan.png", "https://"):
        assert (await error(client, **(plan | {"url": bad})))[0] == "invalid_format", bad
    for bad in ({"width": 0}, {"height": 501}, {"width": "wide"}, {"height": None}):
        assert (await error(client, **(plan | bad)))[0] == "invalid_format", bad
    assert await error(client, **(plan | {"floor": "cellar"})) == ("not_found", "No Wisp node is on the floor cellar.")
    assert await error(client, **(plan | {"floor": None})) == ("not_found", "No Wisp node is without a floor.")
    place = {"type": "wisp/floor/place", "floor": "ground_floor"}
    assert await error(client, **place, nodes={NODE_A: [1, 1]}) == ("not_found", "This floor has no plan yet.")

    await ok(client, **plan)
    assert await error(client, **place, nodes={NODE_B: [1, 1]}) == (
        "invalid_format", f"The node {NODE_B} is not on this floor."  # Office is upstairs
    )
    assert await error(client, **place, nodes={NODE_A: [10.5, 1]}) == (
        "invalid_format", "10.5, 1 is off the plan, which is 10 by 6 m."
    )
    assert (await error(client, **place, access_points={AP: [-1, 1]}))[0] == "invalid_format"
    for bad in ({"wisp": [1, 1]}, {NODE_A: [1]}, {NODE_A: ["a", "b"]}, {NODE_A: [1, "1e999"]}, {NODE_A: "1,1"}):
        assert (await error(client, **place, nodes=bad))[0] == "invalid_format", bad
    many = {f"02:00:00:00:00:{i:02x}": None for i in range(65)}
    assert (await error(client, **place, nodes=many))[0] == "invalid_format"
    # Unplacing a node of another floor is harmless; each floor has its own plan
    view = await ok(client, **place, nodes={NODE_A: [1, 1], NODE_B: None}, access_points={AP.replace(":", "-"): [2, 2]})
    assert view["positions"][NODE_A]["placed"] and view["positions"][AP] == {"x": 2.0, "y": 2.0, "placed": True}
    assert set(view["positions"]) == {NODE_A, AP}  # Office is not on this floor
    assert await error(client, type="wisp/floor/place", floor="upstairs", nodes={NODE_B: [1, 1]}) == (
        "not_found", "This floor has no plan yet."
    )

    # Without the hub loaded
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await error(client, **plan) == ("not_found", "Wisp is not loaded.")


async def test_commands_are_for_admins(
    hass: HomeAssistant, udp: FakeUdp, hass_ws_client, hass_read_only_access_token: str
) -> None:
    await setup_hub(hass, HALL)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    for msg in (
        {"type": "wisp/floor/set_plan", "url": URL, "width": 10, "height": 6},
        {"type": "wisp/floor/place", "nodes": {NODE_A: [1, 1]}},
        {"type": "wisp/floor/clear"},
    ):
        assert (await error(client, **msg))[0] == "unauthorized"


async def test_floors_on_the_map_and_the_panel(hass: HomeAssistant, udp: FakeUdp, hass_ws_client) -> None:
    floors, areas = fr.async_get(hass), ar.async_get(hass)
    ground = floors.async_create("Ground floor", level=0)
    areas.async_create("Kitchen", floor_id=ground.floor_id)
    entry = await setup_with_areas(hass, (*HALL, "kitchen"), OFFICE)
    hub = entry.runtime_data
    watcher = await hass_ws_client(hass)
    await subscribe_map(watcher)
    # Two floors: the map lists each one's nodes so a card can show one floor
    snapshot = (await watcher.receive_json())["event"]
    assert snapshot["floors"] == [
        {"floor": "ground_floor", "name": "Ground floor", "nodes": [NODE_A]},
        {"floor": None, "name": "Wisp", "nodes": [NODE_B]},
    ]

    udp.receive(HIVE, IP_A)
    udp.receive(encode_report(1, NODE_A, [(AP, 0, -52, 110, 300, 20, 0)], uptime=60), IP_A)
    await fire(hass, 1)
    client = await hass_ws_client(hass)
    await ok(client, type="wisp/floor/set_plan", floor="ground_floor", url=URL, width=8, height=5)
    floors_now = hub.map_snapshot()["floors"]
    # One node on the ground floor: alone, it lands on the plan's centre; Office is not drawn there
    assert floors_now[0]["positions"] == {NODE_A: {"x": 4.0, "y": 2.5, "placed": False}}
    assert floors_now[1] == {"floor": None, "name": "Wisp", "nodes": [NODE_B]}

    panel = await hass_ws_client(hass)
    await subscribe_panel(panel)
    ground_floor, own = (await panel.receive_json())["event"]["floors"]
    assert ground_floor["plan"] == {"url": URL, "width": 8.0, "height": 5.0}
    assert ground_floor["positions"] == floors_now[0]["positions"]
    assert ground_floor["access_points"] == [AP] and ground_floor["fit"] is None
    assert not {"plan", "positions", "access_points", "fit"} & set(own)

    # The floor goes from Home Assistant: its plan goes too
    areas.async_update("kitchen", floor_id=None)
    floors.async_delete(ground.floor_id)
    await hass.async_block_till_done()
    assert hub.presence.plans.floors == {}


async def test_unreadable_plans_start_clean(
    hass: HomeAssistant, udp: FakeUdp, hass_storage: dict, caplog: pytest.LogCaptureFixture
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Wisp", unique_id=DOMAIN, data={},
                            subentries_data=[node_subentry(*HALL)])
    hass_storage[f"wisp.{entry.entry_id}.plans"] = {"version": 1, "data": {"floors": {"": {"url": URL}}}}
    entry.add_to_hass(hass)
    with caplog.at_level(logging.WARNING):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert "Discarding the stored floor plans" in caplog.text
    assert entry.runtime_data.presence.plans.floors == {}
